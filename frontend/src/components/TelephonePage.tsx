import { useEffect, useRef, useState, type FormEvent } from 'react';
import { api } from '../api';
import { conversationConfigured, conversationMode, conversationTitle, callSource, settingsUpdate, type InboundProviderStatus, type CallRecord, type Settings, type TelephonyProvider, type TelephonyProviderName, type TelephonySecretDrafts } from '../types';
import { duration } from './CallPage';
import { Icon } from './Icon';

const stateLabels: Record<string, string> = {
  connecting: '连接中', dialing: '拨号中', ringing: '振铃中', listening: '倾听中', recognizing: '识别中',
  thinking: '思考中', speaking: '说话中', ending: '正在结束', ended: '通话已结束',
};
const inboundLabels: Record<InboundProviderStatus['state'], string> = { disabled: '未启用', incomplete: '配置未完成', connecting: '连接中', listening: '正在监听', failed: '连接失败', pending: '等待配置生效', awaiting_callback: '等待来电回调' };
const endReasons: Record<string, string> = {
  user_hangup: '已手动挂断，文字记录保存在本机。', ai_hangup: 'AI 已告别并结束通话。',
  hangup: '通话已挂断。', busy: '被叫忙线。', no_answer: '被叫未接听。', rejected: '被叫拒接。',
  disconnected: '电话连接已结束。', provider_error: '电话接入失败。', transport_error: '电话媒体连接失败。',
};
const running = (call: CallRecord | null) => call !== null && ['connecting', 'active'].includes(call.status);
const errorText = (cause: unknown, fallback: string) => cause instanceof Error ? cause.message : fallback;

