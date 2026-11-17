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
    expect(screen.getAllByText('语音识别 ASR')).toHaveLength(2);
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

it('最新轮次默认展开，历史轮次可比较，精确显示毫秒和占比，平均值排除开场', () => {
  const entries: TurnLatency[] = [
    { ...reply, sequence: 1, kind: 'opening', total_ms: 600, parts: [] },
    { ...reply, sequence: 2, total_ms: 1232.5 },
    { ...reply, sequence: 3, total_ms: 779.4, parts: [{ stage: 'transcription', duration_ms: 104.9 }, { stage: 'speech_synthesis', duration_ms: 445.9 }] },
  ];
  const { container } = render(<LatencyDetails entries={entries} expandLatest />);
  const rounds = container.querySelectorAll('details');
  expect(rounds).toHaveLength(3);
  expect(rounds[0]).not.toHaveAttribute('open');
  expect(rounds[2]).toHaveAttribute('open');
  expect(screen.getByText('2 轮回复 · 平均 1006.0 ms')).toBeInTheDocument();
  expect(screen.getByText('104.9 ms')).toBeVisible();
  expect(screen.getByText('445.9 ms')).toBeVisible();
  expect(screen.getByText('57.2%')).toBeVisible();
  expect(screen.getByText('779.4 ms')).toBeVisible();
  expect(screen.getAllByText(/不包含之后到网页扬声器的传输和播放/)).toHaveLength(3);
});

it('缺少阶段与零耗时均明确展示，未知阶段保留名称', () => {
  render(<LatencyDetails entries={[{ ...reply, sequence: 1, parts: [] }, { ...reply, sequence: 2, total_ms: 0, parts: [{ stage: 'custom_stage', duration_ms: 0 }] }]} expandLatest />);
  expect(screen.getByText('此轮未记录阶段明细。')).toBeInTheDocument();
  expect(screen.getByText('其他处理（custom_stage）')).toBeVisible();
  expect(screen.queryByText(/NaN|Infinity/)).not.toBeInTheDocument();
  expect(screen.getAllByText('—')).toHaveLength(2);
});


it('分别显示分句等待和请求后合成，并兼容原来无法拆分的统计', () => {
  render(<LatencyDetails entries={[{ ...reply, parts: [{ stage: 'sentence_aggregation', duration_ms: 120 }, { stage: 'tts_inference', duration_ms: 430 }, { stage: 'speech_synthesis', duration_ms: 100 }] }]} expandLatest />);
  expect(screen.getByText('分句等待')).toBeVisible();
  expect(screen.getByText('语音合成 TTS（请求至首 PCM）')).toBeVisible();
  expect(screen.getByText('分句与合成 TTS')).toBeVisible();
  expect(screen.getByText('430.0 ms')).toBeVisible();
});
