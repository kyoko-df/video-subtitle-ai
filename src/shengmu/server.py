from __future__ import annotations

import logging
import secrets
import shutil
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import __version__
from .diagnostics import doctor, redacted_traceback
from .engines import EngineOptions
from .exporters import FORMATS, export_files
from .media import MediaInfo, probe
from .models import Cancelled, Segment, SubtitleError, Transcript
from .pipeline import transcribe

WEB = Path(__file__).with_name("web")
TERMINAL = {"done", "error", "cancelled"}
logger = logging.getLogger(__name__)


class JobRequest(BaseModel):
    media_id: str
    engine: Literal["local", "openai"] = "local"
    model: Literal["tiny", "base", "small", "medium", "large-v3", "turbo"] = "small"
    language: str | None = Field(default=None, max_length=20)
    track: int | None = None
    device: Literal["cpu", "cuda", "auto"] = "cpu"
    compute_type: Literal["int8", "float32", "float16", "int8_float16", "auto"] = "int8"
    prompt: str = Field(default="", max_length=2000)
    condition_on_previous_text: bool = False
    filter_hallucinations: bool = True
    formats: list[Literal["srt", "vtt", "ass", "txt", "json"]] = Field(
        default_factory=lambda: ["srt"]
    )


class CueRequest(BaseModel):
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(gt=0, allow_inf_nan=False)
    text: str = Field(min_length=1, max_length=10000)


class EditRequest(BaseModel):
    segments: list[CueRequest] = Field(max_length=100000)
    formats: list[Literal["srt", "vtt", "ass", "txt", "json"]] = Field(
        default_factory=lambda: ["srt"]
    )


@dataclass
class Media:
    path: Path
    name: str
    info: MediaInfo
    size: int = 0
    created: float = field(default_factory=time.monotonic)


@dataclass
class Job:
    id: str
    media: Media
    directory: Path
    request: JobRequest
    status: str = "queued"
    progress: float = 0
    message: str = "任务排队中…"
    transcript: Transcript | None = None
    files: list[Path] = field(default_factory=list)
    cancel: threading.Event = field(default_factory=threading.Event)
    updated: float = field(default_factory=time.monotonic)

    def public(self) -> dict:
        return {
            "id": self.id,
            "source": self.media.name,
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "segments": len(self.transcript.segments) if self.transcript else 0,
            "files": [
                {
                    "format": path.suffix[1:],
                    "name": path.name,
                    "url": f"/api/jobs/{self.id}/files/{path.suffix[1:]}",
                }
                for path in self.files
            ],
        }


class Workspace:
    def __init__(self, root: Path, max_upload: int, max_storage: int):
        self.root = root
        self.max_upload = max_upload
        self.max_storage = max_storage
        self.upload_sizes: dict[str, int] = {}
        self.media: dict[str, Media] = {}
        self.jobs: dict[str, Job] = {}
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="shengmu")

    def get_job(self, job_id: str) -> Job:
        with self.lock:
            job = self.jobs.get(job_id)
        if not job:
            raise HTTPException(404, "任务不存在，或服务已重新启动。")
        return job

    def release_media(self, media_id: str) -> None:
        with self.lock:
            media = self.media.get(media_id)
            if not media:
                raise HTTPException(404, "文件不存在，或已经释放。")
            if any(job.media is media and job.status not in TERMINAL for job in self.jobs.values()):
                raise HTTPException(409, "文件仍用于排队或处理中的任务，暂时不能删除。")
            shutil.rmtree(media.path.parent)
            del self.media[media_id]

    def cleanup(self) -> None:
        # Session results expire after 24 hours. Only unreferenced uploads can be removed.
        cutoff = time.monotonic() - 86400
        with self.lock:
            for job_id, job in list(self.jobs.items()):
                if job.status in TERMINAL and job.updated < cutoff:
                    shutil.rmtree(job.directory, ignore_errors=True)
                    del self.jobs[job_id]
            referenced = {
                job.media.path for job in self.jobs.values() if job.status not in TERMINAL
            }
            for media_id, media in list(self.media.items()):
                if media.created < cutoff and media.path not in referenced:
                    shutil.rmtree(media.path.parent, ignore_errors=True)
                    del self.media[media_id]

    def process(self, job: Job) -> None:
        def update(stage: str, amount: float, message: str) -> None:
            with self.lock:
                job.status, job.progress, job.message = stage, amount, message
                job.updated = time.monotonic()

        try:
            r = job.request
            result = transcribe(
                job.media.path,
                EngineOptions(
                    r.engine,
                    r.model,
                    r.language,
                    r.device,
                    r.compute_type,
                    r.prompt,
                    condition_on_previous_text=r.condition_on_previous_text,
                    filter_hallucinations=r.filter_hallucinations,
                ),
                r.track,
                update,
                job.cancel,
            )
            result.source = job.media.name
            with self.lock:
                if job.cancel.is_set():
                    raise Cancelled("任务已取消。")
                update("export", 0.97, "正在导出字幕…")
                job.files = export_files(
                    result, job.directory, Path(job.media.name).stem, r.formats
                )
                job.transcript = result
                update(
                    "done",
                    1.0,
                    f"完成，共 {len(result.segments)} 条字幕。"
                    if result.segments
                    else "完成，未检测到语音。可尝试切换音轨或识别语言。",
                )
        except Cancelled:
            update("cancelled", job.progress, "任务已取消。")
        except SubtitleError as exc:
            logger.error("转写任务 %s 失败\n%s", job.id, redacted_traceback())
            update("error", job.progress, str(exc))
        except Exception:
            logger.error("转写任务 %s 异常\n%s", job.id, redacted_traceback())
            update("error", job.progress, "任务失败，请检查依赖、文件权限和媒体格式。")

    def close(self) -> None:
        for job in self.jobs.values():
            job.cancel.set()
        # Model load/in-flight SDK requests are cooperative and may need time to return.
        self.executor.shutdown(wait=True, cancel_futures=True)


