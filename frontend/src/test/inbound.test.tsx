import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '../App';
import { api } from '../api';
import { HistoryPage } from '../components/HistoryPage';
import { SettingsPage } from '../components/SettingsPage';
import { TelephonePage } from '../components/TelephonePage';
import { callSource, conversationMode, type CallRecord, type InboundProviderStatus, type Settings, type TelephonyProvider } from '../types';
import { fixtureSettings } from './fixtures';

vi.mock('@pipecat-ai/small-webrtc-transport', () => ({ SmallWebRTCTransport: class {} }));

const consultation: Settings = { ...fixtureSettings, conversation: { mode: 'consultation' } };
const catalog: TelephonyProvider[] = [
  { id: 'asterisk', label: 'Asterisk', mode: 'media', description: 'SIP 媒体接入', enabled: false, fields: [
    { name: 'inbound_enabled', label: '启用来电接听', type: 'boolean', nullable: false, minimum: null, maximum: null },
    { name: 'inbound_numbers', label: '接听号码白名单', type: 'array', nullable: false, minimum: null, maximum: null },
  ] },
  { id: 'aliyun', label: '阿里云 AICCS', mode: 'cloud', description: '托管来电', enabled: false, fields: [
    { name: 'inbound_enabled', label: '启用来电接听', type: 'boolean', nullable: false, minimum: null, maximum: null },
    { name: 'inbound_numbers', label: '接听号码白名单', type: 'array', nullable: false, minimum: null, maximum: null },
  ] },
];
const incoming: CallRecord = {
  id: 'incoming-one', channel: 'telephone', provider: 'asterisk', direction: 'inbound', caller: '13800000000', destination: '02112345678',
  started_at: '2026-10-05T14:00:00Z', ended_at: null, status: 'active', state: 'listening', end_reason: null, settings: consultation,
  transcript: [{ role: 'user', text: '产品怎么导出文件？', timestamp: '2026-10-05T14:00:01Z', interrupted: false }],
};

beforeEach(() => {
  vi.spyOn(api, 'telephonyProviders').mockResolvedValue(catalog);
  vi.spyOn(api, 'activeCall').mockResolvedValue(null);
  vi.spyOn(api, 'inboundProviders').mockResolvedValue([
    { provider: 'asterisk', state: 'disabled', error: null },
    { provider: 'aliyun', state: 'awaiting_callback', error: null },
  ]);
});
afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); });

function page(settings = consultation) {
  return render(<TelephonePage settings={settings} onSave={vi.fn()} onConfigure={vi.fn()} onBrowserCall={vi.fn()} />);
}

