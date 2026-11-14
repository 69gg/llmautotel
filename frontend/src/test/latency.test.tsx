import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { LatencyDetails } from '../components/LatencyDetails';
import type { TurnLatency } from '../types';

const reply: TurnLatency = { sequence: 2, kind: 'reply', timestamp: '2026-11-14T00:00:01Z', total_ms: 1400, target_ms: 1000, parts: [{ stage: 'transcription', duration_ms: 400 }, { stage: 'speech_synthesis', duration_ms: 1000 }] };

describe('逐轮服务端首音统计', () => {
  it('旧记录无指标时不显示虚构的零耗时', () => {
    render(<LatencyDetails entries={[]} />);
    expect(screen.queryByRole('region', { name: '响应耗时' })).not.toBeInTheDocument();
  });

  it('区分开场与回复，显示超出参照和各阶段，不将开场算作回复达标', async () => {
    const user = userEvent.setup();
    render(<LatencyDetails entries={[{ ...reply, sequence: 1, kind: 'opening', total_ms: 600 }, reply]} />);
    expect(screen.getByText('开场 · 服务端首音')).toBeInTheDocument();
    expect(screen.getAllByText('超出 1.00 秒 参照')).toHaveLength(1);
    expect(screen.queryByText(/低于/)).not.toBeInTheDocument();
    await user.click(screen.getByText('回复 · 服务端首音'));
    expect(screen.getAllByText('语音识别')).toHaveLength(2);
    expect(screen.getAllByText(/终端首段有效声音需另行实测/)).toHaveLength(2);
  });

  it('通话只展示最近一条，保留原有指标数组', () => {
    const entries = [{ ...reply, kind: 'opening' as const, sequence: 1 }, reply];
    render(<LatencyDetails entries={entries} latestOnly />);
    expect(screen.queryByText('开场 · 服务端首音')).not.toBeInTheDocument();
    expect(screen.getByText('回复 · 服务端首音')).toBeInTheDocument();
    expect(entries).toHaveLength(2);
  });

  it('等于目标时准确显示不超过参照', () => {
    render(<LatencyDetails entries={[{ ...reply, total_ms: 1000 }]} />);
    expect(screen.getByText('不超过 1.00 秒 参照')).toBeInTheDocument();
  });
});
