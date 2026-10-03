from __future__ import annotations

import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .models import SubtitleError


def copy_exclusive(source: Path, target: Path):
    """Copy without replacing another writer's file; clean up our own failed copy."""
    with source.open("rb") as reader:
        writer = target.open("xb")
        identity = os.fstat(writer.fileno())
        try:
            with writer:
                shutil.copyfileobj(reader, writer)
                writer.flush()
        except BaseException:
            # Close before unlinking for Windows. Do not remove a replacement made by
            # another process while the copy was running.
            try:
                writer.close()
            except OSError:
                pass
            try:
                current = target.lstat()
                if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                    target.unlink()
            except OSError:
                pass
            raise


def atomic_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".save-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


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
