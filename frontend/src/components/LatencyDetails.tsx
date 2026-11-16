import type { TurnLatency } from '../types';

const labels: Record<string, string> = {
  endpointing_wait: '停口确认', transcription: '语音识别 ASR', turn_detection: '回合判断',
  llm_inference: '文本生成 LLM', sentence_aggregation: '分句等待', speech_synthesis: '分句与合成 TTS',
  output_transport: '输出排队', pipeline: '管线处理', first_request: '开场准备',
  llm_tool_call: '工具参数生成', function_handler: '工具处理', turn_completion: '回合确认',
  waiting_for_user: '等待用户', awaiting_speakable_text: '正文等待',
};
const seconds = (ms: number) => `${(ms / 1000).toFixed(2)} 秒`;
const milliseconds = (ms: number) => `${ms.toFixed(1)} ms`;

export function LatencyDetails({ entries, latestOnly = false, expandLatest = false }: { entries: TurnLatency[]; latestOnly?: boolean; expandLatest?: boolean }) {
  if (!entries.length) return null;
  const shown = latestOnly ? entries.slice(-1) : entries;
  const replies = entries.filter(entry => entry.kind === 'reply');
  const average = replies.length ? replies.reduce((sum, entry) => sum + entry.total_ms, 0) / replies.length : null;
  return <section className="latency-panel" aria-label="响应耗时">
    <div className="latency-heading"><strong>各阶段耗时</strong><span>{replies.length} 轮回复{average !== null && ` · 平均 ${milliseconds(average)}`}</span></div>
    <div className="latency-rounds">
      {shown.map(entry => <details className="latency-entry" key={`${entry.sequence}-${entry.timestamp}`} open={expandLatest && entry === entries.at(-1) ? true : undefined}>
        <summary><span>{entry.kind === 'opening' ? '开场 · 服务端首音' : '回复 · 服务端首音'}</span><strong>{seconds(entry.total_ms)}</strong><span className="latency-round-label">#{entry.sequence} · {new Date(entry.timestamp).toLocaleTimeString('zh-CN', { hour12: false })}</span>{entry.kind === 'reply' && <small className={entry.total_ms > entry.target_ms ? 'latency-over' : ''}>{entry.total_ms > entry.target_ms ? '超出' : '不超过'} {seconds(entry.target_ms)} 参照</small>}<span className="latency-toggle">明细</span></summary>
        {entry.parts.length ? <table className="latency-table"><caption>第 {entry.sequence} 次输出的阶段统计</caption><thead><tr><th scope="col">阶段</th><th scope="col">耗时</th><th scope="col">占比</th></tr></thead><tbody>{entry.parts.map((part, index) => <tr key={`${part.stage}-${index}`}><th scope="row">{labels[part.stage] ?? `其他处理（${part.stage}）`}</th><td>{milliseconds(part.duration_ms)}</td><td>{entry.total_ms > 0 ? `${(part.duration_ms / entry.total_ms * 100).toFixed(1)}%` : '—'}</td></tr>)}</tbody><tfoot><tr><th scope="row">服务端合计</th><td>{milliseconds(entry.total_ms)}</td><td>{entry.total_ms > 0 ? '100%' : '—'}</td></tr></tfoot></table> : <p>此轮未记录阶段明细。</p>}
        <p>{entry.kind === 'opening' ? '从连接就绪到服务端开始输出。' : '从用户停口到服务端开始输出。'}GPU 链路统计在 GPU 内计算；不包含之后到网页扬声器的传输和播放。终端首段有效声音需另行实测。</p>
      </details>)}
    </div>
  </section>;
}
