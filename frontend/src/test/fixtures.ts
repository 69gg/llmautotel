import type { Settings } from '../types';

export const fixtureSettings: Settings = {
  sales: { goal: '介绍订阅服务', product_info: '月费 20 元', instructions: '', opening: '' },
  asr: { base_url: '', model: '', timeout_seconds: 30, api_key_set: true, protocol: 'openai', language: 'zh' },
  llm: { base_url: '', model: '', timeout_seconds: 30, api_key_set: false, thinking: null, reasoning_effort: null },
  tts: { base_url: '', model: '', timeout_seconds: 30, api_key_set: false, protocol: 'openai', voice: '', sample_rate: 24000 },
  voice: { vad_start_seconds: 0.1, vad_stop_seconds: 0.6, vad_confidence: 0.7 },
  telephony: {
    public_base_url: '',
    asterisk: { enabled: false, ari_url: '', username: '', password_set: false, app: 'llmautotel', endpoint_template: '', caller_id: '', ring_timeout_seconds: 45, media_timeout_seconds: 10 },
    freeswitch: { enabled: false, host: '', port: 8021, password_set: false, gateway: '', caller_id: '', fs_media_host: '', fs_media_port: 0, audio_bind_host: '127.0.0.1', audio_bind_port: 0, audio_advertised_host: '', ring_timeout_seconds: 45, media_timeout_seconds: 10, playback_tail_seconds: 0.1 },
    aliyun: { enabled: false, endpoint: '', region: '', caller_id: '', access_key_id_set: false, access_key_secret_set: false, gateway_token_set: false, webhook_token_set: false, app_id: '', tts_voice: '', timeout_seconds: 30, session_timeout: null },
    tencent: { enabled: false, endpoint: '', region: '', caller_id: '', secret_id_set: false, secret_key_set: false, gateway_token_set: false, webhook_token_set: false, sdk_app_id: null, tts_voice: '', timeout_seconds: 30, interrupt_speech_duration_ms: null, vad_silence_ms: null },
  },
};
