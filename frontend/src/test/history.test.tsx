import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { api, ApiError } from '../api';
import { HistoryPage } from '../components/HistoryPage';
import type { CallRecord, CallSummary } from '../types';
import { fixtureSettings } from './fixtures';

const record: CallRecord = {
  id: 'first', started_at: '2026-10-04T09:00:00Z', ended_at: '2026-10-04T09:02:00Z', status: 'ended', end_reason: 'user_hangup', settings: fixtureSettings,
  transcript: [{ role: 'assistant', text: '可以为你介绍一下。', timestamp: '2026-10-04T09:00:01Z', interrupted: true }, { role: 'user', text: '价格是多少？', timestamp: '2026-10-04T09:00:04Z', interrupted: false }],
};
const summary: CallSummary = { id: record.id, started_at: record.started_at, ended_at: record.ended_at, status: record.status, end_reason: record.end_reason, goal: fixtureSettings.sales.goal, message_count: 2 };

beforeAll(() => {
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', { configurable: true, value: function(this: HTMLDialogElement) { this.open = true; } });
  Object.defineProperty(HTMLDialogElement.prototype, 'close', { configurable: true, value: function(this: HTMLDialogElement) { this.open = false; } });
});
afterEach(() => vi.restoreAllMocks());

function mockRecords() {
  const calls = vi.spyOn(api, 'calls').mockResolvedValue([summary]);
  const detail = vi.spyOn(api, 'call').mockResolvedValue(record);
  return { calls, detail };
}

async function ready() { await screen.findByText('价格是多少？'); }

