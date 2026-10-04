export type ProviderName = 'asr' | 'llm' | 'tts';

export interface ProviderSettings {
  base_url: string;
  model: string;
  timeout_seconds: number;
  api_key_set: boolean;
}

export interface Settings {
  sales: { goal: string; product_info: string; instructions: string; opening: string };
  asr: ProviderSettings & { language: string };
  llm: ProviderSettings;
  tts: ProviderSettings & { voice: string; sample_rate: number };
  voice: { vad_start_seconds: number; vad_stop_seconds: number; vad_confidence: number };
}

export type SettingsUpdate = Omit<Settings, ProviderName> & {
  asr: Omit<Settings['asr'], 'api_key_set'> & { api_key?: string | null };
  llm: Omit<Settings['llm'], 'api_key_set'> & { api_key?: string | null };
  tts: Omit<Settings['tts'], 'api_key_set'> & { api_key?: string | null };
};

export type SecretDrafts = Record<ProviderName, string | null>;

export function settingsUpdate(settings: Settings, secrets: SecretDrafts): SettingsUpdate {
  const provider = <T extends ProviderSettings>(value: T, secret: string | null) => {
    const { api_key_set: _isSet, ...publicValue } = value;
    return { ...publicValue, ...(secret === null ? { api_key: null } : secret ? { api_key: secret } : {}) };
  };
  return {
    sales: settings.sales,
    asr: provider(settings.asr, secrets.asr),
    llm: provider(settings.llm, secrets.llm),
    tts: provider(settings.tts, secrets.tts),
    voice: settings.voice,
  };
}
