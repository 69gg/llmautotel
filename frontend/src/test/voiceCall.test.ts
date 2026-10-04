import type { RTVIEventCallbacks } from '@pipecat-ai/client-js';
import { describe, expect, it, vi } from 'vitest';
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
      const track = { stop: vi.fn(), applyConstraints: vi.fn().mockResolvedValue(undefined) };
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
    old.onTrackStarted?.(lateTrack, { id: 'bot', name: 'bot', local: false });
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
});
