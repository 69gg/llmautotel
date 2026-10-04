import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { NativeMediaManager } from '../nativeMediaManager';

class TestStream {
  constructor(private readonly tracks: MediaStreamTrack[]) {}
  getTracks() { return this.tracks; }
  getAudioTracks() { return this.tracks; }
}

function microphone() {
  const track = { kind: 'audio', enabled: true, readyState: 'live', getSettings: () => ({ deviceId: 'mic' }), stop: vi.fn(() => { track.readyState = 'ended'; }) };
  return { track, stream: new TestStream([track as unknown as MediaStreamTrack]) };
}

const frames: FrameRequestCallback[] = [];
const contexts: { close: ReturnType<typeof vi.fn>; resume: ReturnType<typeof vi.fn> }[] = [];

class TestAudioContext {
  destination = {};
  close = vi.fn().mockResolvedValue(undefined);
  resume = vi.fn().mockResolvedValue(undefined);
  constructor() { contexts.push(this); }
  createMediaStreamSource() { return { connect: vi.fn(), disconnect: vi.fn() }; }
  createAnalyser() { return { fftSize: 256, connect: vi.fn(), disconnect: vi.fn(), getFloatTimeDomainData: (samples: Float32Array) => samples.fill(.05) }; }
  createGain() { return { gain: { value: 1 }, connect: vi.fn(), disconnect: vi.fn() }; }
}

beforeEach(() => {
  frames.length = 0;
  contexts.length = 0;
  vi.stubGlobal('AudioContext', TestAudioContext);
  vi.stubGlobal('MediaStream', TestStream);
  vi.stubGlobal('requestAnimationFrame', vi.fn((callback: FrameRequestCallback) => { frames.push(callback); return frames.length; }));
  vi.stubGlobal('cancelAnimationFrame', vi.fn());
});
afterEach(() => { vi.unstubAllGlobals(); });

function devices(getUserMedia: ReturnType<typeof vi.fn>) {
  const enumerateDevices = vi.fn().mockResolvedValue([{ kind: 'audioinput', deviceId: 'mic', groupId: '', label: '测试麦克风', toJSON: () => ({}) }]);
  vi.stubGlobal('navigator', { mediaDevices: { getUserMedia, enumerateDevices } });
}

describe('本机媒体适配器', () => {
  it('授权拒绝直接抛出，不吞错误或创建音频分析上下文', async () => {
    const denied = new DOMException('denied', 'NotAllowedError');
    devices(vi.fn().mockRejectedValue(denied));
    const manager = new NativeMediaManager();
    await expect(manager.initialize()).rejects.toBe(denied);
    expect(manager.tracks().local.audio).toBeUndefined();
    expect(contexts).toHaveLength(0);
  });

  it('实际静音停音轨，断开停止音轨并关闭 AudioContext，重开可用', async () => {
    const first = microphone();
    const second = microphone();
    const getUserMedia = vi.fn().mockResolvedValueOnce(first.stream).mockResolvedValueOnce(second.stream);
    devices(getUserMedia);
    const level = vi.fn();
    const trackStarted = vi.fn();
    const manager = new NativeMediaManager();
    manager.setClientOptions({ enableMic: true, callbacks: { onLocalAudioLevel: level, onTrackStarted: trackStarted } });
    await manager.initialize();
    expect(getUserMedia).toHaveBeenCalledWith({ audio: { deviceId: undefined, echoCancellation: true, noiseSuppression: true, autoGainControl: true }, video: false });
    expect(manager.tracks().local.audio).toBe(first.track);
    expect(trackStarted).toHaveBeenCalledWith(first.track, { id: 'local', name: '', local: true });
    frames[0](0);
    expect(level).toHaveBeenLastCalledWith(expect.any(Number));
    const oldFrame = frames[0];
    manager.enableMic(false);
    expect(first.track.enabled).toBe(false);
    expect(level).toHaveBeenLastCalledWith(0);
    manager.enableMic(true);
    expect(first.track.enabled).toBe(true);
    await manager.disconnect();
    expect(first.track.stop).toHaveBeenCalled();
    expect(contexts[0].close).toHaveBeenCalledTimes(1);
    expect(manager.tracks().local.audio).toBeUndefined();
    await manager.initialize();
    expect(manager.tracks().local.audio).toBe(second.track);
    expect(trackStarted).toHaveBeenLastCalledWith(second.track, { id: 'local', name: '', local: true });
    level.mockClear();
    oldFrame(1);
    expect(level).not.toHaveBeenCalled();
    expect(second.track.stop).not.toHaveBeenCalled();
    await manager.disconnect();
    expect(contexts[1].close).toHaveBeenCalledTimes(1);
  });

  it('断开后迟到的授权结果马上停轨，不影响随后新会话', async () => {
    let grant!: (stream: TestStream) => void;
    const permission = new Promise<TestStream>(resolve => { grant = resolve; });
    const late = microphone();
    const next = microphone();
    devices(vi.fn().mockReturnValueOnce(permission).mockResolvedValueOnce(next.stream));
    const manager = new NativeMediaManager();
    const pending = manager.initialize();
    await manager.disconnect();
    await manager.initialize();
    grant(late.stream);
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' });
    expect(late.track.stop).toHaveBeenCalled();
    expect(manager.tracks().local.audio).toBe(next.track);
    expect(next.track.stop).not.toHaveBeenCalled();
    await manager.disconnect();
  });
});
