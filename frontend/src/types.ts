export type ProviderName = 'asr' | 'llm' | 'tts';
export type AudioProtocol = 'openai' | 'mimo';
export type ThinkingMode = 'enabled' | 'disabled';
export type ReasoningEffort = 'none' | 'low' | 'medium' | 'high' | 'max';
export type TelephonyProviderName = 'asterisk' | 'freeswitch' | 'aliyun' | 'tencent';
export type ConversationMode = 'consultation' | 'sales';
export interface ConversationConfig { product_info: string; instructions: string; opening: string }
export interface TelephonyProviderSettings {
  enabled: boolean;
  [field: string]: string | number | boolean | string[] | null;
}
export interface TelephonySettings extends Record<TelephonyProviderName, TelephonyProviderSettings> {
  public_base_url: string;
}
export type TelephonySecretDrafts = Partial<Record<TelephonyProviderName, Record<string, string | null>>>;
export interface TelephonyProvider {
  id: TelephonyProviderName;
  label: string;
  mode: 'media' | 'cloud';
  description: string;
  enabled: boolean;
  fields: { name: string; label: string; type: 'secret' | 'integer' | 'number' | 'string' | 'boolean' | 'array'; nullable: boolean; minimum: number | null; maximum: number | null }[];
}

export interface InboundProviderStatus {
  provider: TelephonyProviderName;
  state: 'disabled' | 'incomplete' | 'connecting' | 'listening' | 'failed' | 'pending' | 'awaiting_callback';
  error: string | null;
}

export interface ProviderSettings {
  base_url: string;
  model: string;
  timeout_seconds: number;
  api_key_set: boolean;
}

export interface Settings {
  conversation: { mode: ConversationMode };
  consultation: ConversationConfig;
  sales: { goal: string; product_info: string; instructions: string; opening: string };
  asr: ProviderSettings & { protocol: AudioProtocol; language: string };
  llm: ProviderSettings & { thinking: ThinkingMode | null; reasoning_effort: ReasoningEffort | null };
  tts: ProviderSettings & { protocol: AudioProtocol; voice: string; sample_rate: number };
  voice: { vad_start_seconds: number; vad_stop_seconds: number; vad_confidence: number };
  telephony: TelephonySettings;
}

export type SettingsUpdate = Omit<Settings, ProviderName> & {
  asr: Omit<Settings['asr'], 'api_key_set'> & { api_key?: string | null };
  llm: Omit<Settings['llm'], 'api_key_set'> & { api_key?: string | null };
  tts: Omit<Settings['tts'], 'api_key_set'> & { api_key?: string | null };
};

export type SecretDrafts = Record<ProviderName, string | null>;

export interface TranscriptEntry {
  role: 'user' | 'assistant';
  text: string;
  timestamp: string;
  interrupted: boolean;
}

export interface CallRecord {
  id: string;
  started_at: string;
  ended_at: string | null;
  status: string;
  end_reason: string | null;
  settings: Settings;
  transcript: TranscriptEntry[];
  channel?: 'browser' | 'telephone';
  provider?: TelephonyProviderName | null;
  destination?: string | null;
  direction?: 'inbound' | 'outbound' | null;
  caller?: string | null;
  state?: string;
}

export interface CallSummary extends Omit<CallRecord, 'settings' | 'transcript'> {
  goal: string;
  conversation_mode?: ConversationMode;
  message_count: number;
}

export interface CallConnection {
  webrtcRequestParams: { endpoint: string };
  iceConfig: { iceServers: RTCIceServer[] };
}

export interface StartCallResult {
  call: CallRecord;
  connection: CallConnection;
}

export type EndReason = 'user_hangup' | 'connection_lost';

export function settingsUpdate(settings: Settings, secrets: SecretDrafts, phoneSecrets: TelephonySecretDrafts = {}): SettingsUpdate {
  const provider = <T extends ProviderSettings>(value: T, secret: string | null) => {
    const { api_key_set: _isSet, ...publicValue } = value;
    return { ...publicValue, ...(secret === null ? { api_key: null } : secret ? { api_key: secret } : {}) };
  };
  return {
    conversation: settings.conversation,
    consultation: settings.consultation,
    sales: settings.sales,
    asr: provider(settings.asr, secrets.asr),
    llm: provider(settings.llm, secrets.llm),
    tts: provider(settings.tts, secrets.tts),
    voice: settings.voice,
    telephony: telephonyUpdate(settings.telephony, phoneSecrets),
  };
}

export function telephonyUpdate(settings: TelephonySettings, secrets: TelephonySecretDrafts): TelephonySettings {
  const providers = Object.fromEntries(Object.entries(settings).filter(([name]) => name !== 'public_base_url').map(([name, value]) => {
    const publicValue = Object.fromEntries(Object.entries(value as TelephonyProviderSettings).filter(([key]) => !key.endsWith('_set')));
    const changes = Object.fromEntries(Object.entries(secrets[name as TelephonyProviderName] ?? {}).filter(([, value]) => value !== ''));
    return [name, { ...publicValue, ...changes }];
  }));
  return { public_base_url: settings.public_base_url, ...providers } as TelephonySettings;
}

export const phoneProviderLabels: Record<TelephonyProviderName, string> = {
  asterisk: 'Asterisk', freeswitch: 'FreeSWITCH', aliyun: '阿里云 AICCS', tencent: '腾讯云 TCCC',
};

export function callSource(call: Pick<CallRecord, 'channel' | 'provider' | 'destination' | 'direction' | 'caller'>): string {
  if (call.channel !== 'telephone') return '浏览器语音';
  return [call.direction === 'inbound' ? '电话来电' : '电话外呼', call.provider ? phoneProviderLabels[call.provider] ?? call.provider : '', call.direction === 'inbound' ? call.caller ?? '' : call.destination ?? ''].filter(Boolean).join(' · ');
}

export function conversationMode(settings: Settings): ConversationMode {
  return settings.conversation?.mode ?? 'sales';
}

export function activeConversation(settings: Settings): ConversationConfig {
  return conversationMode(settings) === 'sales' ? settings.sales : settings.consultation;
}

export function conversationTitle(settings: Settings): string {
  return conversationMode(settings) === 'sales' ? settings.sales.goal || '尚未配置目标' : '产品咨询';
}

export function conversationConfigured(settings: Settings): boolean {
  return Boolean(activeConversation(settings).product_info.trim()) && (conversationMode(settings) !== 'sales' || Boolean(settings.sales.goal.trim()));
}
