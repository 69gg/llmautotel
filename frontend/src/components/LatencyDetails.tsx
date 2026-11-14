import type { TurnLatency } from '../types';

const labels: Record<string, string> = {
  endpointing_wait: '停口确认', transcription: '语音识别', turn_detection: '回合判断',
  llm_inference: '文本生成', sentence_aggregation: '分句等待', speech_synthesis: '分句与合成',
  output_transport: '输出排队', pipeline: '管线处理', first_request: '开场准备',
  llm_tool_call: '工具参数生成', function_handler: '工具处理', turn_completion: '回合确认',
  waiting_for_user: '等待用户', awaiting_speakable_text: '正文等待',
};
const seconds = (ms: number) => `${(ms / 1000).toFixed(2)} 秒`;

export function LatencyDetails({ entries, latestOnly = false }: { entries: TurnLatency[]; latestOnly?: boolean }) {
  if (!entries.length) return null;
  const shown = latestOnly ? entries.slice(-1) : entries;
  return <section className="latency-panel" aria-label="响应耗时">
    {shown.map(entry => <details className="latency-entry" key={entry.sequence}>
      <summary><span>{entry.kind === 'opening' ? '开场 · 服务端首音' : '回复 · 服务端首音'}</span><strong>{seconds(entry.total_ms)}</strong>{entry.kind === 'reply' && <small className={entry.total_ms > entry.target_ms ? 'latency-over' : ''}>{entry.total_ms > entry.target_ms ? '超出' : '不超过'} {seconds(entry.target_ms)} 参照</small>}</summary>
      <dl>{entry.parts.map((part, index) => <div key={`${part.stage}-${index}`}><dt>{labels[part.stage] ?? '其他处理'}</dt><dd>{seconds(part.duration_ms)}</dd></div>)}</dl>
      <p>从停口到服务端输出。开场从连接就绪起算；终端首段有效声音需另行实测。</p>
    </details>)}
  </section>;
}
