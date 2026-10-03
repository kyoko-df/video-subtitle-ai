from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .models import SubtitleError
from .storage import copy_exclusive


def output_capabilities(directory: Path) -> dict:
    """Probe this filesystem with only our temporary files; never cache mount state."""
    directory = Path(directory).expanduser().resolve()
    result = {
        "directory": str(directory),
        "writable": False,
        "atomic_replace": False,
        "hard_link": False,
        "exclusive_create": False,
    }
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".shengmu-probe-", dir=directory) as temporary:
            root = Path(temporary)
            source = root / "source"
            source.write_bytes(b"shengmu output probe\n")
            result["writable"] = True
            replaced = root / "replaced"
            replaced.write_bytes(b"old")
            try:
                os.replace(source, replaced)
                result["atomic_replace"] = replaced.read_bytes() == b"shengmu output probe\n"
                source = replaced
            except OSError:
                pass
            try:
                os.link(source, root / "linked")
                result["hard_link"] = True
            except OSError:
                pass
            try:
                target = root / "exclusive"
                copy_exclusive(source, target)
                try:
                    copy_exclusive(source, target)
                except FileExistsError:
                    result["exclusive_create"] = target.read_bytes() == source.read_bytes()
            except OSError:
                pass
    except OSError:
        result["writable"] = False
        result["note"] = "输出目录无法写入或探测失败，请检查权限、可用空间和共享连接。"
        return result
    if not result["hard_link"] and result["exclusive_create"]:
        result["note"] = (
            "目录不支持硬链接；不覆盖导出将使用独占创建，复制过程中可能暂时看到不完整文件。"
        )
    elif not result["hard_link"]:
        result["note"] = "目录不支持安全的不覆盖导出，请更换输出目录。"
    elif not result["atomic_replace"]:
        result["note"] = "目录不支持文件替换，无法使用覆盖导出。"
    else:
        result["note"] = "输出目录可用。"
    return result


def ensure_output_directory(directory: Path, overwrite=False):
    capabilities = output_capabilities(directory)
    if not capabilities["writable"]:
        raise SubtitleError(capabilities["note"])
    if overwrite and not capabilities["atomic_replace"]:
        raise SubtitleError("输出目录不支持文件替换，请更换目录后再使用覆盖导出。")
    if not overwrite and not (capabilities["hard_link"] or capabilities["exclusive_create"]):
        raise SubtitleError("输出目录不支持安全的不覆盖导出，请检查权限或更换目录。")
    return capabilities


@lru_cache(maxsize=8)
def ffmpeg_capabilities(executable, modified):
    try:
        available = {}
        for option in ("-filters", "-encoders"):
            result = subprocess.run(
                [executable, "-hide_banner", option], capture_output=True, text=True, timeout=5
            )
            if result.returncode:
                raise OSError("ffmpeg capability check failed")
            available[option] = {
                fields[1] for line in result.stdout.splitlines() if len(fields := line.split()) >= 2
            }
        missing = [name for name in ("ass",) if name not in available["-filters"]]
        missing += [name for name in ("libx264", "aac") if name not in available["-encoders"]]
        return {
            "burn_supported": not missing,
            "soft_supported": {"aac", "mov_text"} <= available["-encoders"],
            "missing_burn": missing,
            "checked": True,
        }
    except (OSError, subprocess.SubprocessError):
        return {
            "burn_supported": False,
            "soft_supported": False,
            "missing_burn": [],
            "checked": False,
        }


def doctor(network=False, output_dir: Path | None = None) -> dict:
    executable = shutil.which(os.environ.get("FFMPEG_BINARY", "ffmpeg"))
    capabilities = {
        "burn_supported": False,
        "soft_supported": False,
        "missing_burn": ["ffmpeg"],
        "checked": False,
    }
    if executable:
        try:
            capabilities = ffmpeg_capabilities(executable, os.stat(executable).st_mtime_ns)
        except OSError:
            pass
    result = {
        "python": sys.version.split()[0],
        "ffmpeg": executable,
        "ffprobe": shutil.which(os.environ.get("FFPROBE_BINARY", "ffprobe")),
        "local_engine_installed": importlib.util.find_spec("faster_whisper") is not None,
        "openai_engine_installed": importlib.util.find_spec("openai") is not None,
        "openai_key_configured": bool(os.environ.get("OPENAI_API_KEY", "").strip()),
        "ffmpeg_capabilities": capabilities,
        "ort_telemetry_disabled": os.environ.get("ORT_DISABLE_TELEMETRY") == "1",
    }
    if output_dir is not None:
        result["output_capabilities"] = output_capabilities(output_dir)
    if network:
        endpoint = os.environ.get("HF_ENDPOINT", "https://huggingface.co")
        try:
            parsed = urlsplit(endpoint)
        except ValueError:
            parsed = urlsplit("")
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            result["huggingface"] = {"reachable": False, "message": "HF_ENDPOINT 地址无效。"}
        else:
            try:
                with urlopen(Request(endpoint, method="HEAD"), timeout=2) as response:
                    reachable = response.status < 400
                result["huggingface"] = {"reachable": reachable}
            except (OSError, ValueError):
                result["huggingface"] = {
                    "reachable": False,
                    "message": "模型下载服务探测失败；已有本地模型仍可使用。",
                }
        from . import lmstudio
        from .models import SubtitleError

        try:
            result["lmstudio"] = {"reachable": True, "text_models": len(lmstudio.models())}
        except SubtitleError as exc:
            result["lmstudio"] = {"reachable": False, "message": str(exc)}
    return result


def redacted_traceback() -> str:
    text = traceback.format_exc()
    for name in (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "HF_TOKEN",
        "LM_STUDIO_API_KEY",
        "LM_STUDIO_BASE_URL",
        "HF_ENDPOINT",
    ):
        value = os.environ.get(name, "").strip()
        if value:
            text = text.replace(value, "[redacted]")
    text = re.sub(r"https?://[^\s\"'<>]+", "[redacted URL]", text)
    return re.sub(r"\b(?:sk-|hf_)[A-Za-z0-9_-]+", "[redacted key]", text)
