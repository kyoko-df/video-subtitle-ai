from __future__ import annotations

import os
from pathlib import Path

from .models import SubtitleError

MODELS = ("tiny", "base", "small", "medium", "large-v3", "turbo")
REPOS = {name: "Systran/faster-whisper-" + name for name in MODELS}
REPOS["turbo"] = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"


def model_root(workspace):
    return (
        Path(os.environ.get("SHENGMU_MODEL_DIR", str(workspace / "models"))).expanduser().resolve()
        / "managed"
    )


def model_path(root, name):
    if name not in MODELS:
        raise SubtitleError("不支持的模型。")
    return root / name


def model_inventory(root):
    results = []
    for name in MODELS:
        path = model_path(root, name)
        downloaded = (
            (path / "model.bin").is_file()
            and (path / "config.json").is_file()
            and ((path / "tokenizer.json").is_file() or any(path.glob("vocabulary.*")))
            and (path / ".complete").is_file()
        )
        cached = None
        if not downloaded:
            try:
                from faster_whisper.utils import download_model

                cached = download_model(
                    name, local_files_only=True, cache_dir=os.environ.get("SHENGMU_MODEL_DIR")
                )
            except (ImportError, OSError, ValueError):
                pass
        size = sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) if path.exists() else 0
        if cached and not downloaded:
            size = sum(p.stat().st_size for p in Path(cached).iterdir() if p.is_file())
        results.append(
            {
                "name": name,
                "downloaded": downloaded or cached is not None,
                "managed": downloaded,
                "size": size,
                "path": str(path) if downloaded or not cached else cached,
            }
        )
    return results


def download(root, name, update, cancel=None):
    from .media import check_cancel

    try:
        from huggingface_hub import HfApi, hf_hub_download
    except ImportError as exc:
        raise SubtitleError('请先安装本地引擎依赖：pip install -e ".[local]"。') from exc
    path = model_path(root, name)
    check_cancel(cancel)
    try:
        info = HfApi().model_info(REPOS[name], files_metadata=True)
        files = [
            f
            for f in info.siblings
            if f.rfilename
            in {"config.json", "preprocessor_config.json", "model.bin", "tokenizer.json"}
            or f.rfilename.startswith("vocabulary.")
        ]
        total = sum(f.size or 0 for f in files)
        path.mkdir(parents=True, exist_ok=True)
        update(0, total, "正在下载模型…")
        done = 0
        for f in files:
            check_cancel(cancel)
            hf_hub_download(REPOS[name], f.rfilename, revision=info.sha, local_dir=path)
            done += f.size or 0
            update(done, total, f"已下载 {f.rfilename}")
        check_cancel(cancel)
        (path / ".complete").write_text(info.sha or "complete", encoding="utf-8")
        update(total, total, "模型下载完成")
    except SubtitleError:
        raise
    except Exception as exc:
        raise SubtitleError("模型下载失败，请检查网络和磁盘空间，可重试续传。") from exc
