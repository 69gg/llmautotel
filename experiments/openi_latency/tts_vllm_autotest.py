"""在独立 GPU 任务中补测 Qwen 基线与隔离环境的 CosyVoice3 vLLM。"""

from __future__ import annotations

if __package__:
    from .tts_autotest import main
else:
    from tts_autotest import main


if __name__ == "__main__":
    main(["--vllm-only"])
