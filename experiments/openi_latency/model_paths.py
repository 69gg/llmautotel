"""解析站内预训练模型与镜像缓存，缺文件时失败而不回退外网下载。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def validate_model(path: Path, patterns: list[str]) -> str:
    if not path.is_dir():
        raise FileNotFoundError(f"model_directory_missing: {path}")
    for pattern in patterns:
        files = [item for item in path.glob(pattern) if item.is_file()]
        if not files or any(item.stat().st_size == 0 for item in files):
            raise FileNotFoundError(f"model_files_missing: {path.name}/{pattern}")
    for index in path.glob("*.safetensors.index.json"):
        shards = set(json.loads(index.read_text())["weight_map"].values())
        for shard in shards:
            file = path / shard
            if not file.is_file() or file.stat().st_size == 0:
                raise FileNotFoundError(f"model_shard_missing: {path.name}/{shard}")
    return str(path.resolve())


def resolve_models(config: dict[str, Any], pretrain_root: Path | None) -> dict[str, str]:
    resolved: dict[str, str] = {}
    required = config.get("required_model_files", {})
    for role, name in config.get("mounted_models", {}).items():
        if pretrain_root is None:
            raise ValueError("openi_pretrain_root_missing")
        path = pretrain_root / name
        if not path.resolve().is_relative_to(pretrain_root.resolve()):
            raise ValueError("model_path_outside_pretrain_root")
        resolved[role] = validate_model(path, required.get(role, []))
    for role, pattern in config.get("cached_models", {}).items():
        matches: list[Path] = []
        for root in config.get("model_cache_roots", []):
            matches.extend(path for path in Path(root).expanduser().glob(pattern) if path.is_dir())
        paths = list(dict.fromkeys(path.resolve() for path in matches))
        if len(paths) != 1:
            raise ValueError(f"cached_model_expected_one: {role}, found={len(paths)}")
        resolved[role] = validate_model(paths[0], required.get(role, []))
    if not config.get("allow_model_downloads", True) and set(resolved) != {"asr", "llm", "tts"}:
        raise ValueError("offline_models_require_all_three_local_paths")
    return resolved


def main() -> None:
    root = Path(__file__).resolve().parent
    config = json.loads((root / "settings.run.json").read_text())
    pretrain_root: Path | None = None
    if config.get("mounted_models"):
        from c2net.context import prepare

        context = prepare()
        pretrain_root = Path(context.pretrain_model_path)
    paths = resolve_models(config, pretrain_root)
    (root / "model-paths.run.json").write_text(json.dumps(paths))
    print("local_models_verified " + json.dumps(paths), flush=True)


if __name__ == "__main__":
    main()
