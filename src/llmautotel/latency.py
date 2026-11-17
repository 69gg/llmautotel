"""将 Pipecat 官方逐轮首音耗时转换为不含模型上下文的公开记录。"""

from datetime import UTC, datetime

from pipecat.observers.user_bot_latency_observer import LatencyBreakdown, MeasuredFrom

from llmautotel.models import LatencyPart, TurnLatency


def latency_parts(breakdown: LatencyBreakdown) -> list[LatencyPart]:
    """使用 TTS 自身的请求起点拆开分句等待，不回推或伪造旧记录。"""
    result: list[LatencyPart] = []
    for part in breakdown.contributions:
        requests = [
            metric
            for metric in breakdown.ttfb
            if metric.processor == part.owner
            and part.start_time <= metric.start_time < part.start_time + part.duration_secs
        ]
        if part.key == "speech_synthesis" and requests:
            request_at = min(metric.start_time for metric in requests)
            waiting_ms = round((request_at - part.start_time) * 1000, 1)
            total_ms = round(part.duration_secs * 1000, 1)
            if waiting_ms > 0:
                result.append(LatencyPart(stage="sentence_aggregation", duration_ms=waiting_ms))
            result.append(
                LatencyPart(stage="tts_inference", duration_ms=round(total_ms - waiting_ms, 1))
            )
        else:
            result.append(
                LatencyPart(stage=part.key, duration_ms=round(part.duration_secs * 1000, 1))
            )
    return result


def turn_latency(
    breakdown: LatencyBreakdown, *, sequence: int, target_ms: int
) -> TurnLatency | None:
    """统计到服务端开始输出；终端首个有效声音须另外实测。"""
    if breakdown.measured_from is None or not breakdown.contributions:
        return None
    return TurnLatency(
        sequence=sequence,
        kind="reply" if breakdown.measured_from == MeasuredFrom.USER_SILENCE else "opening",
        timestamp=datetime.now(UTC).isoformat(),
        total_ms=round(breakdown.total_secs * 1000, 1),
        target_ms=target_ms,
        parts=latency_parts(breakdown),
    )
