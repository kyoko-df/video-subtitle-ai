from __future__ import annotations

import os
from contextlib import contextmanager

from .models import SubtitleError


@contextmanager
def workspace_lock(root):
    """Keep two server processes from independently editing the same manifests."""
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".lock").open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        try:
            if os.name == "nt":
                import msvcrt

                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise SubtitleError("该项目目录已有声幕服务在运行，请使用另一端口和项目目录。") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)
