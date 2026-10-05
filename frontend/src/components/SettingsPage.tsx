import { useState, type FormEvent } from 'react';
import { api } from '../api';
import { activeConversation, conversationMode, settingsUpdate, type ConversationMode, type ProviderName, type SecretDrafts, type Settings } from '../types';
import { Icon } from './Icon';

const providers: { name: ProviderName; title: string; description: string; label: string }[] = [
  { name: 'asr', title: '语音识别', description: '将用户语音转换成文字', label: 'ASR' },
  { name: 'llm', title: '文本模型', description: '理解问题，组织回答', label: 'LLM' },
  { name: 'tts', title: '语音合成', description: '将回答转换成声音', label: 'TTS' },
];

export function SettingsPage({ settings, onSave }: { settings: Settings; onSave: (value: Settings) => void }) {
  const [draft, setDraft] = useState(settings);
  const [secrets, setSecrets] = useState<SecretDrafts>({ asr: '', llm: '', tts: '' });
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [dirty, setDirty] = useState(false);

  const mode = conversationMode(draft);
  const conversation = activeConversation(draft);

  function updateMode(value: ConversationMode) {
    setDraft(current => ({ ...current, conversation: { mode: value } }));
    setDirty(true);
    setMessage('');
  }

  function updateConversation(key: keyof Settings['sales'], value: string) {
    const section = mode === 'sales' ? 'sales' : 'consultation';
    setDraft(current => ({ ...current, [section]: { ...current[section], [key]: value } }));
    setDirty(true);
    setMessage('');
  }

  function updateProvider(name: ProviderName, key: string, value: string | number | null) {
    setDraft(current => ({ ...current, [name]: { ...current[name], [key]: value } }));
    setDirty(true);
    setMessage('');
  }

  function updateSecret(name: ProviderName, value: string | null) {
    setSecrets(current => ({ ...current, [name]: value }));
    setDirty(true);
    setMessage('');
  }

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSaving(true);
    setError('');
    try {
      const saved = await api.saveSettings(settingsUpdate(draft, secrets));
      setDraft(saved);
      setSecrets({ asr: '', llm: '', tts: '' });
      setDirty(false);
      setMessage('已保存，下次通话将使用新配置。');
      onSave(saved);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '保存失败，请重试。');
    } finally {
      setSaving(false);
    }
  }

  return <form className="settings-form" onSubmit={save}>
    <div className="page-heading"><div><p className="eyebrow">对话设置</p><h1>配置你的下一通对话</h1><p className="subtle">选择咨询或销售，配置资料、提示词和模型。</p></div><button className="button primary save-top" type="submit" disabled={saving}><Icon name={dirty ? 'arrow' : 'check'} size={17} />{saving ? '保存中…' : '保存配置'}</button></div>
    <section className="config-section" aria-labelledby="sales-heading">
      <div className="section-heading"><span className="section-index">01</span><div><h2 id="sales-heading">对话模式</h2><p>告诉 AI 要做什么，以及可以依据哪些信息。</p></div></div>
      <div className="form-content">
        <label className="field"><span>对话模式</span><select aria-label="对话模式" value={mode} onChange={event => updateMode(event.target.value as ConversationMode)}><option value="consultation">产品咨询</option><option value="sales">销售推介</option></select><small>两种模式的资料和提示词分别保存，切换不会删除原配置。</small></label>
        {mode === 'sales' && <label className="field"><span>销售目标</span><textarea rows={2} value={draft.sales.goal} onChange={event => updateConversation('goal', event.target.value)} placeholder="例如：介绍产品，邀请用户订阅" /></label>}
        <label className="field"><span>产品资料</span><textarea rows={5} value={conversation.product_info} onChange={event => updateConversation('product_info', event.target.value)} placeholder={mode === 'sales' ? '填写产品功能、价格和购买方式…' : '填写产品功能、使用步骤、费用、服务范围和常见问题…'} /><small>AI 将依据这些资料回答，请提供准确的信息。</small></label>
        <div className="field-pair"><label className="field"><span>{mode === 'sales' ? '话术要求' : '咨询提示词'} <em>可选</em></span><textarea rows={3} value={conversation.instructions} onChange={event => updateConversation('instructions', event.target.value)} placeholder={mode === 'sales' ? '例如：介绍资料支持的价值，每次回答简短' : '例如：先直接回答功能问题；资料不足时说明无法确认'} /></label><label className="field"><span>{mode === 'sales' ? '固定开场白' : '固定欢迎语'} <em>可选</em></span><textarea rows={3} value={conversation.opening} onChange={event => updateConversation('opening', event.target.value)} placeholder={mode === 'sales' ? '留空时，AI 会根据目标生成开场白' : '留空时，AI 接通后先说一句简短欢迎语'} /></label></div>
      </div>
    </section>
    <section className="config-section" aria-labelledby="models-heading">
      <div className="section-heading"><span className="section-index">02</span><div><h2 id="models-heading">模型连接</h2><p>三个模型可以使用不同的服务商。</p></div></div>
      <div className="form-content">
        {providers.map(({ name, title, description, label }) => <fieldset className="provider-section" key={name}><legend><span className="provider-label">{label}</span><span>{title}</span><small>{description}</small></legend>
          {name !== 'llm' && <label className="field protocol-field"><span>接口协议</span><select aria-label={`${title}接口协议`} value={draft[name].protocol ?? 'openai'} onChange={event => updateProvider(name, 'protocol', event.target.value)}><option value="openai">OpenAI 兼容</option><option value="mimo">小米 MiMo</option></select></label>}
          <div className="field-pair"><label className="field"><span>API 地址</span><input type="url" aria-label={`${title} API 地址`} value={draft[name].base_url} onChange={event => updateProvider(name, 'base_url', event.target.value)} placeholder="服务商提供的兼容 API 基础地址" /></label><label className="field"><span>模型名称</span><input aria-label={`${title}模型名称`} value={draft[name].model} onChange={event => updateProvider(name, 'model', event.target.value)} placeholder="填写服务商提供的模型名称" /></label></div>
          <div className="secret-line"><label className="field secret-field"><span>API 密钥 <em>{secrets[name] === null ? '保存后清除' : draft[name].api_key_set ? '已设置' : '未设置'}</em></span><input type="password" autoComplete="off" aria-label={`${title} API 密钥`} value={secrets[name] ?? ''} onChange={event => updateSecret(name, event.target.value)} placeholder={draft[name].api_key_set ? '留空保留现有密钥' : '按服务商要求填写'} /></label>{(draft[name].api_key_set || secrets[name]) && <button type="button" className="text-button clear-key" onClick={() => updateSecret(name, null)} aria-label={`清除${title}密钥`}>清除密钥</button>}{secrets[name] === null && <button type="button" className="text-button clear-key" onClick={() => updateSecret(name, '')}>撤销清除</button>}</div>
          {name === 'asr' && <label className="field narrow-field"><span>识别语言</span><input value={draft.asr.language} onChange={event => updateProvider('asr', 'language', event.target.value)} aria-label="识别语言" /></label>}
          {name === 'llm' && <div className="field-pair"><label className="field"><span>思考模式</span><select aria-label="思考模式" value={draft.llm.thinking ?? ''} onChange={event => updateProvider('llm', 'thinking', event.target.value || null)}><option value="">服务默认</option><option value="enabled">启用思考</option><option value="disabled">关闭思考</option></select><small>服务默认不向模型发送思考开关。</small></label><label className="field"><span>推理强度</span><select aria-label="推理强度" value={draft.llm.reasoning_effort ?? ''} onChange={event => updateProvider('llm', 'reasoning_effort', event.target.value || null)}><option value="">服务默认</option><option value="none">无</option><option value="low">低</option><option value="medium">中</option><option value="high">高</option><option value="max">最高</option></select><small>仅在模型支持时设置。</small></label></div>}
          {name === 'tts' && <div className="field-pair"><label className="field"><span>音色名称</span><input value={draft.tts.voice} onChange={event => updateProvider('tts', 'voice', event.target.value)} placeholder="服务商提供的音色名称" /></label><label className="field"><span>PCM 采样率 <em>Hz</em></span><input type="number" min={8000} max={96000} value={draft.tts.sample_rate} onChange={event => updateProvider('tts', 'sample_rate', Number(event.target.value))} /><small>使用服务商实际输出的采样率。</small></label></div>}
        </fieldset>)}
        <p className="privacy-note">密钥仅保存在本机服务端，保存后不再显示。</p>
      </div>
    </section>
    <div className="save-bar"><div aria-live="polite">{error ? <p className="error-message" role="alert">{error}</p> : message ? <p className="success-message"><Icon name="check" size={16} />{message}</p> : <p className="subtle">{dirty ? '有尚未保存的更改' : '通话使用开始时的配置'}</p>}</div><button className="button primary" type="submit" disabled={saving}>{saving ? '保存中…' : '保存配置'}<Icon name="arrow" size={17} /></button></div>
  </form>;
}
