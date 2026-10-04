import { useEffect, useState } from 'react';
import { api } from './api';
import { Icon, type IconName } from './components/Icon';
import { SettingsPage } from './components/SettingsPage';
import type { Settings } from './types';

type Page = 'call' | 'settings' | 'history';
const navigation: { id: Page; title: string; icon: IconName }[] = [{ id: 'call', title: '语音通话', icon: 'phone' }, { id: 'settings', title: '对话配置', icon: 'settings' }, { id: 'history', title: '通话记录', icon: 'history' }];

export function App() {
  const [page, setPage] = useState<Page>('settings');
  const [settings, setSettings] = useState<Settings | null>(null);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setError('');
    api.settings(controller.signal).then(setSettings).catch(cause => {
      if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : '配置加载失败。');
    });
    return () => controller.abort();
  }, [attempt]);

  return <div className="app-shell"><aside className="sidebar"><a className="brand" href="#" onClick={event => event.preventDefault()} aria-label="llmautotel 语音工作台"><span className="brand-symbol"><i /><i /><i /><i /></span><span>llmautotel<small>语音工作台</small></span></a><p className="nav-caption">工作空间</p><nav aria-label="主导航">{navigation.map(item => <button key={item.id} onClick={() => setPage(item.id)} className={`nav-item ${page === item.id ? 'active' : ''}`} aria-current={page === item.id ? 'page' : undefined}><Icon name={item.icon} /><span>{item.title}</span>{page === item.id && <span className="nav-dot" />}</button>)}</nav><div className="sidebar-footer"><span className="status-dot" /><span>本机工作空间<small>文字记录保存在本机</small></span></div></aside><div className="workspace"><header className="topbar"><span>{navigation.find(item => item.id === page)?.title}</span><span className="workspace-status"><span className={`status-dot ${error ? 'offline' : ''}`} />{error ? '服务未连接' : settings ? '服务已连接' : '连接服务中'}</span></header><main className="main-content">{error ? <div className="empty-state"><Icon name="settings" size={32} /><h1>暂时无法读取配置</h1><p role="alert">{error}</p><button className="button primary" onClick={() => setAttempt(value => value + 1)}>重新连接</button></div> : !settings ? <div className="empty-state" role="status"><span className="loading-ring" /><p>正在读取配置…</p></div> : page === 'settings' ? <SettingsPage settings={settings} onSave={setSettings} /> : <div className="empty-state"><div className="empty-symbol"><Icon name={page === 'call' ? 'phone' : 'history'} size={30} /></div><p className="eyebrow">{page === 'call' ? '语音通话' : '通话记录'}</p><h1>{page === 'call' ? '准备好你的对话目标' : '对话将从这里延续'}</h1><p>{page === 'call' ? '先完成目标与模型配置，语音通话模块将在下一步接入。' : '历史记录模块将在语音通话完成后接入。'}</p><button className="button secondary" onClick={() => setPage('settings')}>前往对话配置<Icon name="arrow" size={17} /></button></div>}</main><footer className="workspace-footer"><span>llmautotel</span><span>目标明确 · 对话自然</span></footer></div></div>;
}
