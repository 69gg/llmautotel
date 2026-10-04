import { useEffect, useState, useSyncExternalStore } from 'react';
import { api } from './api';
import { Icon, type IconName } from './components/Icon';
import { SettingsPage } from './components/SettingsPage';
import { CallPage } from './components/CallPage';
import { HistoryPage } from './components/HistoryPage';
import { TelephonePage } from './components/TelephonePage';
import type { Settings } from './types';
import { VoiceCallController } from './voiceCall';

type Page = 'call' | 'telephone' | 'settings' | 'history';
const navigation: { id: Page; title: string; icon: IconName }[] = [{ id: 'call', title: '语音通话', icon: 'phone' }, { id: 'telephone', title: '电话外呼', icon: 'phone' }, { id: 'settings', title: '对话配置', icon: 'settings' }, { id: 'history', title: '通话记录', icon: 'history' }];

export function App() {
  const [page, setPage] = useState<Page>('call');
  const [settings, setSettings] = useState<Settings | null>(null);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  const [controller] = useState(() => new VoiceCallController());
  const voice = useSyncExternalStore(controller.subscribe, controller.getSnapshot);

  useEffect(() => () => controller.dispose(), [controller]);

  useEffect(() => {
    const controller = new AbortController();
    setError('');
    api.settings(controller.signal).then(setSettings).catch(cause => {
      if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : '配置加载失败。');
    });
    return () => controller.abort();
  }, [attempt]);

  return <div className="app-shell"><audio ref={controller.attachAudio} autoPlay /><aside className="sidebar"><a className="brand" href="#" onClick={event => event.preventDefault()} aria-label="llmautotel 语音工作台"><span className="brand-symbol"><i /><i /><i /><i /></span><span>llmautotel<small>语音工作台</small></span></a><p className="nav-caption">工作空间</p><nav aria-label="主导航">{navigation.map(item => <button key={item.id} onClick={() => setPage(item.id)} className={`nav-item ${page === item.id ? 'active' : ''}`} aria-label={item.title} aria-current={page === item.id ? 'page' : undefined}><Icon name={item.icon} /><span>{item.title}</span>{page === item.id && <span className="nav-dot" />}</button>)}</nav><div className="sidebar-footer"><span className="status-dot" /><span>本机工作空间<small>文字记录保存在本机</small></span></div></aside><div className="workspace"><header className="topbar"><span>{navigation.find(item => item.id === page)?.title}</span><span className="workspace-status"><span className={`status-dot ${error ? 'offline' : ''}`} />{error ? '服务未连接' : settings ? '服务已连接' : '连接服务中'}</span></header><main className="main-content">{error ? <div className="empty-state"><Icon name="settings" size={32} /><h1>暂时无法读取配置</h1><p role="alert">{error}</p><button className="button primary" onClick={() => setAttempt(value => value + 1)}>重新连接</button></div> : !settings ? <div className="empty-state" role="status"><span className="loading-ring" /><p>正在读取配置…</p></div> : page === 'settings' ? <SettingsPage settings={settings} onSave={setSettings} /> : page === 'call' ? <CallPage settings={settings} voice={voice} controller={controller} onConfigure={() => setPage('settings')} /> : page === 'telephone' ? <TelephonePage settings={settings} onSave={setSettings} onConfigure={() => setPage('settings')} onBrowserCall={() => setPage('call')} /> : <HistoryPage />}</main><footer className="workspace-footer"><span>llmautotel</span><span>目标明确 · 对话自然</span></footer></div></div>;
}
