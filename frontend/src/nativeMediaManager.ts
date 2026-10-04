import type { PipecatClientOptions, RTVIEventCallbacks, Tracks } from '@pipecat-ai/client-js';
import { createAudioMeter, type AudioMeter } from './audioMeter';

/** SmallWebRTC's MediaManager contract, using only native browser audio APIs. */
export class NativeMediaManager {
  private callbacks: RTVIEventCallbacks = {};
  private stream: MediaStream | null = null;
  private meter: AudioMeter | null = null;
  private generation = 0;
  private micEnabled = true;
  private microphoneId = '';
  private mic: MediaDeviceInfo | Record<string, never> = {};

  setClientOptions(options: Pick<PipecatClientOptions, 'callbacks' | 'enableMic'>) {
    this.callbacks = options.callbacks ?? {};
    this.micEnabled = options.enableMic ?? true;
  }

  setUserAudioCallback(_callback: (data: ArrayBuffer) => void) { /* WebRTC carries audio directly. */ }

  async initialize(): Promise<void> {
    if (this.stream?.getAudioTracks().some(track => track.readyState !== 'ended')) return;
    const generation = ++this.generation;
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { deviceId: this.microphoneId ? { exact: this.microphoneId } : undefined, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      video: false,
    });
    if (generation !== this.generation) {
      stream.getTracks().forEach(track => track.stop());
      throw new DOMException('The device request was cancelled.', 'AbortError');
    }
    this.stream = stream;
    const track = stream.getAudioTracks()[0];
    if (!track) { await this.disconnect(); throw new DOMException('No microphone track.', 'NotFoundError'); }
    track.enabled = this.micEnabled;
    try {
      const devices = await navigator.mediaDevices.enumerateDevices();
      if (generation !== this.generation) return;
      const microphones = devices.filter(device => device.kind === 'audioinput');
      this.mic = microphones.find(device => device.deviceId === track.getSettings().deviceId) ?? microphones[0] ?? {};
      this.callbacks.onAvailableMicsUpdated?.(microphones);
      this.callbacks.onAvailableSpeakersUpdated?.(devices.filter(device => device.kind === 'audiooutput'));
      this.callbacks.onMicUpdated?.(this.mic);
      this.callbacks.onTrackStarted?.(track, { id: 'local', name: '', local: true });
      this.meter = createAudioMeter(track, level => {
        if (generation === this.generation) this.callbacks.onLocalAudioLevel?.(this.micEnabled ? level : 0);
      });
    } catch (cause) {
      if (generation === this.generation) await this.disconnect();
      throw cause;
    }
  }

  async connect(): Promise<void> { if (!this.stream) await this.initialize(); }

  async disconnect(): Promise<void> {
    ++this.generation;
    const stream = this.stream;
    const meter = this.meter;
    this.stream = null;
    this.meter = null;
    stream?.getTracks().forEach(track => track.stop());
    await meter?.stop();
  }

  async getAllMics(): Promise<MediaDeviceInfo[]> { return (await navigator.mediaDevices.enumerateDevices()).filter(device => device.kind === 'audioinput'); }
  async getAllCams(): Promise<MediaDeviceInfo[]> { return []; }
  async getAllSpeakers(): Promise<MediaDeviceInfo[]> { return (await navigator.mediaDevices.enumerateDevices()).filter(device => device.kind === 'audiooutput'); }
  updateMic(id: string): void {
    if (this.stream) throw new Error('请结束当前通话后切换麦克风。');
    this.microphoneId = id;
  }
  updateCam(_id: string): void { throw new Error('当前版本不支持摄像头。'); }
  updateSpeaker(_id: string): void { throw new Error('请在系统设置中选择扬声器。'); }
  enableMic(enabled: boolean): void {
    this.micEnabled = enabled;
    this.stream?.getAudioTracks().forEach(track => { track.enabled = enabled; });
    if (!enabled) this.callbacks.onLocalAudioLevel?.(0);
  }
  enableCam(enabled: boolean): void { if (enabled) throw new Error('当前版本不支持摄像头。'); }
  enableScreenShare(enabled: boolean): void { if (enabled) throw new Error('当前版本不支持屏幕共享。'); }
  async userStartedSpeaking(): Promise<void> { /* Backend interruption clears WebRTC output. */ }
  bufferBotAudio(_data: ArrayBuffer | Int16Array, _id?: string): undefined { return undefined; }
  get selectedMic(): MediaDeviceInfo | Record<string, never> { return this.mic; }
  get selectedCam(): Record<string, never> { return {}; }
  get selectedSpeaker(): Record<string, never> { return {}; }
  get isMicEnabled(): boolean { return this.micEnabled; }
  get isCamEnabled(): boolean { return false; }
  get isSharingScreen(): boolean { return false; }
  get supportsScreenShare(): boolean { return false; }
  tracks(): Tracks { return { local: { audio: this.stream?.getAudioTracks()[0] } }; }
}
