import { LogLevel, PipecatClient, type RTVIEventCallbacks, type Tracks } from '@pipecat-ai/client-js';
import { SmallWebRTCTransport } from '@pipecat-ai/small-webrtc-transport';
import { NativeMediaManager } from './nativeMediaManager';
import { createAudioMeter, type AudioMeter } from './audioMeter';
import { api, ApiError } from './api';
import type { CallConnection, CallRecord, EndReason, StartCallResult, TranscriptEntry } from './types';

export type CallState = 'idle' | 'connecting' | 'listening' | 'recognizing' | 'thinking' | 'speaking' | 'ending' | 'ended';

export interface VoiceSnapshot {
  state: CallState;
  call: CallRecord | null;
  transcript: TranscriptEntry[];
  muted: boolean;
  userSpeaking: boolean;
  localLevel: number;
  remoteLevel: number;
  error: string;
  audioBlocked: boolean;
}

export interface VoiceClient {
  initDevices(): Promise<void>;
  connect(connection: CallConnection): Promise<unknown>;
  disconnect(): Promise<void>;
  enableMic(enabled: boolean): void;
  tracks(): Tracks;
}

export interface VoiceDependencies {
  createClient(callbacks: RTVIEventCallbacks): VoiceClient;
  startCall(): Promise<StartCallResult>;
  endCall(id: string, reason: EndReason): Promise<CallRecord>;
}

interface Session {
  client: VoiceClient;
  live: boolean;
  id: string | null;
  reason: EndReason;
  serverEnded: boolean;
  tracks: Set<MediaStreamTrack>;
  creation?: Promise<StartCallResult>;
  finishing?: Promise<void>;
}

const initialSnapshot: VoiceSnapshot = { state: 'idle', call: null, transcript: [], muted: false, userSpeaking: false, localLevel: 0, remoteLevel: 0, error: '', audioBlocked: false };
const defaultDependencies: VoiceDependencies = {
  createClient: callbacks => {
    const mediaManager = new NativeMediaManager();
    const client = new PipecatClient({ transport: new SmallWebRTCTransport({ mediaManager }), enableMic: true, enableCam: false, callbacks });
    client.setLogLevel(LogLevel.NONE);
    return {
      initDevices: () => client.initDevices(),
      connect: connection => client.connect(connection),
      enableMic: enabled => client.enableMic(enabled),
      tracks: () => client.tracks(),
      disconnect: async () => {
        try { await client.disconnect(); }
        finally { await mediaManager.disconnect(); }
      },
    };
  },
  startCall: () => api.startCall(),
  endCall: (id, reason) => api.endCall(id, reason),
};

function isTranscript(value: unknown): value is TranscriptEntry {
  if (!value || typeof value !== 'object') return false;
  const entry = value as Partial<TranscriptEntry>;
  return (entry.role === 'assistant' || entry.role === 'user') && typeof entry.text === 'string' && typeof entry.timestamp === 'string' && typeof entry.interrupted === 'boolean';
}

function stopTracks(session: Session) {
  session.tracks.forEach(track => track.stop());
  const tracks = session.client.tracks();
  Object.values(tracks.local).forEach(track => track?.stop());
  Object.values(tracks.bot ?? {}).forEach(track => track?.stop());
}

/** Owns one call and rejects callbacks from every previous connection. */
export class VoiceCallController {
  private snapshot: VoiceSnapshot = initialSnapshot;
  private listeners = new Set<() => void>();
  private session: Session | null = null;
  private audio: HTMLAudioElement | null = null;
  private audioTrack: MediaStreamTrack | null = null;
  private remoteMeter: AudioMeter | null = null;

  constructor(private readonly dependencies: VoiceDependencies = defaultDependencies) {}

