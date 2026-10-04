import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { api, ApiError } from '../api';
import { TelephonePage } from '../components/TelephonePage';
import { settingsUpdate, type CallRecord, type Settings, type TelephonyProvider } from '../types';
import { fixtureSettings } from './fixtures';

const catalog: TelephonyProvider[] = [
  { id: 'asterisk', label: 'Asterisk', mode: 'media', description: '使用本机三组模型。', enabled: false, fields: [
    { name: 'ari_url', label: 'ARI 地址', type: 'string', nullable: false, minimum: null, maximum: null },
    { name: 'password', label: '连接密码', type: 'secret', nullable: true, minimum: null, maximum: null },
    { name: 'ring_timeout_seconds', label: '振铃超时（秒）', type: 'number', nullable: false, minimum: 5, maximum: 180 },
  ] },
  { id: 'freeswitch', label: 'FreeSWITCH', mode: 'media', description: '本机语音管线。', enabled: false, fields: [] },
  { id: 'aliyun', label: '阿里云 AICCS', mode: 'cloud', description: '平台托管语音。', enabled: false, fields: [
    { name: 'access_key_secret', label: 'AccessKey Secret', type: 'secret', nullable: true, minimum: null, maximum: null },
    { name: 'session_timeout', label: '平台会话超时', type: 'integer', nullable: true, minimum: 30, maximum: 3600 },
  ] },
  { id: 'tencent', label: '腾讯云 TCCC', mode: 'cloud', description: '通过模型网关。', enabled: false, fields: [] },
];

const configured: Settings = {
  ...fixtureSettings,
  asr: { ...fixtureSettings.asr, base_url: 'https://asr.test/v1', model: 'asr' },
  llm: { ...fixtureSettings.llm, base_url: 'https://llm.test/v1', model: 'llm' },
  tts: { ...fixtureSettings.tts, base_url: 'https://tts.test/v1', model: 'tts', voice: 'voice' },
  telephony: { ...fixtureSettings.telephony, asterisk: { ...fixtureSettings.telephony.asterisk, enabled: true, password_set: true } },
};
const record: CallRecord = {
  id: 'phone-one', channel: 'telephone', provider: 'asterisk', destination: '13800000000',
  started_at: '2026-10-04T09:00:00Z', ended_at: null, status: 'active', state: 'listening',
  end_reason: null, settings: configured,
  transcript: [{ role: 'assistant', text: '您好，考虑预约演示吗？', timestamp: '2026-10-04T09:00:01Z', interrupted: true }],
};

function page(settings = fixtureSettings) {
  return render(<TelephonePage settings={settings} onSave={vi.fn()} onConfigure={vi.fn()} onBrowserCall={vi.fn()} />);
}

function StatefulPage({ initial }: { initial: Settings }) {
  const [settings, setSettings] = useState(initial);
  return <TelephonePage settings={settings} onSave={setSettings} onConfigure={vi.fn()} onBrowserCall={vi.fn()} />;
}

beforeEach(() => {
  vi.spyOn(api, 'telephonyProviders').mockResolvedValue(catalog);
  vi.spyOn(api, 'activeCall').mockResolvedValue(null);
});
afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); });