describe('咨询配置和来电监控', () => {
  it('工作台默认打开电话接听，查询状态但不发起电话或使用麦克风', async () => {
    vi.spyOn(api, 'settings').mockResolvedValue(consultation);
    const start = vi.spyOn(api, 'startCall');
    const phoneStart = vi.spyOn(api, 'startTelephoneCall');
    const media = vi.spyOn(HTMLMediaElement.prototype, 'play');
    render(<App />);
    expect(await screen.findByRole('heading', { name: '接听产品咨询' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '电话接入' })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('button', { name: '来电接听' })).toHaveAttribute('aria-pressed', 'true');
    expect(await screen.findByText('等待来电回调')).toBeInTheDocument();
    expect(screen.queryByLabelText('被叫号码')).not.toBeInTheDocument();
    expect(start).not.toHaveBeenCalled();
    expect(phoneStart).not.toHaveBeenCalled();
    expect(media).not.toHaveBeenCalled();
  });

  it('咨询和销售分别编辑，切换后保留旧目标和两组提示词，保存全部配置', async () => {
    const user = userEvent.setup();
    const save = vi.spyOn(api, 'saveSettings').mockResolvedValue(consultation);
    render(<SettingsPage settings={consultation} onSave={vi.fn()} />);
    expect(screen.getByRole('combobox', { name: '对话模式' })).toHaveValue('consultation');
    expect(screen.queryByLabelText('销售目标')).not.toBeInTheDocument();
    await user.type(screen.getByLabelText(/咨询提示词/), '回答时先说明操作入口。');
    await user.type(screen.getByLabelText(/固定欢迎语/), '您好，欢迎咨询。');
    await user.selectOptions(screen.getByRole('combobox', { name: '对话模式' }), 'sales');
    expect(screen.getByLabelText('销售目标')).toHaveValue(fixtureSettings.sales.goal);
    expect(screen.getByRole('textbox', { name: /^产品资料/ })).toHaveValue(fixtureSettings.sales.product_info);
    await user.type(screen.getByLabelText(/话术要求/), '介绍产品价值。');
    await user.selectOptions(screen.getByRole('combobox', { name: '对话模式' }), 'consultation');
    expect(screen.getByLabelText(/咨询提示词/)).toHaveValue('回答时先说明操作入口。');
    expect(screen.getByLabelText(/固定欢迎语/)).toHaveValue('您好，欢迎咨询。');
    expect(screen.getByRole('textbox', { name: /^产品资料/ })).toHaveValue(consultation.consultation.product_info);
    await user.click(screen.getAllByRole('button', { name: '保存配置' })[0]);
    await waitFor(() => expect(save).toHaveBeenCalledOnce());
    expect(save.mock.calls[0][0]).toMatchObject({
      conversation: { mode: 'consultation' }, consultation: { instructions: '回答时先说明操作入口。', opening: '您好，欢迎咨询。' },
      sales: { goal: fixtureSettings.sales.goal, instructions: '介绍产品价值。' },
    });
  });

  it('咨询模式无需销售目标，云托管外呼只要求咨询资料和文本模型', async () => {
    const user = userEvent.setup();
    const settings: Settings = {
      ...consultation, sales: { ...consultation.sales, goal: '', product_info: '' },
      llm: { ...consultation.llm, base_url: 'https://llm.test/v1', model: 'configured-model' },
      telephony: { ...consultation.telephony, aliyun: { ...consultation.telephony.aliyun, enabled: true } },
    };
    page(settings);
    await user.click(screen.getByRole('button', { name: '手动外呼' }));
    await user.selectOptions(await screen.findByLabelText('外呼 provider'), 'aliyun');
    await user.type(screen.getByLabelText('被叫号码'), '13800000000');
    expect(screen.getByRole('button', { name: '发起外呼' })).toBeEnabled();
    expect(screen.getByText('产品咨询')).toBeInTheDocument();
  });

  it('布尔接听开关和数组号码按元数据填写，保存不拨号并说明后台连接', async () => {
    const user = userEvent.setup();
    const save = vi.spyOn(api, 'saveSettings').mockResolvedValue(consultation);
    const start = vi.spyOn(api, 'startTelephoneCall');
    page();
    await user.selectOptions(await screen.findByLabelText('配置 provider'), 'aliyun');
    await user.click(screen.getByLabelText('启用 阿里云 AICCS'));
    await user.click(screen.getByLabelText('阿里云 AICCS 启用来电接听'));
    await user.type(screen.getByLabelText('阿里云 AICCS 接听号码白名单'), '02112345678, 01012345678\n4001234567');
    expect(screen.getByLabelText('阿里云 AICCS 接听号码白名单')).toHaveValue('02112345678, 01012345678\n4001234567');
    await user.click(screen.getByRole('button', { name: '保存电话配置' }));
    await waitFor(() => expect(save).toHaveBeenCalledOnce());
    expect(save.mock.calls[0][0].telephony.aliyun).toMatchObject({ enabled: true, inbound_enabled: true, inbound_numbers: ['02112345678', '01012345678', '4001234567'] });
    expect(JSON.stringify(save.mock.calls[0][0])).not.toContain('_set');
    expect(await screen.findByText('电话配置已保存；已启用的接听线路将建立监听，外呼仍需手动发起。')).toBeInTheDocument();
    expect(start).not.toHaveBeenCalled();
    expect(screen.getByText(/号码路由和平台连通性需在服务商控制台确认/)).toBeInTheDocument();
  });

  it('来电期间显示主被叫号码与咨询模式，可手动结束并保留文字', async () => {
    const user = userEvent.setup();
    vi.spyOn(api, 'activeCall').mockResolvedValue(incoming);
    const end = vi.spyOn(api, 'endCall').mockResolvedValue({ ...incoming, status: 'ended', state: 'ended', ended_at: '2026-10-05T14:00:40Z', end_reason: 'user_hangup' });
    page();
    expect(await screen.findByText('产品怎么导出文件？')).toBeInTheDocument();
    expect(screen.getByText('来电用户')).toBeInTheDocument();
    expect(screen.getByText('电话来电 · Asterisk · 13800000000')).toBeInTheDocument();
    expect(screen.getByText('来电 13800000000 · 接听 02112345678')).toBeInTheDocument();
    expect(screen.getByText('产品咨询')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '挂断电话' }));
    expect(end).toHaveBeenCalledExactlyOnceWith('incoming-one');
    expect(await screen.findByText('通话已结束')).toBeInTheDocument();
    expect(screen.getByText('产品怎么导出文件？')).toBeInTheDocument();
  });

  it('空闲后新来电可由轮询显示，切页停止两个状态轮询且不挂断', async () => {
    vi.useFakeTimers();
    vi.spyOn(api, 'activeCall').mockResolvedValueOnce(null).mockResolvedValue(incoming);
    let resolve: (value: InboundProviderStatus[]) => void = () => undefined;
    let signal: AbortSignal | undefined;
    const status = vi.spyOn(api, 'inboundProviders').mockImplementation(current => {
      signal = current;
      return new Promise(done => { resolve = done; });
    });
    const end = vi.spyOn(api, 'endCall');
    const rendered = page();
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByText('等待来电')).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(screen.getByText('产品怎么导出文件？')).toBeInTheDocument();
    rendered.unmount();
    expect(signal?.aborted).toBe(true);
    await act(async () => { resolve([]); await vi.advanceTimersByTimeAsync(3000); });
    expect(status).toHaveBeenCalledOnce();
    expect(end).not.toHaveBeenCalled();
  });

  it('来电历史使用咨询配置，旧无模式和方向字段仍识别为销售外呼', async () => {
    const user = userEvent.setup();
    const ended = { ...incoming, status: 'ended', ended_at: '2026-10-05T14:00:40Z' };
    vi.spyOn(api, 'calls').mockResolvedValue([{ ...ended, goal: '产品咨询', message_count: 1 }]);
    vi.spyOn(api, 'call').mockResolvedValue(ended);
    render(<HistoryPage />);
    expect(await screen.findByText('产品怎么导出文件？')).toBeInTheDocument();
    const detail = within(screen.getByRole('region', { name: '记录详情' }));
    expect(detail.getByText('电话来电 · Asterisk · 13800000000')).toBeInTheDocument();
    expect(screen.getByText('1 条对话 · 电话来电')).toBeInTheDocument();
    await user.click(screen.getByText('查看本次配置快照'));
    expect(detail.getByText(consultation.consultation.product_info)).toBeInTheDocument();
    expect(detail.queryByText(fixtureSettings.sales.product_info)).not.toBeInTheDocument();
    expect(callSource({ channel: 'telephone', provider: 'asterisk', destination: '13800000000' })).toBe('电话外呼 · Asterisk · 13800000000');
    const { conversation: _mode, consultation: _consultation, ...oldSettings } = fixtureSettings;
    expect(conversationMode(oldSettings as Settings)).toBe('sales');
  });
});