describe('本机文字历史', () => {
  it('显示真实列表、结束原因、被打断文字和通话配置快照', async () => {
    const user = userEvent.setup();
    mockRecords();
    render(<HistoryPage />);
    await ready();
    const detail = within(screen.getByRole('region', { name: '记录详情' }));
    expect(detail.getByText('用户挂断')).toBeInTheDocument();
    expect(detail.getByText('02:00')).toBeInTheDocument();
    expect(detail.getByText('已打断 · 播放未完成')).toBeInTheDocument();
    await user.click(detail.getByText('查看本次配置快照'));
    expect(detail.getByText('月费 20 元')).toBeVisible();
    expect(detail.getByText('24000 Hz', { exact: false })).toBeInTheDocument();
    expect(screen.queryByText('api_key')).not.toBeInTheDocument();
  });

  it('AI 挂断记录显示确认结束原因', async () => {
    vi.spyOn(api, 'calls').mockResolvedValue([{ ...summary, end_reason: 'ai_hangup' }]);
    vi.spyOn(api, 'call').mockResolvedValue({ ...record, end_reason: 'ai_hangup' });
    render(<HistoryPage />);
    await ready();
    expect(screen.getByText('AI 确认结束')).toBeInTheDocument();
    expect(screen.queryByText('连接中断')).not.toBeInTheDocument();
  });

  it('确认框可以取消，取消不发删除请求', async () => {
    const user = userEvent.setup();
    mockRecords();
    const remove = vi.spyOn(api, 'deleteCall').mockResolvedValue(undefined);
    render(<HistoryPage />);
    await ready();
    await user.click(screen.getByRole('button', { name: '删除记录' }));
    expect(screen.getByRole('dialog', { name: '删除这通记录？' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '取消' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(remove).not.toHaveBeenCalled();
    expect(screen.getByText('价格是多少？')).toBeInTheDocument();
  });

  it('确认后删除目标记录并刷新成空列表', async () => {
    const user = userEvent.setup();
    const mocks = mockRecords();
    mocks.calls.mockResolvedValueOnce([summary]).mockResolvedValue([]);
    const remove = vi.spyOn(api, 'deleteCall').mockResolvedValue(undefined);
    render(<HistoryPage />);
    await ready();
    await user.click(screen.getByRole('button', { name: '删除记录' }));
    await user.click(screen.getByRole('button', { name: '确认删除' }));
    expect(await screen.findByText('还没有通话记录')).toBeInTheDocument();
    expect(remove).toHaveBeenCalledOnce();
    expect(remove).toHaveBeenCalledWith('first');
    expect(screen.queryByText('价格是多少？')).not.toBeInTheDocument();
  });

  it('活动记录禁止删除，刷新后使用新的服务端状态', async () => {
    const user = userEvent.setup();
    const mocks = mockRecords();
    mocks.detail.mockResolvedValueOnce({ ...record, status: 'active', ended_at: null, end_reason: null }).mockResolvedValue(record);
    const remove = vi.spyOn(api, 'deleteCall');
    render(<HistoryPage />);
    await ready();
    expect(screen.getByRole('button', { name: '删除记录' })).toBeDisabled();
    expect(remove).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: '刷新记录' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '删除记录' })).toBeEnabled());
  });

  it('服务端 409 删除失败保留记录，显示错误并允许刷新', async () => {
    const user = userEvent.setup();
    mockRecords();
    vi.spyOn(api, 'deleteCall').mockRejectedValue(new ApiError('通话仍在进行，结束后可以删除。', 409));
    render(<HistoryPage />);
    await ready();
    await user.click(screen.getByRole('button', { name: '删除记录' }));
    await user.click(screen.getByRole('button', { name: '确认删除' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('通话仍在进行');
    expect(screen.getByText('价格是多少？')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '刷新状态' })).toBeEnabled();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('列表失败可重试，空列表不伪造记录', async () => {
    const user = userEvent.setup();
    vi.spyOn(api, 'calls').mockRejectedValueOnce(new ApiError('服务响应超时', 0)).mockResolvedValue([]);
    const detail = vi.spyOn(api, 'call');
    render(<HistoryPage />);
    expect(await screen.findByRole('alert')).toHaveTextContent('服务响应超时');
    await user.click(screen.getByRole('button', { name: '重试' }));
    expect(await screen.findByText('还没有通话记录')).toBeInTheDocument();
    expect(detail).not.toHaveBeenCalled();
  });

  it('切换详情后旧请求迟到不覆盖当前选中的记录', async () => {
    const user = userEvent.setup();
    let resolveOld!: (call: CallRecord) => void;
    const old = new Promise<CallRecord>(resolve => { resolveOld = resolve; });
    const second: CallRecord = { ...record, id: 'second', settings: { ...fixtureSettings, sales: { ...fixtureSettings.sales, goal: '第二个目标' } }, transcript: [{ role: 'user', text: '第二通对话', timestamp: record.started_at, interrupted: false }] };
    vi.spyOn(api, 'calls').mockResolvedValue([summary, { ...summary, id: second.id, goal: '第二个目标' }]);
    const detail = vi.spyOn(api, 'call').mockImplementation(id => id === 'first' ? old : Promise.resolve(second));
    render(<HistoryPage />);
    await waitFor(() => expect(detail).toHaveBeenCalledWith('first', expect.any(AbortSignal)));
    await user.click(screen.getByRole('button', { name: /查看 .*第二个目标/ }));
    expect(await screen.findByText('第二通对话')).toBeInTheDocument();
    expect(detail.mock.calls[0][1]?.aborted).toBe(true);
    await act(async () => { resolveOld(record); await old; });
    expect(screen.queryByText('价格是多少？')).not.toBeInTheDocument();
    expect(screen.getByText('第二通对话')).toBeInTheDocument();
  });

  it('详情读取失败可以重试，不遗留上一份文字', async () => {
    const user = userEvent.setup();
    const mocks = mockRecords();
    mocks.detail.mockRejectedValueOnce(new ApiError('记录暂时无法读取', 503)).mockResolvedValue(record);
    render(<HistoryPage />);
    expect(await screen.findByRole('alert')).toHaveTextContent('记录暂时无法读取');
    expect(screen.queryByText('价格是多少？')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '重新读取' }));
    await ready();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});