export function TelephonePage({ settings, onSave, onConfigure, onBrowserCall }: {
  settings: Settings;
  onSave: (value: Settings) => void;
  onConfigure: () => void;
  onBrowserCall: () => void;
}) {
  const [view, setView] = useState<'inbound' | 'outbound'>('inbound');
  const [inbound, setInbound] = useState<InboundProviderStatus[]>([]);
  const [inboundError, setInboundError] = useState('');
  const [arrayInputs, setArrayInputs] = useState<Partial<Record<TelephonyProviderName, Record<string, string>>>>({});
  const [providers, setProviders] = useState<TelephonyProvider[]>([]);
  const [catalogError, setCatalogError] = useState('');
  const [catalogRevision, setCatalogRevision] = useState(0);
  const [configProvider, setConfigProvider] = useState<TelephonyProviderName | ''>('');
  const [dialProvider, setDialProvider] = useState<TelephonyProviderName | ''>('');
  const [destination, setDestination] = useState('');
  const [draft, setDraft] = useState(settings);
  const [secrets, setSecrets] = useState<TelephonySecretDrafts>({});
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState('');
  const [saveMessage, setSaveMessage] = useState('');
  const [call, setCall] = useState<CallRecord | null>(null);
  const [checking, setChecking] = useState(true);
  const [busy, setBusy] = useState(false);
  const [callError, setCallError] = useState('');
  const [pollError, setPollError] = useState('');
  const [now, setNow] = useState(Date.now());
  const callRef = useRef<CallRecord | null>(null);
  const busyRef = useRef(false);
  const actionRevision = useRef(0);
  const mounted = useRef(true);
  const pollController = useRef<AbortController | null>(null);
  const transcriptEnd = useRef<HTMLDivElement>(null);
  const selectedConfig = providers.find(provider => provider.id === configProvider);
  const selectedDial = providers.find(provider => provider.id === dialProvider);
  const occupied = running(call);
  const telephone = call?.channel === 'telephone' ? call : null;
  const cloudConversation = (providers.find(provider => provider.id === telephone?.provider) ?? selectedDial)?.mode === 'cloud';
  const modelConfig = conversationConfigured(settings) && [ settings.llm.base_url, settings.llm.model,
    ...(selectedDial?.mode === 'media' ? [settings.asr.base_url, settings.asr.model, settings.tts.base_url, settings.tts.model, settings.tts.voice] : [])].every(value => value.trim());
  const dialEnabled = dialProvider !== '' && settings.telephony[dialProvider].enabled;
  const visibleCallError = callError || pollError || (telephone?.status === 'failed' ? endReasons[telephone.end_reason ?? ''] ?? telephone.end_reason ?? '电话接入失败。' : '');

  useEffect(() => {
    const controller = new AbortController();
    setCatalogError('');
    api.telephonyProviders(controller.signal).then(result => {
      if (controller.signal.aborted) return;
      setProviders(result);
      setConfigProvider(previous => previous || result[0]?.id || '');
    }).catch(cause => {
      if (!controller.signal.aborted) setCatalogError(errorText(cause, '电话 provider 加载失败。'));
    });
    return () => controller.abort();
  }, [catalogRevision]);

  useEffect(() => {
    let stopped = false;
    let timer: number | undefined;
    const controller = new AbortController();
    async function pollInbound() {
      try {
        const result = await api.inboundProviders(controller.signal);
        if (!stopped) { setInbound(result); setInboundError(''); }
      } catch (cause) {
        if (!stopped) setInboundError(errorText(cause, '接听状态读取失败。'));
      } finally {
        if (!stopped) timer = window.setTimeout(() => void pollInbound(), 1000);
      }
    }
    void pollInbound();
    return () => { stopped = true; controller.abort(); window.clearTimeout(timer); };
  }, [settings.telephony]);

  useEffect(() => {
    mounted.current = true;
    let stopped = false;
    let timer: number | undefined;
    async function poll() {
      const revision = actionRevision.current;
      const controller = new AbortController();
      pollController.current = controller;
      try {
        if (busyRef.current) return;
        const current = callRef.current;
        const updated = running(current) ? await api.call(current!.id, controller.signal) : await api.activeCall(controller.signal);
        if (stopped || controller.signal.aborted || revision !== actionRevision.current) return;
        if (updated) {
          callRef.current = updated;
          setCall(updated);
        }
        setChecking(false);
        setPollError('');
      } catch (cause) {
        if (!stopped && !controller.signal.aborted && revision === actionRevision.current) {
          setChecking(false);
          setPollError(errorText(cause, '通话状态读取失败。'));
        }
      } finally {
        if (!stopped) {
          setNow(Date.now());
          timer = window.setTimeout(() => void poll(), 1000);
        }
      }
    }
    void poll();
    return () => {
      stopped = true;
      mounted.current = false;
      pollController.current?.abort();
      window.clearTimeout(timer);
    };
  }, []);

  useEffect(() => {
    transcriptEnd.current?.scrollIntoView?.({ behavior: 'smooth', block: 'nearest' });
  }, [telephone?.transcript]);

  function updateField(name: string, value: string | number | boolean | string[] | null) {
    if (!configProvider) return;
    setDraft(current => ({ ...current, telephony: { ...current.telephony, [configProvider]: { ...current.telephony[configProvider], [name]: value } } }));
    setSaveMessage('');
  }

  function updateSecret(name: string, value: string | null) {
    if (!configProvider) return;
    setSecrets(current => ({ ...current, [configProvider]: { ...current[configProvider], [name]: value } }));
    setSaveMessage('');
  }

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (saving) return;
    setSaving(true);
    setSaveError('');
    try {
      const saved = await api.saveSettings(settingsUpdate(draft, { asr: '', llm: '', tts: '' }, secrets));
      // 保存完成时即使已切页，也须更新工作台的公开配置。
      onSave(saved);
      if (!mounted.current) return;
      setDraft(saved);
      setSecrets({});
      setArrayInputs({});
      setSaveMessage('电话配置已保存；已启用的接听线路将建立监听，外呼仍需手动发起。');
    } catch (cause) {
      if (mounted.current) setSaveError(errorText(cause, '电话配置保存失败。'));
    } finally {
      if (mounted.current) setSaving(false);
    }
  }

  async function start(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busyRef.current || occupied || !dialProvider || !dialEnabled || !modelConfig) return;
    busyRef.current = true;
    actionRevision.current += 1;
    pollController.current?.abort();
    setBusy(true);
    setCallError('');
    try {
      const result = await api.startTelephoneCall(dialProvider, destination.trim());
      if (mounted.current) {
        callRef.current = result.call;
        setCall(result.call);
      }
    } catch (cause) {
      if (mounted.current) setCallError(errorText(cause, '电话外呼失败。'));
    } finally {
      busyRef.current = false;
      if (mounted.current) setBusy(false);
    }
  }

  async function end() {
    if (!telephone || !occupied || busyRef.current) return;
    busyRef.current = true;
    actionRevision.current += 1;
    pollController.current?.abort();
    setBusy(true);
    setCallError('');
    try {
      const ended = await api.endCall(telephone.id);
      if (mounted.current) {
        callRef.current = ended;
        setCall(ended);
      }
    } catch (cause) {
      if (mounted.current) setCallError(errorText(cause, '挂断失败，请重试。'));
    } finally {
      busyRef.current = false;
      if (mounted.current) setBusy(false);
    }
  }

  return <div className="telephone-page">
    <div className="page-heading"><div><p className="eyebrow">电话接入</p><h1>{view === 'inbound' ? '接听产品咨询' : '拨打一次电话'}</h1><p className="subtle">{view === 'inbound' ? '启用已配置线路的接听，让 AI 根据产品资料回复来电。' : '选择已启用的线路，手动拨打手机号或固话。'}</p></div><button className="button secondary" onClick={onConfigure}><Icon name="settings" size={16} />对话配置</button></div>
    <div className="telephone-view-switch" role="group" aria-label="电话工作方式"><button type="button" className={`text-button ${view === 'inbound' ? 'selected' : ''}`} aria-pressed={view === 'inbound'} onClick={() => setView('inbound')}>来电接听</button><button type="button" className={`text-button ${view === 'outbound' ? 'selected' : ''}`} aria-pressed={view === 'outbound'} onClick={() => setView('outbound')}>手动外呼</button></div>
    {view === 'inbound' && <section className="inbound-monitor" aria-label="线路接听状态"><div className="conversation-title"><h2>接听状态</h2><span>同一时间接待一通电话</span></div>{inboundError && <p className="error-message" role="alert">{inboundError}</p>}{!inbound.length && !inboundError ? <p className="subtle" role="status">正在读取接听状态…</p> : inbound.map(item => <div className="inbound-status-row" key={item.provider}><span>{providers.find(provider => provider.id === item.provider)?.label ?? item.provider}</span><span className={`inbound-state ${item.state === 'failed' ? 'failed' : ''}`}>{inboundLabels[item.state]}</span>{item.error && <p role="alert">{item.error}</p>}</div>)}<p className="telephone-mode-note">云平台“等待来电回调”表示本机配置就绪，号码路由和平台连通性需在服务商控制台确认。</p></section>}
    <div className="call-layout telephone-layout">
      <section className="call-surface telephone-surface" aria-label="电话通话控制">
        <div className="call-meta"><span className="call-mode"><span className={`status-dot ${callError || pollError ? 'offline' : ''}`} />{telephone ? callSource(telephone) : '电话线路'}</span><time className="call-time">{telephone ? duration(telephone.started_at, telephone.ended_at, now) : '00:00'}</time></div>
        {telephone?.direction === 'inbound' && <p className="telephone-number-note">来电 {telephone.caller || '未提供号码'} · 接听 {telephone.destination || '未提供号码'}</p>}
        <div className="telephone-status" role="status"><Icon name={occupied ? 'phone' : 'end'} size={34} /><h2>{checking ? '读取通话状态' : occupied && !telephone ? '浏览器语音正在通话' : telephone ? telephone.status === 'failed' ? '电话接入失败' : stateLabels[telephone.state ?? (occupied ? 'connecting' : 'ended')] ?? telephone.state : view === 'inbound' ? '等待来电' : '准备拨号'}</h2><p>{occupied && !telephone ? '同一时间只能进行一通对话。' : telephone && occupied ? '切换页面后，电话会继续。' : telephone ? endReasons[telephone.end_reason ?? ''] ?? telephone.end_reason ?? '文字记录保存在本机。' : '所有电话 provider 默认关闭，请先填写并保存配置。'}</p></div>
        {occupied && !telephone ? <button className="button secondary" onClick={onBrowserCall}>返回语音通话<Icon name="arrow" size={16} /></button> : view === 'outbound' ? <form onSubmit={start} className="dial-form">
          <label className="field"><span>外呼 provider</span><select aria-label="外呼 provider" value={dialProvider} disabled={occupied || busy} onChange={event => setDialProvider(event.target.value as TelephonyProviderName | '')}><option value="">选择已启用的 provider</option>{providers.map(provider => <option key={provider.id} value={provider.id} disabled={!settings.telephony[provider.id].enabled}>{provider.label}{!settings.telephony[provider.id].enabled ? '（未启用）' : ''}</option>)}</select></label>
          <label className="field"><span>被叫号码</span><input aria-label="被叫号码" type="tel" inputMode="tel" autoComplete="off" required value={destination} placeholder="填写中国大陆手机号或带区号的固话" disabled={occupied || busy} onChange={event => setDestination(event.target.value)} /></label>
          {selectedDial && <p className="telephone-mode-note">{selectedDial.mode === 'media' ? '沿用已配置的 ASR、LLM 和 TTS。' : '平台托管语音，使用本机模型网关连接已配置 LLM。'}</p>}
          {occupied ? <button type="button" className="button danger" disabled={busy} onClick={() => void end()}><Icon name="end" size={18} />{busy ? '挂断中…' : '挂断电话'}</button> : <button type="submit" className="button primary" disabled={busy || checking || !dialEnabled || !destination.trim() || !modelConfig}><Icon name="phone" size={18} />{busy ? '发起中…' : '发起外呼'}</button>}
          {!modelConfig && selectedDial && !occupied && <button type="button" className="text-button complete-config" onClick={onConfigure}>先完成资料与所需模型配置<Icon name="arrow" size={13} /></button>}
        </form> : occupied ? <button type="button" className="button danger" disabled={busy} onClick={() => void end()}><Icon name="end" size={18} />{busy ? '挂断中…' : '挂断电话'}</button> : <p className="telephone-mode-note">在下方填写线路参数，启用 provider 和来电接听后保存。</p>}
        {visibleCallError && <p className="call-error" role="alert">{visibleCallError}</p>}
        <p className="telephone-device-note">声音通过电话线路传输；本页不使用麦克风。</p>
      </section>
      <aside className="conversation-panel" aria-label="电话对话文字"><div className="conversation-title"><h2>对话文字</h2><span>{telephone?.transcript.length ? `${telephone.transcript.length} 条` : '实时记录'}</span></div><div className="conversation-scroll" aria-live="polite">{telephone?.transcript.length ? telephone.transcript.map((entry, index) => <div className={`transcript-entry ${entry.role}`} key={`${entry.timestamp}-${index}`}><div className="transcript-meta"><span>{entry.role === 'assistant' ? 'AI 助手' : telephone?.direction === 'inbound' ? '来电用户' : '被叫用户'}</span><time>{new Date(entry.timestamp).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}</time></div><p>{entry.text}</p>{entry.interrupted && <span className="interrupted-label">已打断 · 播放未完成</span>}</div>) : <div className="transcript-empty"><span className="transcript-lines"><i /><i /><i /></span><p>{cloudConversation ? '云平台文字在回执或官方记录到达后显示。' : '接通后，对话文字会实时显示在这里。'}</p></div>}<div ref={transcriptEnd} /></div><div className="conversation-note">{cloudConversation ? '平台文字在回执或官方记录到达后更新' : '通话与记录由服务端管理'}</div></aside>
    </div>
    <div className="call-goal"><span>{conversationMode(telephone?.settings ?? settings) === 'sales' ? '本次目标' : '本次模式'}</span><p>{conversationTitle(telephone?.settings ?? settings)}</p>{occupied && <small>使用通话开始时的配置</small>}</div>
    <form className="settings-form telephone-config" onSubmit={save}>
      <section className="config-section" aria-labelledby="phone-config-heading"><div className="section-heading"><span className="section-index">01</span><div><h2 id="phone-config-heading">电话连接</h2><p>选择方案并填写连接参数，启用后保存。</p></div></div><div className="form-content">
        {catalogError ? <div className="history-alert" role="alert"><p>{catalogError}</p><button type="button" className="text-button" onClick={() => setCatalogRevision(value => value + 1)}>重新读取 provider</button></div> : !providers.length ? <p className="subtle" role="status">正在读取电话 provider…</p> : <>
          <label className="field"><span>配置 provider</span><select aria-label="配置 provider" value={configProvider} onChange={event => setConfigProvider(event.target.value as TelephonyProviderName)}>{providers.map(provider => <option key={provider.id} value={provider.id}>{provider.label}</option>)}</select></label>
          {selectedConfig && <fieldset className="provider-section telephone-provider"><legend><span>{selectedConfig.label}</span><small>{selectedConfig.mode === 'media' ? '本机三组模型' : '平台托管语音'}</small></legend><p className="subtle">{selectedConfig.description}</p>
            <label className="provider-enabled"><input type="checkbox" checked={draft.telephony[selectedConfig.id].enabled} onChange={event => updateField('enabled', event.target.checked)} aria-label={`启用 ${selectedConfig.label}`} /><span>启用此 provider</span><small>保存配置后生效</small></label>
            <div className="phone-fields">{selectedConfig.fields.map(field => field.type === 'secret' ? <div className="secret-line" key={field.name}><label className="field secret-field"><span>{field.label}<em>{secrets[selectedConfig.id]?.[field.name] === null ? '保存后清除' : draft.telephony[selectedConfig.id][`${field.name}_set`] ? '已设置' : '未设置'}</em></span><input type="password" autoComplete="off" aria-label={`${selectedConfig.label} ${field.label}`} value={secrets[selectedConfig.id]?.[field.name] ?? ''} onChange={event => updateSecret(field.name, event.target.value)} placeholder={draft.telephony[selectedConfig.id][`${field.name}_set`] ? '留空保留现有值' : '填写连接凭据'} /></label>{(draft.telephony[selectedConfig.id][`${field.name}_set`] || secrets[selectedConfig.id]?.[field.name]) && <button type="button" className="text-button clear-key" aria-label={`清除 ${selectedConfig.label} ${field.label}`} onClick={() => updateSecret(field.name, null)}>清除</button>}{secrets[selectedConfig.id]?.[field.name] === null && <button type="button" className="text-button clear-key" onClick={() => updateSecret(field.name, '')}>撤销清除</button>}</div> : field.type === 'boolean' ? <label className="provider-enabled" key={field.name}><input type="checkbox" aria-label={`${selectedConfig.label} ${field.label}`} checked={Boolean(draft.telephony[selectedConfig.id][field.name])} onChange={event => updateField(field.name, event.target.checked)} /><span>{field.label}</span></label> : field.type === 'array' ? <label className="field" key={field.name}><span>{field.label}</span><textarea rows={3} aria-label={`${selectedConfig.label} ${field.label}`} value={arrayInputs[selectedConfig.id]?.[field.name] ?? (draft.telephony[selectedConfig.id][field.name] as string[] ?? []).join('\n')} onChange={event => { const text = event.target.value; setArrayInputs(current => ({ ...current, [selectedConfig.id]: { ...current[selectedConfig.id], [field.name]: text } })); updateField(field.name, text.split(/[,，\n]/).map(value => value.trim()).filter(Boolean)); }} /><small>{selectedConfig.mode === 'cloud' ? '填写允许接听的已开通号码，用逗号或换行分隔。' : '可留空，接听转入专属路由的号码；多个号码用逗号或换行分隔。'}</small></label> : <label className="field" key={field.name}><span>{field.label}</span><input aria-label={`${selectedConfig.label} ${field.label}`} type={field.type === 'string' ? 'text' : 'number'} step={field.type === 'integer' ? 1 : 'any'} min={field.minimum ?? undefined} max={field.maximum ?? undefined} value={String(draft.telephony[selectedConfig.id][field.name] ?? '')} onChange={event => updateField(field.name, field.type === 'string' ? event.target.value : event.target.value === '' ? field.nullable ? null : '' : Number(event.target.value))} /></label>)}</div>
          </fieldset>}
          <label className="field"><span>云平台可访问的本机服务地址 <em>云托管语音使用</em></span><input type="url" aria-label="云平台可访问的本机服务地址" value={draft.telephony.public_base_url} onChange={event => { setDraft(current => ({ ...current, telephony: { ...current.telephony, public_base_url: event.target.value } })); setSaveMessage(''); }} placeholder="填写已部署的 HTTPS 服务地址" /><small>平台通过此地址访问模型网关和通话回执；本机媒体 provider 可留空。</small></label>
          <p className="privacy-note">启用 provider 和来电接听后保存，本机媒体线路会在后台连接并监听；关闭接听不会中断已开始的通话。</p>
          <p className="privacy-note">连接凭据仅保存在服务端，保存后不再显示。</p>
          <p className="privacy-note">PBX 或云账号及电话线路需先开通；保存配置不会注册或开通线路。</p>
        </>}
      </div></section>
      <div className="save-bar"><div aria-live="polite">{saveError ? <p className="error-message" role="alert">{saveError}</p> : saveMessage ? <p className="success-message"><Icon name="check" size={16} />{saveMessage}</p> : <p className="subtle">启用接听后保存会建立线路监听；外呼需手动发起</p>}</div><button className="button primary" type="submit" disabled={saving || !providers.length}>{saving ? '保存中…' : '保存电话配置'}<Icon name="arrow" size={17} /></button></div>
    </form>
  </div>;
}
