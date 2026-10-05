import { useEffect, useRef, useState } from 'react';
import { api } from '../api';
import { activeConversation, conversationMode, conversationTitle, callSource, type CallRecord, type CallSummary } from '../types';
import { duration } from './CallPage';
import { Icon } from './Icon';

const reasons: Record<string, string> = { user_hangup: '用户挂断', ai_hangup: 'AI 确认结束', connection_lost: '连接中断', disconnected: '连接中断', connection_timeout: '连接超时', server_shutdown: '服务关闭', server_restarted: '服务重新启动', model_error: '模型请求失败', internal_error: '服务处理异常', busy: '被叫忙线', no_answer: '无人接听', rejected: '被叫拒接', call_failed: '外呼失败', provider_error: '电话接入失败', media_error: '电话音频接入失败' };
const statuses: Record<string, string> = { connecting: '连接中', active: '通话中', ended: '已结束', failed: '异常结束' };
const isActive = (call: CallSummary | CallRecord) => ['connecting', 'active'].includes(call.status);
const time = (value: string) => new Date(value).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
const errorText = (cause: unknown) => cause instanceof Error ? cause.message : '记录加载失败，请重试。';

function DeleteDialog({ call, pending, onCancel, onDelete }: { call: CallRecord; pending: boolean; onCancel: () => void; onDelete: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const element = dialog.current;
    element?.showModal();
    return () => { element?.close(); };
  }, []);
  return <dialog className="delete-dialog" ref={dialog} onCancel={event => { event.preventDefault(); if (!pending) onCancel(); }} aria-labelledby="delete-title" aria-describedby="delete-description"><p className="eyebrow">本机记录</p><h2 id="delete-title">删除这通记录？</h2><p id="delete-description">{time(call.started_at)} 的对话文字与配置快照将从本机删除。</p><div className="dialog-actions"><button className="button secondary" autoFocus disabled={pending} onClick={onCancel}>取消</button><button className="button danger" disabled={pending} onClick={onDelete}>{pending ? '删除中…' : '确认删除'}</button></div></dialog>;
}

