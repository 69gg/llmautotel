import type { Settings } from '../types';

export const fixtureSettings: Settings = {
  sales: { goal: '介绍订阅服务', product_info: '月费 20 元', instructions: '', opening: '' },
  asr: { base_url: '', model: '', timeout_seconds: 30, api_key_set: true, language: 'zh' },
  llm: { base_url: '', model: '', timeout_seconds: 30, api_key_set: false },
  tts: { base_url: '', model: '', timeout_seconds: 30, api_key_set: false, voice: '', sample_rate: 24000 },
  voice: { vad_start_seconds: 0.1, vad_stop_seconds: 0.6, vad_confidence: 0.7 },
};
