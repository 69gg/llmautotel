"""OpenI 补测入口：复用已验证镜像 CUDA，比较 Qwen 与 VoxCPM1.5。"""

from __future__ import annotations

if __package__:
    from .tts_autotest import main
else:
    from tts_autotest import main


if __name__ == "__main__":
    main(["--vox-only"])
