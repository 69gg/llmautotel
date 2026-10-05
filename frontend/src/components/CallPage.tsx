import { useEffect, useRef, useState, type CSSProperties } from 'react';
import { conversationConfigured, conversationMode, conversationTitle, type Settings } from '../types';
import { VoiceCallController, type CallState, type VoiceSnapshot } from '../voiceCall';
import { Icon } from './Icon';

const stateLabels: Record<CallState, string> = { idle: '准备开始', connecting: '连接中', listening: '倾听中', recognizing: '识别中', thinking: '思考中', speaking: '说话中', ending: '正在结束', ended: '通话已结束' };

export function duration(start: string, end?: string | null, now = Date.now()): string {
  const seconds = Math.max(0, Math.floor(((end ? new Date(end).getTime() : now) - new Date(start).getTime()) / 1000));
  return `${Math.floor(seconds / 60).toString().padStart(2, '0')}:${(seconds % 60).toString().padStart(2, '0')}`;
}

export function CallPage({ settings, voice, controller, onConfigure }: { settings: Settings; voice: VoiceSnapshot; controller: VoiceCallController; onConfigure: () => void }) {
  const [now, setNow] = useState(Date.now());
  const transcriptEnd = useRef<HTMLDivElement>(null);
  const running = !['idle', 'ended'].includes(voice.state);
  const connected = !['idle', 'ended', 'connecting', 'ending'].includes(voice.state);
  const hasConfig = conversationConfigured(settings) && [ settings.asr.base_url, settings.asr.model, settings.llm.base_url, settings.llm.model, settings.tts.base_url, settings.tts.model, settings.tts.voice].every(value => value.trim());
  const snapshot = voice.call?.settings ?? settings;
  const goal = conversationTitle(snapshot);
  const level = voice.state === 'speaking' ? voice.remoteLevel : voice.muted ? 0 : voice.localLevel;

  useEffect(() => {
    if (!running) return;
    const interval = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(interval);
  }, [running]);
  useEffect(() => { transcriptEnd.current?.scrollIntoView?.({ behavior: 'smooth', block: 'nearest' }); }, [voice.transcript]);

  return <div className="call-page">
    <div className="page-heading"><div><p className="eyebrow">语音通话</p><h1>开始一次对话</h1><p className="subtle">AI 会先开口。你说话时，它会停下来倾听。</p></div><button className="button secondary" onClick={onConfigure}><Icon name="settings" size={16} />对话配置</button></div>
    <div className="call-layout"><section className="call-surface" aria-label="通话控制">
      <div className="call-meta"><span className="call-mode"><span className={`status-dot ${voice.error ? 'offline' : ''}`} />{running ? '语音会话' : '本机语音'}</span><time className="call-time">{voice.call ? duration(voice.call.started_at, voice.call.ended_at, now) : '00:00'}</time></div>
      <div className={`voice-orb ${connected ? 'connected' : ''} ${voice.state === 'speaking' ? 'speaking' : ''}`} style={{ '--voice-level': Math.max(0, Math.min(1, level)) } as CSSProperties}><div className="orb-halo" /><div className="orb-core"><span /><span /><span /><span /><span /></div></div>
      <div className="voice-status" role="status"><h2>{voice.userSpeaking ? '正在听你说' : voice.muted && connected ? '麦克风已静音' : stateLabels[voice.state]}</h2><p>{voice.state === 'idle' ? '戴上耳机，让声音更清晰' : voice.state === 'connecting' ? '等待麦克风授权并建立连接' : voice.state === 'thinking' ? 'AI 正在组织回答' : voice.state === 'recognizing' ? '正在识别你刚才说的话' : voice.state === 'speaking' ? '随时开口，打断当前回答' : voice.state === 'ended' ? '文字记录保存在本机' : voice.muted ? '点击下方按钮继续说话' : '自然地说话就好'}</p></div>
      {voice.audioBlocked && <div className="audio-prompt"><p>浏览器暂停了声音播放。</p><button className="text-button" onClick={() => void controller.resumeAudio()}>点击播放声音</button></div>}
      {voice.error && <p className="call-error" role="alert">{voice.error}</p>}
      {!running ? <button className="button primary start-call" disabled={!hasConfig} onClick={() => void controller.start()}><Icon name="phone" size={18} />{voice.state === 'ended' ? '再次通话' : '开始通话'}</button> : <div className="call-controls"><button className={`control-button ${voice.muted ? 'muted' : ''}`} disabled={!connected} onClick={controller.toggleMute} aria-label={voice.muted ? '取消麦克风静音' : '麦克风静音'} aria-pressed={voice.muted}><Icon name={voice.muted ? 'muted' : 'mic'} size={23} /><span>{voice.muted ? '取消静音' : '静音'}</span></button><button className="control-button end-call" disabled={voice.state === 'ending'} onClick={() => void controller.end()} aria-label="挂断通话"><Icon name="end" size={23} /><span>挂断</span></button></div>}
      {!hasConfig && !running && <button className="text-button complete-config" onClick={onConfigure}>先完成资料与模型配置<Icon name="arrow" size={13} /></button>}
      <div className="level-monitor" aria-label="音量反馈"><span>{voice.state === 'speaking' ? 'AI 音量' : '麦克风'}</span><div className="level-track"><i style={{ width: `${level * 100}%` }} /></div><span>{Math.round(level * 100)}%</span></div>
    </section><aside className="conversation-panel" aria-label="对话文字"><div className="conversation-title"><h2>对话文字</h2><span>{voice.transcript.length ? `${voice.transcript.length} 条` : '实时记录'}</span></div><div className="conversation-scroll" aria-live="polite">{voice.transcript.length ? voice.transcript.map((entry, index) => <div className={`transcript-entry ${entry.role}`} key={`${entry.timestamp}-${index}`}><div className="transcript-meta"><span>{entry.role === 'assistant' ? 'AI 助手' : '你'}</span><time>{new Date(entry.timestamp).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}</time></div><p>{entry.text}</p>{entry.interrupted && <span className="interrupted-label">已打断 · 播放未完成</span>}</div>) : <div className="transcript-empty"><span className="transcript-lines"><i /><i /><i /></span><p>通话开始后，对话文字会显示在这里。</p></div>}<div ref={transcriptEnd} /></div><div className="conversation-note">完整播放的文字会保留，打断前的生成内容用于下一轮背景</div></aside></div>
    <div className="call-goal"><span>{conversationMode(snapshot) === 'sales' ? '本次目标' : '本次模式'}</span><p>{goal}</p>{running && <small>使用通话开始时的配置</small>}</div>
  </div>;
}
