from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import traceback
from functools import lru_cache
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


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


def doctor(network=False) -> dict:
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
