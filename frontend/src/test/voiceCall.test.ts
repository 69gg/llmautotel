import type { RTVIEventCallbacks } from '@pipecat-ai/client-js';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api';
import { VoiceCallController, type VoiceClient, type VoiceDependencies } from '../voiceCall';
import type { CallRecord, StartCallResult } from '../types';
import { fixtureSettings } from './fixtures';

vi.mock('@pipecat-ai/small-webrtc-transport', () => ({ SmallWebRTCTransport: class {} }));

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (cause: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

const record: CallRecord = { id: 'call-1', started_at: '2026-10-04T09:00:00Z', ended_at: null, status: 'active', end_reason: null, settings: fixtureSettings, transcript: [] };
const connection = { webrtcRequestParams: { endpoint: '/api/offer?call_id=call-1' }, iceConfig: { iceServers: [] } };

function setup(overrides: Partial<VoiceDependencies> = {}) {
  const clients: { client: VoiceClient; callbacks: RTVIEventCallbacks; track: { stop: ReturnType<typeof vi.fn>; applyConstraints: ReturnType<typeof vi.fn> } }[] = [];
  const startCall = vi.fn<VoiceDependencies['startCall']>().mockResolvedValue({ call: record, connection });
  const endCall = vi.fn<VoiceDependencies['endCall']>().mockResolvedValue({ ...record, ended_at: '2026-10-04T09:00:10Z', status: 'ended' });
  const dependencies: VoiceDependencies = {
    startCall, endCall,
    createClient: callbacks => {
      const track = { kind: 'audio', stop: vi.fn(), applyConstraints: vi.fn().mockResolvedValue(undefined) };
      const client: VoiceClient = {
        initDevices: vi.fn().mockResolvedValue(undefined),
        connect: vi.fn(async () => { callbacks.onBotReady?.({} as never); }),
        disconnect: vi.fn(async () => { callbacks.onDisconnected?.(); }),
        enableMic: vi.fn(),
        tracks: () => ({ local: { audio: track as unknown as MediaStreamTrack } }),
      };
      clients.push({ client, callbacks, track });
      return client;
    },
    ...overrides,
  };
  return { controller: new VoiceCallController(dependencies), clients, startCall, endCall };
}

function playback() {
  const frames: FrameRequestCallback[] = [];
  const close = vi.fn().mockResolvedValue(undefined);
  const resume = vi.fn().mockResolvedValue(undefined);
  class TestStream {
    constructor(private readonly tracks: MediaStreamTrack[]) {}
    getAudioTracks() { return this.tracks; }
  }
  class TestAudioContext {
    destination = {};
    close = close;
    resume = resume;
    createMediaStreamSource() { return { connect: vi.fn(), disconnect: vi.fn() }; }
    createAnalyser() { return { fftSize: 256, connect: vi.fn(), disconnect: vi.fn(), getFloatTimeDomainData: (samples: Float32Array) => samples.fill(.05) }; }
    createGain() { return { gain: { value: 1 }, connect: vi.fn(), disconnect: vi.fn() }; }
  }
  vi.stubGlobal('MediaStream', TestStream);
  vi.stubGlobal('AudioContext', TestAudioContext);
  vi.stubGlobal('requestAnimationFrame', vi.fn((callback: FrameRequestCallback) => { frames.push(callback); return frames.length; }));
  vi.stubGlobal('cancelAnimationFrame', vi.fn());
  const audio = { srcObject: null as MediaStream | null, play: vi.fn().mockResolvedValue(undefined), pause: vi.fn() };
  return { audio, frames, close, resume };
}

afterEach(() => { vi.unstubAllGlobals(); });

describe('语音会话生命周期', () => {
  it('麦克风拒绝时不创建服务端会话，并释放客户端', async () => {
    const disconnect = vi.fn().mockResolvedValue(undefined);
    const denied = setup({ createClient: () => ({
      initDevices: vi.fn().mockRejectedValue(new DOMException('denied', 'NotAllowedError')),
      connect: vi.fn(), disconnect, enableMic: vi.fn(), tracks: () => ({ local: {} }),
    }) });
    await denied.controller.start();
    expect(denied.startCall).not.toHaveBeenCalled();
    expect(denied.controller.getSnapshot().error).toContain('允许浏览器访问麦克风');
    expect(denied.controller.getSnapshot().state).toBe('ended');
    expect(disconnect).toHaveBeenCalledTimes(1);
  });

  it('先授权和设置回声消除再创建会话，重复开始只创建一通', async () => {
    const test = setup();
    await Promise.all([test.controller.start(), test.controller.start()]);
    expect(test.clients).toHaveLength(1);
    expect(test.startCall).toHaveBeenCalledTimes(1);
    expect(test.clients[0].track.applyConstraints).toHaveBeenCalledWith({ echoCancellation: true, noiseSuppression: true, autoGainControl: true });
    expect(vi.mocked(test.clients[0].client.initDevices).mock.invocationCallOrder[0]).toBeLessThan(test.startCall.mock.invocationCallOrder[0]);
    expect(test.clients[0].client.connect).toHaveBeenCalledWith(connection);
    await test.controller.end();
  });

  it('创建失败（包括其他标签页占用）释放麦克风，保留业务提示', async () => {
    const test = setup({ startCall: vi.fn().mockRejectedValue(new ApiError('已有一通会话', 409)) });
    await test.controller.start();
    expect(test.controller.getSnapshot().error).toBe('已有一通会话');
    expect(test.clients[0].track.stop).toHaveBeenCalled();
    expect(test.clients[0].client.disconnect).toHaveBeenCalled();
    expect(test.endCall).not.toHaveBeenCalled();
  });

  it('连接失败后结束已经创建的会话，不暴露原始 SDK 错误', async () => {
    const test = setup();
    const start = test.controller.start();
    vi.mocked(test.clients[0].client.connect).mockRejectedValue(new Error('raw provider response'));
    await start;
    expect(test.endCall).toHaveBeenCalledWith('call-1', 'connection_lost');
    expect(test.controller.getSnapshot().error).toBe('通话连接失败，请检查服务状态后重试。');
    expect(test.clients[0].track.stop).toHaveBeenCalled();
  });

  it('生成中挂断立即停音，结束请求完成前禁止新会话', async () => {
    const ended = deferred<CallRecord>();
    const test = setup({ endCall: vi.fn().mockReturnValue(ended.promise) });
    await test.controller.start();
    test.clients[0].callbacks.onBotLlmStarted?.();
    expect(test.controller.getSnapshot().state).toBe('thinking');
    const finishing = test.controller.end();
    expect(test.clients[0].track.stop).toHaveBeenCalled();
    expect(test.controller.getSnapshot().state).toBe('ending');
    await test.controller.start();
    expect(test.startCall).toHaveBeenCalledTimes(1);
    ended.resolve({ ...record, status: 'ended' });
    await finishing;
    expect(test.controller.getSnapshot().state).toBe('ended');
  });

  it('意外断开释放设备并结束会话，随后可以重新开始', async () => {
    const test = setup();
    await test.controller.start();
    test.clients[0].callbacks.onDisconnected?.();
    await vi.waitFor(() => expect(test.controller.getSnapshot().state).toBe('ended'));
    expect(test.endCall).toHaveBeenCalledWith('call-1', 'connection_lost');
    expect(test.clients[0].track.stop).toHaveBeenCalled();
    await test.controller.start();
    expect(test.startCall).toHaveBeenCalledTimes(2);
    await test.controller.end();
  });

  it('旧连接迟到的状态、音量、文字和音轨不污染新连接', async () => {
    const test = setup();
    await test.controller.start();
    const old = test.clients[0].callbacks;
    await test.controller.end();
    await test.controller.start();
    old.onBotStartedSpeaking?.();
    old.onLocalAudioLevel?.(.9);
    old.onServerMessage?.({ type: 'transcript', entry: { role: 'assistant', text: '旧结果', timestamp: 'old', interrupted: false } });
    const lateTrack = { stop: vi.fn(), kind: 'audio' } as unknown as MediaStreamTrack;
    old.onTrackStarted?.(lateTrack);
    expect(lateTrack.stop).toHaveBeenCalled();
    expect(test.controller.getSnapshot().state).toBe('listening');
    expect(test.controller.getSnapshot().localLevel).toBe(0);
    expect(test.controller.getSnapshot().transcript).toEqual([]);
    await test.controller.end();
  });

  it('仅服务端已输出文字进入展示，结束时采用持久化记录', async () => {
    const entry = { role: 'assistant' as const, text: '完整句。中断句', timestamp: '2026-10-04T09:00:01Z', interrupted: true };
    const test = setup({ endCall: vi.fn().mockResolvedValue({ ...record, status: 'ended', transcript: [entry] }) });
    await test.controller.start();
    const callbacks = test.clients[0].callbacks;
    callbacks.onBotTranscript?.({ text: '未播放生成稿' } as never);
    expect(test.controller.getSnapshot().transcript).toEqual([]);
    callbacks.onServerMessage?.({ type: 'transcript', entry });
    expect(test.controller.getSnapshot().transcript).toEqual([entry]);
    await test.controller.end();
    expect(test.controller.getSnapshot().transcript).toEqual([entry]);
  });

  it('挂断后才到达的创建响应立即结束，不能继续连接', async () => {
    const response = deferred<StartCallResult>();
    const test = setup({ startCall: vi.fn().mockReturnValue(response.promise) });
    const start = test.controller.start();
    await vi.waitFor(() => expect(test.clients[0].track.applyConstraints).toHaveBeenCalled());
    const finish = test.controller.end();
    await test.controller.start();
    expect(test.clients).toHaveLength(1);
    response.resolve({ call: record, connection });
    await Promise.all([start, finish]);
    expect(test.endCall).toHaveBeenCalledWith('call-1', 'user_hangup');
    expect(test.clients[0].client.connect).not.toHaveBeenCalled();
  });

  it('授权在挂断后才完成时立即停止音轨，不创建会话', async () => {
    const permission = deferred<void>();
    const stop = vi.fn();
    const late = setup({ createClient: () => ({ initDevices: () => permission.promise, connect: vi.fn(), disconnect: vi.fn().mockResolvedValue(undefined), enableMic: vi.fn(), tracks: () => ({ local: { audio: { stop, applyConstraints: vi.fn() } as unknown as MediaStreamTrack } }) }) });
    const start = late.controller.start();
    await late.controller.end();
    permission.resolve(undefined);
    await start;
    expect(late.startCall).not.toHaveBeenCalled();
    expect(stop).toHaveBeenCalled();
  });

  it('静音调用实际客户端并可取消，配置快照保持开始时的值', async () => {
    const test = setup();
    await test.controller.start();
    test.controller.toggleMute();
    expect(test.clients[0].client.enableMic).toHaveBeenLastCalledWith(false);
    expect(test.controller.getSnapshot().muted).toBe(true);
    test.controller.toggleMute();
    expect(test.clients[0].client.enableMic).toHaveBeenLastCalledWith(true);
    expect(test.controller.getSnapshot().call?.settings).toEqual(fixtureSettings);
    await test.controller.end();
  });

  it('SDK 无 participant 的远端音轨实际播放和反馈音量，挂断清理且旧采样不能复活', async () => {
    const media = playback();
    const test = setup();
    test.controller.attachAudio(media.audio as unknown as HTMLAudioElement);
    await test.controller.start();
    const remote = { kind: 'audio', stop: vi.fn() } as unknown as MediaStreamTrack;
    test.clients[0].callbacks.onTrackStarted?.(remote);
    expect(media.audio.srcObject?.getAudioTracks()).toEqual([remote]);
    expect(media.audio.play).toHaveBeenCalledTimes(1);
    media.frames[0](0);
    expect(test.controller.getSnapshot().remoteLevel).toBeCloseTo(.2);
    const oldFrame = media.frames[0];
    await test.controller.end();
    expect(media.audio.pause).toHaveBeenCalledTimes(1);
    expect(media.audio.srcObject).toBeNull();
    expect(remote.stop).toHaveBeenCalled();
    expect(media.close).toHaveBeenCalledTimes(1);
    oldFrame(1);
    expect(test.controller.getSnapshot().remoteLevel).toBe(0);
    await test.controller.start();
    oldFrame(2);
    expect(media.audio.play).toHaveBeenCalledTimes(1);
    expect(test.controller.getSnapshot().remoteLevel).toBe(0);
    await test.controller.end();
  });

  it('本地轨道即使没有 participant 也不会回放，显式 local 标记同样排除', async () => {
    const media = playback();
    const test = setup();
    test.controller.attachAudio(media.audio as unknown as HTMLAudioElement);
    await test.controller.start();
    const local = test.clients[0].client.tracks().local.audio!;
    test.clients[0].callbacks.onTrackStarted?.(local);
    const markedLocal = { kind: 'audio', stop: vi.fn() } as unknown as MediaStreamTrack;
    test.clients[0].callbacks.onTrackStarted?.(markedLocal, { id: 'local', name: '', local: true });
    expect(media.audio.srcObject).toBeNull();
    expect(media.audio.play).not.toHaveBeenCalled();
    expect(media.frames).toHaveLength(0);
    await test.controller.end();
    expect(markedLocal.stop).toHaveBeenCalled();
  });

  it('无 participant 远端轨播放被浏览器拒绝时显示恢复入口，点击后重试实际播放', async () => {
    const media = playback();
    media.audio.play.mockRejectedValueOnce(new DOMException('blocked', 'NotAllowedError'));
    const test = setup();
    test.controller.attachAudio(media.audio as unknown as HTMLAudioElement);
    await test.controller.start();
    const remote = { kind: 'audio', stop: vi.fn() } as unknown as MediaStreamTrack;
    test.clients[0].callbacks.onTrackStarted?.(remote);
    await vi.waitFor(() => expect(test.controller.getSnapshot().audioBlocked).toBe(true));
    await test.controller.resumeAudio();
    expect(media.audio.play).toHaveBeenCalledTimes(2);
    expect(media.resume).toHaveBeenCalledTimes(2);
    expect(test.controller.getSnapshot().audioBlocked).toBe(false);
    expect(media.audio.srcObject?.getAudioTracks()).toEqual([remote]);
    await test.controller.end();
  });
});
