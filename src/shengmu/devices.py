from __future__ import annotations

import importlib.util
import platform
import sys
from functools import lru_cache
from subprocess import SubprocessError, run


@lru_cache(maxsize=1)
def metal_available() -> bool:
    # A broken / sandboxed Metal runtime can abort on import. Probe in a child
    # so environment diagnostics never take down the GUI or a CPU-only job.
    try:
        result = run(
            [sys.executable, "-c", "import mlx.core as mx; print(int(mx.metal.is_available()))"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0 and result.stdout.strip() == "1"
    except (OSError, SubprocessError):
        return False


def mlx_status() -> dict:
    supported = sys.platform == "darwin" and platform.machine().lower() == "arm64"
    installed = importlib.util.find_spec("mlx_whisper") is not None
    result = {"supported": supported, "installed": installed, "available": False}
    if not supported:
        result["message"] = "Mac GPU 转写需要 Apple Silicon Mac 和原生 arm64 Python。"
    elif not installed:
        result["message"] = '请安装 Mac GPU 依赖：pip install -e ".[mlx]"。'
    else:
        result["available"] = metal_available()
        result["message"] = (
            "Apple GPU / Metal 可用。"
            if result["available"]
            else "Metal GPU 不可用，请检查 macOS、运行权限及 MLX 安装。"
        )
    return result


def local_backend(device: str) -> str:
    if device == "mlx" or (device == "auto" and mlx_status()["available"]):
        return "mlx"
    return "faster-whisper"