export function HistoryPage() {
  const [calls, setCalls] = useState<CallSummary[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<CallRecord | null>(null);
  const [listError, setListError] = useState('');
  const [detailError, setDetailError] = useState('');
  const [deleteError, setDeleteError] = useState('');
  const [refreshing, setRefreshing] = useState(false);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [revision, setRevision] = useState(0);
  const [confirmation, setConfirmation] = useState<CallRecord | null>(null);
  const [deleting, setDeleting] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    setListError('');
    setRefreshing(true);
    api.calls(controller.signal).then(records => {
      if (controller.signal.aborted) return;
      setCalls(records);
      setSelected(previous => records.some(record => record.id === previous) ? previous : records[0]?.id ?? null);
    }).catch(cause => {
      if (!controller.signal.aborted) setListError(errorText(cause));
    }).finally(() => {
      if (!controller.signal.aborted) setRefreshing(false);
    });
    return () => controller.abort();
  }, [revision]);

  useEffect(() => {
    const controller = new AbortController();
    setDetail(null);
    setDetailError('');
    setDeleteError('');
    if (!selected) { setLoadingDetail(false); return () => controller.abort(); }
    setLoadingDetail(true);
    api.call(selected, controller.signal).then(record => {
      if (!controller.signal.aborted) setDetail(record);
    }).catch(cause => {
      if (!controller.signal.aborted) setDetailError(errorText(cause));
    }).finally(() => {
      if (!controller.signal.aborted) setLoadingDetail(false);
    });
    return () => controller.abort();
  }, [selected, revision]);

  function refresh() { setRevision(value => value + 1); }

  async function remove() {
    if (!confirmation || deleting) return;
    const id = confirmation.id;
    setDeleting(true);
    setDeleteError('');
    try {
      await api.deleteCall(id);
      setConfirmation(null);
      setCalls(previous => previous?.filter(call => call.id !== id) ?? []);
      setSelected(previous => previous === id ? null : previous);
      refresh();
    } catch (cause) {
      setDeleteError(errorText(cause));
      setConfirmation(null);
    } finally {
      setDeleting(false);
    }
  }

  return <div className="history-page">
    <div className="page-heading"><div><p className="eyebrow">文字历史</p><h1>回看你的对话</h1><p className="subtle">查看本机保存的来电、外呼与对话配置快照。</p></div><button className="button secondary" disabled={refreshing || deleting} onClick={refresh}><Icon name="history" size={16} />{refreshing ? '刷新中…' : '刷新记录'}</button></div>
    {listError && <div className="history-alert" role="alert"><p>{listError}</p><button className="text-button" onClick={refresh}>重试</button></div>}
    {calls === null ? !listError && <div className="history-loading" role="status"><span className="loading-ring" /><p>正在读取通话记录…</p></div> : calls.length === 0 ? <div className="empty-state history-empty"><div className="empty-symbol"><Icon name="history" size={30} /></div><h2>还没有通话记录</h2><p>开始一次语音通话后，文字记录会保存在这里。</p></div> : <div className="history-layout"><section className="history-list" aria-label="通话列表"><div className="history-list-heading"><span>全部通话</span><span>{calls.length} 通</span></div>{calls.map(call => <button className={`history-row ${selected === call.id ? 'selected' : ''}`} key={call.id} onClick={() => setSelected(call.id)} aria-pressed={selected === call.id} aria-label={`查看 ${time(call.started_at)} ${call.goal || '未填写目标'} 的通话`}><div className="history-row-meta"><time>{time(call.started_at)}</time><span className={`record-status ${isActive(call) ? 'active' : call.status === 'failed' ? 'failed' : ''}`}>{statuses[call.status] ?? call.status}</span></div><p>{call.goal || '未填写目标'}</p><div className="history-row-bottom"><span>{call.message_count} 条对话 · {call.channel === 'telephone' ? call.direction === 'inbound' ? '电话来电' : '电话外呼' : '浏览器语音'}</span><span>{call.ended_at ? duration(call.started_at, call.ended_at) : '进行中'}<Icon name="arrow" size={13} /></span></div></button>)}</section><section className="history-detail" aria-label="记录详情">{loadingDetail ? <div className="history-loading" role="status"><span className="loading-ring" /><p>正在读取对话文字…</p></div> : detailError ? <div className="history-detail-error"><p className="error-message" role="alert">{detailError}</p><button className="button secondary" onClick={refresh}>重新读取</button></div> : detail ? <>
      <div className="record-heading"><div><p className="eyebrow">{new Date(detail.started_at).getFullYear()} · {time(detail.started_at)}</p><h2>对话详情</h2></div><button className="text-button delete-record" disabled={isActive(detail) || deleting} onClick={() => setConfirmation(detail)} title={isActive(detail) ? '通话进行中，结束后可以删除' : undefined}>删除记录</button></div>
      {deleteError && <div className="history-alert" role="alert"><p>{deleteError}</p><button className="text-button" onClick={refresh}>刷新状态</button></div>}
      <div className="record-facts"><div><span>通话时长</span><p>{detail.ended_at ? duration(detail.started_at, detail.ended_at) : '进行中'}</p></div><div><span>结束原因</span><p>{detail.end_reason ? reasons[detail.end_reason] ?? detail.end_reason : '尚未结束'}</p></div></div>
      <div className="record-source"><span>通话来源</span><p>{callSource(detail)}</p></div><div className="record-goal"><span>{conversationMode(detail.settings) === 'sales' ? '本次目标' : '本次模式'}</span><p>{conversationTitle(detail.settings)}</p></div>
      <div className="record-transcript">{detail.transcript.length ? detail.transcript.map((entry, index) => <div className={`transcript-entry ${entry.role}`} key={`${entry.timestamp}-${index}`}><div className="transcript-meta"><span>{entry.role === 'assistant' ? 'AI 助手' : '你'}</span><time>{new Date(entry.timestamp).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })}</time></div><p>{entry.text}</p>{entry.interrupted && <span className="interrupted-label">已打断 · 播放未完成</span>}</div>) : <p className="record-no-text">这通对话没有产生文字记录。</p>}</div>
      <details className="snapshot-details"><summary>查看本次配置快照<Icon name="arrow" size={13} /></summary><dl><dt>对话模式</dt><dd>{conversationMode(detail.settings) === 'sales' ? '销售推介' : '产品咨询'}</dd><dt>产品资料</dt><dd>{activeConversation(detail.settings).product_info || '未填写'}</dd><dt>话术要求</dt><dd>{activeConversation(detail.settings).instructions || '未填写'}</dd><dt>固定开场白</dt><dd>{activeConversation(detail.settings).opening || '自动生成'}</dd>{(['asr', 'llm', 'tts'] as const).map(provider => <div className="snapshot-provider" key={provider}><dt>{provider.toUpperCase()} 模型</dt><dd>{detail.settings[provider].model || '未填写'}<small>{detail.settings[provider].base_url || '未填写 API 地址'}</small></dd></div>)}<dt>音色与采样率</dt><dd>{detail.settings.tts.voice || '未填写音色'} · {detail.settings.tts.sample_rate} Hz</dd></dl></details>
    </> : <div className="history-detail-error"><p className="subtle">选择一通记录，查看对话文字。</p></div>}</section></div>}
    {confirmation && <DeleteDialog call={confirmation} pending={deleting} onCancel={() => setConfirmation(null)} onDelete={() => void remove()} />}
  </div>;
}