describe('电话配置', () => {
  it('默认全禁用，不自动选择线路、发起外呼或调用浏览器语音接口', async () => {
    const start = vi.spyOn(api, 'startTelephoneCall');
    const browserStart = vi.spyOn(api, 'startCall');
    const media = vi.spyOn(HTMLMediaElement.prototype, 'play');
    page();
    const select = await screen.findByLabelText('配置 provider');
    expect(select).toHaveValue('asterisk');
    expect(screen.getByLabelText('启用 Asterisk')).not.toBeChecked();
    const dial = within(screen.getByLabelText('外呼 provider'));
    for (const provider of catalog) expect(dial.getByRole('option', { name: `${provider.label}（未启用）` })).toBeDisabled();
    expect(screen.getByLabelText('外呼 provider')).toHaveValue('');
    expect(screen.getByRole('button', { name: '发起外呼' })).toBeDisabled();
    expect(start).not.toHaveBeenCalled();
    expect(browserStart).not.toHaveBeenCalled();
    expect(media).not.toHaveBeenCalled();
  });

  it('元数据生成数值与密码字段；保存保留空凭据、清除显式null，去掉所有回读标记', async () => {
    const user = userEvent.setup();
    const save = vi.spyOn(api, 'saveSettings').mockResolvedValue(configured);
    const storage = vi.spyOn(Storage.prototype, 'setItem');
    render(<StatefulPage initial={configured} />);
    const password = await screen.findByLabelText('Asterisk 连接密码');
    expect(password).toHaveValue('');
    await user.type(password, 'new-phone-secret');
    await user.clear(screen.getByLabelText('Asterisk ARI 地址'));
    await user.type(screen.getByLabelText('Asterisk ARI 地址'), 'https://pbx.test/ari');
    await user.selectOptions(screen.getByLabelText('配置 provider'), 'aliyun');
    await user.type(screen.getByLabelText('阿里云 AICCS AccessKey Secret'), 'cloud-secret');
    await user.click(screen.getByRole('button', { name: '清除 阿里云 AICCS AccessKey Secret' }));
    await user.type(screen.getByLabelText('阿里云 AICCS 平台会话超时'), '120');
    await user.click(screen.getByRole('button', { name: '保存电话配置' }));
    await waitFor(() => expect(save).toHaveBeenCalledOnce());
    const body = save.mock.calls[0][0];
    expect(body.telephony.asterisk).toMatchObject({ password: 'new-phone-secret', ari_url: 'https://pbx.test/ari' });
    expect(body.telephony.aliyun).toMatchObject({ access_key_secret: null, session_timeout: 120 });
    expect(body.telephony.freeswitch).not.toHaveProperty('password');
    expect(body.asr).not.toHaveProperty('api_key');
    expect(JSON.stringify(body)).not.toContain('_set');
    expect(await screen.findByText('电话配置已保存；仅在手动发起外呼时使用。')).toBeInTheDocument();
    expect(screen.getByLabelText('阿里云 AICCS AccessKey Secret')).toHaveValue('');
    expect(storage).not.toHaveBeenCalled();
  });

  it('保存启用不会拨号，只有保存后外呼选项才启用', async () => {
    const user = userEvent.setup();
    const start = vi.spyOn(api, 'startTelephoneCall');
    vi.spyOn(api, 'saveSettings').mockResolvedValue(configured);
    render(<StatefulPage initial={{ ...configured, telephony: fixtureSettings.telephony }} />);
    await user.click(await screen.findByLabelText('启用 Asterisk'));
    expect(within(screen.getByLabelText('外呼 provider')).getByRole('option', { name: 'Asterisk（未启用）' })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: '保存电话配置' }));
    await screen.findByText('电话配置已保存；仅在手动发起外呼时使用。');
    expect(within(screen.getByLabelText('外呼 provider')).getByRole('option', { name: 'Asterisk' })).toBeEnabled();
    expect(screen.getByLabelText('外呼 provider')).toHaveValue('');
    expect(start).not.toHaveBeenCalled();
  });

  it('保存失败保留新凭据和开关，允许重试', async () => {
    const user = userEvent.setup();
    vi.spyOn(api, 'saveSettings').mockRejectedValue(new ApiError('请检查连接参数。', 422));
    page();
    await user.type(await screen.findByLabelText('Asterisk 连接密码'), 'retry-secret');
    await user.click(screen.getByLabelText('启用 Asterisk'));
    await user.click(screen.getByRole('button', { name: '保存电话配置' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('请检查连接参数。');
    expect(screen.getByLabelText('Asterisk 连接密码')).toHaveValue('retry-secret');
    expect(screen.getByLabelText('启用 Asterisk')).toBeChecked();
    expect(screen.getByRole('button', { name: '保存电话配置' })).toBeEnabled();
  });

  it('普通对话配置保存保留电话配置并省略电话凭据回读标记', () => {
    const body = settingsUpdate(configured, { asr: '', llm: '', tts: '' });
    expect(body.telephony.asterisk).toMatchObject({ enabled: true, app: 'llmautotel' });
    expect(body.telephony.asterisk).not.toHaveProperty('password_set');
    expect(body.telephony.asterisk).not.toHaveProperty('password');
    expect(body.telephony.aliyun).not.toHaveProperty('access_key_secret_set');
  });

  it('保存过程中切页仍更新工作台公开配置，不丢失已保存的启用状态', async () => {
    const user = userEvent.setup();
    let complete: (settings: Settings) => void = () => undefined;
    vi.spyOn(api, 'saveSettings').mockImplementation(() => new Promise<Settings>(resolve => { complete = resolve; }));
    const saved = vi.fn();
    const rendered = render(<TelephonePage settings={fixtureSettings} onSave={saved} onConfigure={vi.fn()} onBrowserCall={vi.fn()} />);
    await screen.findByLabelText('配置 provider');
    await user.click(screen.getByRole('button', { name: '保存电话配置' }));
    rendered.unmount();
    await act(async () => { complete(configured); });
    expect(saved).toHaveBeenCalledExactlyOnceWith(configured);
  });
});

describe('手动电话外呼', () => {
  it('选择线路并点击才拨号，显示文字和打断标记，挂断调用共享结束接口', async () => {
    const user = userEvent.setup();
    const start = vi.spyOn(api, 'startTelephoneCall').mockResolvedValue({ call: record });
    const end = vi.spyOn(api, 'endCall').mockResolvedValue({ ...record, status: 'ended', state: 'ended', ended_at: '2026-10-04T09:01:00Z' });
    page(configured);
    await user.selectOptions(await screen.findByLabelText('外呼 provider'), 'asterisk');
    await user.type(screen.getByLabelText('被叫号码'), '13800000000');
    expect(start).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: '发起外呼' }));
    expect(start).toHaveBeenCalledExactlyOnceWith('asterisk', '13800000000');
    expect(await screen.findByText('倾听中')).toBeInTheDocument();
    expect(screen.getByText('您好，考虑预约演示吗？')).toBeInTheDocument();
    expect(screen.getByText('已打断 · 播放未完成')).toBeInTheDocument();
    expect(screen.getByLabelText('被叫号码')).toBeDisabled();
    await user.click(screen.getByRole('button', { name: '挂断电话' }));
    expect(end).toHaveBeenCalledExactlyOnceWith('phone-one');
    expect(await screen.findByText('通话已结束')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '发起外呼' })).toBeEnabled();
  });

  it('已有浏览器通话占用槽位时禁止拨号，可返回语音页', async () => {
    const user = userEvent.setup();
    vi.spyOn(api, 'activeCall').mockResolvedValue({ ...record, channel: 'browser', provider: null, destination: null });
    const start = vi.spyOn(api, 'startTelephoneCall');
    const navigate = vi.fn();
    render(<TelephonePage settings={configured} onSave={vi.fn()} onConfigure={vi.fn()} onBrowserCall={navigate} />);
    expect(await screen.findByText('浏览器语音正在通话')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '发起外呼' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '返回语音通话' }));
    expect(navigate).toHaveBeenCalledOnce();
    expect(start).not.toHaveBeenCalled();
  });

  it('两标签页竞争时显示409，下一次尝试仍可操作', async () => {
    const user = userEvent.setup();
    vi.spyOn(api, 'startTelephoneCall').mockRejectedValue(new ApiError('已有一通会话，不能重复开始。', 409));
    page(configured);
    await user.selectOptions(await screen.findByLabelText('外呼 provider'), 'asterisk');
    await user.type(screen.getByLabelText('被叫号码'), '13800000000');
    await user.click(screen.getByRole('button', { name: '发起外呼' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('已有一通会话');
    expect(screen.getByRole('button', { name: '发起外呼' })).toBeEnabled();
  });

  it('云托管只要求目标和LLM，未配置本机ASR/TTS也可手动发起', async () => {
    const user = userEvent.setup();
    const cloudSettings = { ...fixtureSettings, llm: configured.llm, telephony: { ...fixtureSettings.telephony, aliyun: { ...fixtureSettings.telephony.aliyun, enabled: true } } };
    const start = vi.spyOn(api, 'startTelephoneCall').mockResolvedValue({ call: { ...record, provider: 'aliyun' } });
    page(cloudSettings);
    await user.selectOptions(await screen.findByLabelText('外呼 provider'), 'aliyun');
    await user.type(screen.getByLabelText('被叫号码'), '13800000000');
    expect(screen.getByText('平台托管语音，使用本机模型网关连接已配置 LLM。')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '发起外呼' })).toBeEnabled();
    await user.click(screen.getByRole('button', { name: '发起外呼' }));
    expect(start).toHaveBeenCalledExactlyOnceWith('aliyun', '13800000000');
  });

  it('卸载取消轮询但不挂远端电话，迟到状态不会继续发请求', async () => {
    vi.useFakeTimers();
    vi.spyOn(api, 'activeCall').mockResolvedValue(record);
    let signal: AbortSignal | undefined;
    let resolve: (call: CallRecord) => void = () => undefined;
    const detail = vi.spyOn(api, 'call').mockImplementation((_id, currentSignal) => {
      signal = currentSignal;
      return new Promise<CallRecord>(done => { resolve = done; });
    });
    const end = vi.spyOn(api, 'endCall');
    const rendered = page(configured);
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByText('倾听中')).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(detail).toHaveBeenCalledOnce();
    rendered.unmount();
    expect(signal?.aborted).toBe(true);
    await act(async () => { resolve({ ...record, state: 'speaking' }); await vi.advanceTimersByTimeAsync(3000); });
    expect(detail).toHaveBeenCalledOnce();
    expect(end).not.toHaveBeenCalled();
  });

  it('后台接入失败经轮询显示实际失败阶段，不只显示外呼失败', async () => {
    vi.useFakeTimers();
    vi.spyOn(api, 'activeCall').mockResolvedValue(record);
    vi.spyOn(api, 'call').mockResolvedValue({ ...record, status: 'failed', state: 'failed', ended_at: '2026-10-04T09:01:00Z', end_reason: 'Asterisk 媒体接入失败。' });
    page(configured);
    await act(async () => { await Promise.resolve(); });
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(screen.getByText('外呼失败')).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('Asterisk 媒体接入失败。');
  });
});
