import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { RTVIEventCallbacks } from '@pipecat-ai/client-js';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { App } from '../App';
import { api } from '../api';
import { fixtureSettings } from './fixtures';

const fake = vi.hoisted(() => ({ callbacks: null as RTVIEventCallbacks | null, stop: vi.fn(), disconnect: vi.fn(), enableMic: vi.fn() }));
vi.mock('@pipecat-ai/small-webrtc-transport', () => ({ SmallWebRTCTransport: class {} }));
vi.mock('@pipecat-ai/client-js', () => ({
  LogLevel: { NONE: 0 },
  PipecatClient: class {
    constructor(options: { callbacks: RTVIEventCallbacks }) { fake.callbacks = options.callbacks; }
    setLogLevel() {}
    async initDevices() {}
    async connect() { fake.callbacks?.onBotReady?.({} as never); }
    async disconnect() { fake.disconnect(); fake.callbacks?.onDisconnected?.(); }
    tracks() { return { local: { audio: { stop: fake.stop, applyConstraints: async () => undefined } } }; }
    enableMic = fake.enableMic;
  },
}));

afterEach(() => vi.restoreAllMocks());

describe('工作台导航', () => {
  it('已填写目标与全部模型但产品资料为空时，开始通话不可用', async () => {
    const user = userEvent.setup();
    vi.spyOn(api, 'settings').mockResolvedValue({
      ...fixtureSettings,
      sales: { ...fixtureSettings.sales, product_info: '' },
      asr: { ...fixtureSettings.asr, base_url: 'https://asr.test/v1', model: 'asr' },
      llm: { ...fixtureSettings.llm, base_url: 'https://llm.test/v1', model: 'llm' },
      tts: { ...fixtureSettings.tts, base_url: 'https://tts.test/v1', model: 'tts', voice: 'custom' },
    });
    const start = vi.spyOn(api, 'startCall');
    render(<App />);
    const button = await screen.findByRole('button', { name: '开始通话' });
    expect(button).toBeDisabled();
    await user.click(button);
    expect(start).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: '语音通话' })).toHaveAttribute('aria-label', '语音通话');
  });

  it('开始后切换配置页仍维持当前通话，保存新配置不改变通话快照', async () => {
    vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => undefined);
    const user = userEvent.setup();
    const settings = {
      ...fixtureSettings,
      asr: { ...fixtureSettings.asr, base_url: 'https://asr.test/v1', model: 'asr' },
      llm: { ...fixtureSettings.llm, base_url: 'https://llm.test/v1', model: 'llm' },
      tts: { ...fixtureSettings.tts, base_url: 'https://tts.test/v1', model: 'tts', voice: 'custom' },
    };
    const record = { id: 'one', started_at: new Date().toISOString(), ended_at: null, status: 'active', end_reason: null, settings, transcript: [] };
    vi.spyOn(api, 'settings').mockResolvedValue(settings);
    vi.spyOn(api, 'startCall').mockResolvedValue({ call: record, connection: { webrtcRequestParams: { endpoint: '/api/offer?call_id=one' }, iceConfig: { iceServers: [] } } });
    vi.spyOn(api, 'endCall').mockResolvedValue({ ...record, ended_at: new Date().toISOString(), status: 'ended' });
    vi.spyOn(api, 'saveSettings').mockResolvedValue({ ...settings, sales: { ...settings.sales, goal: '新的目标' } });
    render(<App />);
    await user.click(await screen.findByRole('button', { name: '开始通话' }));
    await screen.findByText('倾听中');
    await user.click(screen.getAllByRole('button', { name: '对话配置' })[0]);
    const goal = await screen.findByLabelText('销售目标');
    await user.clear(goal);
    await user.type(goal, '新的目标');
    await user.click(screen.getAllByRole('button', { name: '保存配置' })[0]);
    await screen.findByText('已保存，下次通话将使用新配置。');
    expect(fake.disconnect).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: '语音通话' }));
    expect(await screen.findByText('倾听中')).toBeInTheDocument();
    expect(screen.getByText('介绍订阅服务')).toBeInTheDocument();
    expect(screen.queryByText('新的目标')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '挂断通话' }));
    await waitFor(() => expect(screen.getByText('通话已结束')).toBeInTheDocument());
    expect(fake.stop).toHaveBeenCalled();
    expect(fake.disconnect).toHaveBeenCalledTimes(1);
  });
});