  getSnapshot = () => this.snapshot;
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  };

  private update(value: Partial<VoiceSnapshot>) {
    this.snapshot = { ...this.snapshot, ...value };
    this.listeners.forEach(listener => listener());
  }

  attachAudio = (element: HTMLAudioElement | null) => {
    this.audio = element;
    if (element && this.audioTrack) this.playTrack(this.audioTrack);
  };

  private playTrack(track: MediaStreamTrack) {
    this.audioTrack = track;
    if (!this.audio) return;
    this.audio.srcObject = new MediaStream([track]);
    const session = this.session;
    void this.audio.play().catch(() => {
      if (session?.live && session === this.session) this.update({ audioBlocked: true });
    });
  }

  private monitorRemoteTrack(track: MediaStreamTrack, session: Session) {
    void this.remoteMeter?.stop().catch(() => undefined);
    this.remoteMeter = createAudioMeter(track, level => {
      if (this.session === session && session.live) this.update({ remoteLevel: level });
    });
  }

  resumeAudio = async () => {
    if (!this.audio || !this.session?.live) return;
    try {
      await this.audio.play();
      await this.remoteMeter?.resume();
      this.update({ audioBlocked: false });
    } catch {
      this.update({ audioBlocked: true });
    }
  };

  start = async () => {
    if (this.session) return;
    this.update({ ...initialSnapshot, state: 'connecting' });
    let session: Session;
    const current = () => this.session === session && session.live;
    const change = (value: Partial<VoiceSnapshot>) => {
      if (current()) this.update(session.serverEnded ? { ...value, state: 'ending', userSpeaking: false } : value);
    };
    const callbacks: RTVIEventCallbacks = {
      onBotReady: () => change({ state: 'listening' }),
      onDisconnected: () => { if (current()) void this.finish(session, session.serverEnded ? '' : '语音连接已断开，可以重新开始。', 'connection_lost'); },
      onError: () => { if (current()) void this.finish(session, session.serverEnded ? '' : '语音连接异常，请重新开始。', 'connection_lost'); },
      onDeviceError: () => { if (current()) void this.finish(session, '无法使用麦克风，请检查浏览器授权和设备连接。', 'connection_lost'); },
      onLocalAudioLevel: level => change({ localLevel: Math.max(0, Math.min(1, level)) }),
      onRemoteAudioLevel: level => change({ remoteLevel: Math.max(0, Math.min(1, level)) }),
      onUserStartedSpeaking: () => change({ state: 'listening', userSpeaking: true }),
      onUserStoppedSpeaking: () => change({ state: 'recognizing', userSpeaking: false }),
      onBotStartedSpeaking: () => change({ state: 'speaking', userSpeaking: false }),
      onBotStoppedSpeaking: () => { if (current() && this.snapshot.state === 'speaking') change({ state: 'listening', remoteLevel: 0 }); },
      onBotLlmStarted: () => change({ state: 'thinking' }),
      onTrackStarted: (track, participant) => {
        if (!current()) { track.stop(); return; }
        session.tracks.add(track);
        // SmallWebRTC remote-track events omit participant; never play the local microphone.
        if (track.kind === 'audio' && !participant?.local && track !== session.client.tracks().local.audio) {
          this.playTrack(track);
          this.monitorRemoteTrack(track, session);
        }
      },
      onServerMessage: (message: unknown) => {
        if (!current() || !message || typeof message !== 'object') return;
        const data = message as { type?: string; state?: string; entry?: unknown; message?: unknown; reason?: unknown };
        if (data.type === 'transcript' && isTranscript(data.entry)) {
          change({ transcript: [...this.snapshot.transcript, data.entry] });
        } else if (data.type === 'call-ended' && data.reason === 'ai_hangup') {
          // Let the server finish the goodbye audio before its transport disconnects.
          session.serverEnded = true;
          change({ state: 'ending', userSpeaking: false });
        } else if (data.type === 'state') {
          if (data.state === 'ended') void this.finish(session, '', 'connection_lost');
          else if (['listening', 'recognizing', 'thinking', 'speaking'].includes(data.state ?? '')) change({ state: data.state as CallState });
        } else if (data.type === 'error') {
          void this.finish(session, typeof data.message === 'string' ? data.message : '模型请求失败，请检查配置后重新开始。', 'connection_lost');
        }
      },
    };
    let client: VoiceClient;
    try {
      client = this.dependencies.createClient(callbacks);
    } catch {
      this.update({ state: 'ended', error: '初始化语音设备失败，请刷新页面后重试。' });
      return;
    }
    session = { client, id: null, live: true, reason: 'user_hangup', serverEnded: false, tracks: new Set() };
    this.session = session;
    try {
      await client.initDevices();
      if (!current()) { stopTracks(session); return; }
      await client.tracks().local.audio?.applyConstraints({ echoCancellation: true, noiseSuppression: true, autoGainControl: true });
      if (!current()) { stopTracks(session); return; }
      session.creation = this.dependencies.startCall();
      const result = await session.creation;
      session.id = result.call.id;
      if (!current()) { stopTracks(session); await session.finishing; return; }
      change({ call: result.call });
      await client.connect(result.connection);
      if (!current()) stopTracks(session);
    } catch (cause) {
      if (!current()) { stopTracks(session); return; }
      const permission = cause instanceof DOMException && ['NotAllowedError', 'NotFoundError', 'NotReadableError'].includes(cause.name);
      const error = permission ? '无法使用麦克风，请允许浏览器访问麦克风并确认设备可用。' : cause instanceof ApiError ? cause.message : '通话连接失败，请检查服务状态后重试。';
      await this.finish(session, error, 'connection_lost');
    }
  };

  private finish(session: Session, error = '', reason: EndReason = 'user_hangup'): Promise<void> {
    if (session.finishing) return session.finishing;
    session.live = false;
    session.reason = reason;
    if (this.session === session) this.update({ state: 'ending', error: error || this.snapshot.error, userSpeaking: false, localLevel: 0, remoteLevel: 0, audioBlocked: false });
    this.audio?.pause();
    if (this.audio) this.audio.srcObject = null;
    this.audioTrack = null;
    void this.remoteMeter?.stop().catch(() => undefined);
    this.remoteMeter = null;
    stopTracks(session);
    session.finishing = (async () => {
      try { await session.client.disconnect(); } catch { /* Tracks were stopped before disconnect. */ }
      stopTracks(session);
      try {
        if (session.creation && !session.id) {
          const result = await session.creation.catch(() => null);
          session.id = result?.call.id ?? null;
        }
        if (session.id) {
          const record = await this.dependencies.endCall(session.id, session.reason);
          if (this.session === session) this.update({ call: record, transcript: record.transcript });
        }
      } catch {
        if (this.session === session && !this.snapshot.error) this.update({ error: '本机音频已停止，通话记录暂未确认保存，请检查服务状态。' });
      } finally {
        if (this.session === session) {
          this.session = null;
          this.update({ state: 'ended', muted: false });
        }
      }
    })();
    return session.finishing;
  }

  end = async () => { if (this.session) await this.finish(this.session); };
  toggleMute = () => {
    if (!this.session?.live || this.snapshot.state === 'connecting') return;
    const muted = !this.snapshot.muted;
    this.session.client.enableMic(!muted);
    this.update({ muted, localLevel: muted ? 0 : this.snapshot.localLevel });
  };
  dispose = () => { if (this.session) void this.finish(this.session); };
}
