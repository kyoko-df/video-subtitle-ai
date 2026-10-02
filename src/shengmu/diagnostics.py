from __future__ import annotations

import importlib.util
import os
import re
import shutil
import sys
import traceback


def doctor() -> dict:
    return {
        "python": sys.version.split()[0],
        "ffmpeg": shutil.which(os.environ.get("FFMPEG_BINARY", "ffmpeg")),
        "ffprobe": shutil.which(os.environ.get("FFPROBE_BINARY", "ffprobe")),
        "local_engine_installed": importlib.util.find_spec("faster_whisper") is not None,
        "openai_engine_installed": importlib.util.find_spec("openai") is not None,
        "openai_key_configured": bool(os.environ.get("OPENAI_API_KEY", "").strip()),
    }


def redacted_traceback() -> str:
    text = traceback.format_exc()
    for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL"):
        value = os.environ.get(name, "").strip()
        if value:
            text = text.replace(value, "[redacted]")
    text = re.sub(r"https?://[^\s\"'<>]+", "[redacted URL]", text)
    return re.sub(r"\bsk-[A-Za-z0-9_-]+", "[redacted key]", text)
