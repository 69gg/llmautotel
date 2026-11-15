"""低延迟实验的纯 Python 组件；不导入 Torch，不加载权重。"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import textwrap
from dataclasses import dataclass, field
from types import MethodType
from typing import Any

if __package__:
    from .protocol import signal_offset_ms
else:
    from protocol import signal_offset_ms


@dataclass
class LeadingSilenceFilter:
    """真实裁剪待发送 PCM，保留前沿，不能只从计时结果中减去静音。"""

    sample_rate: int
    keep_ms: int = 40
    max_trim_ms: int = 400
    threshold: int = 100
    removed_bytes: int = 0
    started: bool = False
    pending: bytearray = field(default_factory=bytearray)

    def __post_init__(self) -> None:
        if self.sample_rate <= 0 or not 0 <= self.keep_ms <= self.max_trim_ms:
            raise ValueError("invalid_silence_filter")

    def feed(self, pcm: bytes) -> bytes:
        if len(pcm) % 2:
            raise ValueError("unaligned_output_pcm")
        if self.started:
            return pcm
        self.pending.extend(pcm)
        offset = signal_offset_ms(bytes(self.pending), self.sample_rate, self.threshold)
        limit = self.max_trim_ms * self.sample_rate // 1000 * 2
        if offset is None and len(self.pending) <= limit:
            return b""
        discard = (
            limit
            if offset is None
            else min(limit, max(0, round((offset - self.keep_ms) * self.sample_rate / 1000) * 2))
        )
        self.removed_bytes = discard
        self.started = True
        result = bytes(self.pending[discard:])
        self.pending.clear()
        return result

    def finish(self) -> bytes:
        result = bytes(self.pending)
        self.pending.clear()
        return result

    @property
    def removed_ms(self) -> float:
        return self.removed_bytes / (self.sample_rate * 2) * 1000


class ReplyAudioCache:
    """仅精确匹配完整回复；价格、音色或资料快照变化时不能命中。"""

    def __init__(self, snapshot: dict[str, Any]) -> None:
        self.fingerprint = self.snapshot_key(snapshot)
        self.entries: dict[str, bytes] = {}

    @staticmethod
    def snapshot_key(snapshot: dict[str, Any]) -> str:
        serialized = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode()).hexdigest()

    def put(self, text: str, pcm: bytes) -> None:
        if not text.strip() or not pcm or len(pcm) % 2:
            raise ValueError("invalid_cached_reply")
        self.entries[text.strip()] = pcm

    def get(self, text: str, snapshot: dict[str, Any]) -> bytes | None:
        if self.snapshot_key(snapshot) != self.fingerprint:
            return None
        return self.entries.get(text.strip())


def bind_streaming_chunk_options(model: Any) -> dict[str, Any]:
    """仅改内存中的锁定上游方法，使文字块和实际 PCM 块可一起对照。

    上游外层 CHUNK_SIZE 写死，单传 audio_token_chunk_size 并不会缩小 PCM 块。
    AST 只允许修改两个已知赋值；源码变化时失败，不改共享文件或挂载权重。
    """
    original = type(model).streaming_generate
    source = textwrap.dedent(inspect.getsource(original))
    tree = ast.parse(source)
    changes: dict[str, int] = {"CHUNK_SIZE": 0, "generate_chunk_size": 0}
    defaults = {"CHUNK_SIZE": 25, "generate_chunk_size": 10}
    attributes = {
        "CHUNK_SIZE": "_latency_audio_tokens",
        "generate_chunk_size": "_latency_text_tokens",
    }

    class Options(ast.NodeTransformer):
        def visit_Assign(self, node: ast.Assign) -> ast.AST:
            if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                name = node.targets[0].id
                if name in defaults:
                    if (
                        not isinstance(node.value, ast.Constant)
                        or node.value.value != defaults[name]
                    ):
                        raise ValueError("upstream_chunk_assignment_changed")
                    changes[name] += 1
                    node.value = ast.Call(
                        func=ast.Name(id="getattr", ctx=ast.Load()),
                        args=[
                            ast.Name(id="self", ctx=ast.Load()),
                            ast.Constant(attributes[name]),
                            node.value,
                        ],
                        keywords=[],
                    )
            return self.generic_visit(node)

    tree = ast.fix_missing_locations(Options().visit(tree))
    if set(changes.values()) != {1}:
        raise ValueError("upstream_chunk_assignments_not_unique")
    namespace = dict(original.__globals__)
    exec(compile(tree, original.__code__.co_filename, "exec"), namespace)
    model.streaming_generate = MethodType(namespace[original.__name__], model)
    return {
        "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "modified_assignments": changes,
    }
