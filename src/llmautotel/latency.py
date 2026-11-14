"""将 Pipecat 官方逐轮首音耗时转换为不含模型上下文的公开记录。"""

from datetime import UTC, datetime

from pipecat.observers.user_bot_latency_observer import LatencyBreakdown, MeasuredFrom

from llmautotel.models import LatencyPart, TurnLatency


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
        parts=[
            LatencyPart(stage=part.key, duration_ms=round(part.duration_secs * 1000, 1))
            for part in breakdown.contributions
        ],
    )