def create_app(max_upload: int = 2 * 1024**3, max_storage: int | None = None) -> FastAPI:
    max_storage = max_upload * 2 if max_storage is None else max_storage
    token = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        with tempfile.TemporaryDirectory(prefix="shengmu-gui-") as directory:
            workspace = Workspace(Path(directory), max_upload, max_storage)
            app.state.workspace = workspace
            yield
            await run_in_threadpool(workspace.close)

    app = FastAPI(
        title="声幕", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None
    )

    @app.middleware("http")
    async def local_session(request: Request, call_next):
        try:
            authority = urlsplit("//" + request.headers.get("host", ""))
            host = authority.hostname
            valid = not (
                authority.username
                or authority.password
                or authority.path
                or authority.query
                or authority.fragment
            )
            valid = valid and (authority.port is None or 1 <= authority.port <= 65535)
        except ValueError:
            host, valid = None, False
        if not valid or host not in {"127.0.0.1", "localhost", "::1", "testserver"}:
            return JSONResponse({"detail": "仅允许本地访问。"}, status_code=403)
        if request.url.path.startswith("/api/") and request.method not in {"GET", "HEAD"}:
            if not secrets.compare_digest(request.headers.get("x-session-token", ""), token):
                return JSONResponse({"detail": "会话已失效，请刷新页面。"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/config")
    def config():
        return {
            "token": token,
            "formats": FORMATS,
            "version": __version__,
            "max_upload": max_upload,
            "max_storage": max_storage,
            **doctor(),
        }

    @app.post("/api/media")
    async def upload(request: Request, name: str):
        workspace = app.state.workspace
        await run_in_threadpool(workspace.cleanup)
        # Filename is used only inside a unique workspace directory.
        filename = Path(name.replace("\\", "/")).name
        if not filename or filename in {".", ".."} or len(filename.encode("utf-8")) > 240:
            raise HTTPException(400, "文件名无效或过长。")
        if any(ord(char) < 32 for char in filename):
            raise HTTPException(400, "文件名包含无效字符。")
        media_id = uuid.uuid4().hex
        with workspace.lock:
            if len(workspace.media) + len(workspace.upload_sizes) >= 30:
                raise HTTPException(429, "本次会话已达到 30 个文件，请释放旧文件或重启服务。")
            workspace.upload_sizes[media_id] = 0
        folder = workspace.root / media_id
        path = folder / filename
        size = 0
        try:
            folder.mkdir()
            with path.open("wb") as stream:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > max_upload:
                        raise HTTPException(413, "文件过大。请使用 CLI 直接处理本地文件。")
                    with workspace.lock:
                        used = sum(m.size for m in workspace.media.values()) + sum(
                            workspace.upload_sizes.values()
                        )
                        if used + len(chunk) > max_storage:
                            raise HTTPException(413, "会话上传空间不足，请释放旧文件或使用 CLI。")
                        workspace.upload_sizes[media_id] = size
                    await run_in_threadpool(stream.write, chunk)
            if size == 0:
                raise HTTPException(400, "文件为空。")
            info = await run_in_threadpool(probe, path)
            if await request.is_disconnected():
                raise HTTPException(409, "上传已取消。")
            with workspace.lock:
                workspace.media[media_id] = Media(path, filename, info, size)
                workspace.upload_sizes.pop(media_id, None)
            return {"id": media_id, "name": filename, "size": size, **info.to_dict()}
        except BaseException as exc:
            await run_in_threadpool(shutil.rmtree, folder, ignore_errors=True)
            if isinstance(exc, SubtitleError):
                raise HTTPException(400, str(exc)) from exc
            raise
        finally:
            with workspace.lock:
                workspace.upload_sizes.pop(media_id, None)

    @app.delete("/api/media/{media_id}")
    def delete_media(media_id: str):
        app.state.workspace.release_media(media_id)
        return {"id": media_id, "deleted": True}

    @app.post("/api/jobs", status_code=202)
    def start_job(data: JobRequest):
        workspace = app.state.workspace
        workspace.cleanup()
        with workspace.lock:
            media = workspace.media.get(data.media_id)
            if not media:
                raise HTTPException(404, "文件不存在，请重新选择。")
            if not data.formats:
                raise HTTPException(400, "请至少选择一种字幕格式。")
            if data.track is not None and data.track not in {
                t.index for t in media.info.audio_tracks
            }:
                raise HTTPException(400, "所选音轨不存在。")
            if sum(j.status not in TERMINAL for j in workspace.jobs.values()) >= 5:
                raise HTTPException(429, "最多允许 5 个任务等待或处理。")
            if len(workspace.jobs) >= 100:
                raise HTTPException(429, "本次会话已达到 100 个任务，请下载结果后重启服务。")
            job_id = uuid.uuid4().hex
            job = Job(job_id, media, workspace.root / job_id, data)
            workspace.jobs[job_id] = job
            workspace.executor.submit(workspace.process, job)
            return job.public()

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str):
        workspace = app.state.workspace
        with workspace.lock:
            return workspace.get_job(job_id).public()

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str):
        workspace = app.state.workspace
        with workspace.lock:
            job = workspace.get_job(job_id)
            if job.status not in TERMINAL:
                job.cancel.set()
                job.message = "正在取消，将在当前识别片段或请求结束后停止…"
            return job.public()

    @app.get("/api/jobs/{job_id}/transcript")
    def get_transcript(job_id: str):
        job = app.state.workspace.get_job(job_id)
        if job.status != "done" or job.transcript is None:
            raise HTTPException(409, "字幕尚未生成。")
        return job.transcript.to_dict()

    @app.put("/api/jobs/{job_id}/transcript")
    def edit_transcript(job_id: str, data: EditRequest):
        workspace = app.state.workspace
        with workspace.lock:
            job = workspace.get_job(job_id)
            if job.status != "done" or job.transcript is None:
                raise HTTPException(409, "只能编辑已完成任务的字幕。")
            try:
                result = replace(
                    job.transcript,
                    segments=[Segment(c.start, c.end, c.text) for c in data.segments],
                )
                job.files = export_files(
                    result, job.directory, Path(job.media.name).stem, data.formats, overwrite=True
                )
            except SubtitleError as exc:
                raise HTTPException(400, str(exc)) from exc
            job.transcript = result
            job.updated = time.monotonic()
            return job.public()

    @app.get("/api/jobs/{job_id}/files/{fmt}")
    def download(job_id: str, fmt: str):
        workspace = app.state.workspace
        with workspace.lock:
            job = workspace.get_job(job_id)
            path = next((p for p in job.files if p.suffix == f".{fmt}"), None)
            if job.status != "done" or path is None or not path.is_file():
                raise HTTPException(404, "字幕文件不存在。")
            return FileResponse(path, filename=path.name, media_type="application/octet-stream")

    app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
    return app
