from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import tempfile
import threading
import time
import uuid
import zipfile
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
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from . import __version__, model_manager
from .captions import CaptionOptions, quality_report
from .diagnostics import doctor, redacted_traceback
from .engines import EngineOptions
from .exporters import FORMATS, export_files
from .media import MediaInfo, check_cancel, probe
from .models import Cancelled, Segment, SubtitleError, Transcript, Word
from .pipeline import transcribe
from .storage import workspace_lock
from .translation import translate
from .video import export_video, waveform

WEB = Path(__file__).with_name("web")
TERMINAL = {"done", "error", "cancelled"}
logger = logging.getLogger(__name__)


class CaptionRequest(BaseModel):
    cjk_chars: int = Field(default=20, ge=5, le=100)
    latin_chars: int = Field(default=42, ge=5, le=200)
    max_lines: int = Field(default=2, ge=1, le=4)
    max_seconds: float = Field(default=7, ge=0.5, le=30, allow_inf_nan=False)
    min_seconds: float = Field(default=1, ge=0, le=30, allow_inf_nan=False)
    max_cps: float = Field(default=20, ge=1, le=100, allow_inf_nan=False)


class StyleRequest(BaseModel):
    font: str = Field(default="Arial", min_length=1, max_length=80, pattern=r"^[^,\r\n{}\\]+$")
    size: int = Field(default=54, ge=12, le=200)
    color: str = Field(default="#FFFFFF", pattern=r"^#[0-9A-Fa-f]{6}$")
    position: Literal["bottom", "middle", "top"] = "bottom"
    margin: int = Field(default=60, ge=0, le=500)


class ExportSettings(BaseModel):
    captions: CaptionRequest = Field(default_factory=CaptionRequest)
    style: StyleRequest = Field(default_factory=StyleRequest)
    export_mode: Literal["original", "translated", "bilingual"] = "original"
    speaker_labels: bool = False
    word_highlight: bool = False
    allow_overlap: bool = False


class JobRequest(ExportSettings):
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
    diarize: bool = False
    num_speakers: int | None = Field(default=None, ge=1, le=20)
    chunk_seconds: int = Field(default=600, ge=30, le=600)
    formats: list[Literal["srt", "vtt", "ass", "txt", "json"]] = Field(
        default_factory=lambda: ["srt"]
    )


class WordRequest(BaseModel):
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(gt=0, allow_inf_nan=False)
    text: str = Field(min_length=1, max_length=10000)
    estimated: bool = False


class CueRequest(BaseModel):
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(gt=0, allow_inf_nan=False)
    text: str = Field(min_length=1, max_length=10000)
    words: list[WordRequest] = Field(default_factory=list, max_length=10000)
    speaker: str | None = Field(default=None, max_length=80)
    translation: str | None = Field(default=None, max_length=10000)


class EditRequest(ExportSettings):
    revision: int | None = None
    segments: list[CueRequest] = Field(max_length=100000)
    formats: list[Literal["srt", "vtt", "ass", "txt", "json"]] = Field(
        default_factory=lambda: ["srt"]
    )


class DraftCue(BaseModel):
    start: float | None = Field(default=None, allow_inf_nan=False)
    end: float | None = Field(default=None, allow_inf_nan=False)
    text: str = Field(default="", max_length=10000)
    words: list[WordRequest] = Field(default_factory=list, max_length=10000)
    speaker: str | None = Field(default=None, max_length=80)
    translation: str | None = Field(default=None, max_length=10000)


class DraftRequest(ExportSettings):
    formats: list[Literal["srt", "vtt", "ass", "txt", "json"]] = Field(
        default_factory=lambda: ["srt"]
    )
    segments: list[DraftCue] = Field(max_length=100000)
    revision: int


class TranslationRequest(BaseModel):
    target: str = Field(min_length=1, max_length=80)
    model: str = Field(default="gpt-4o-mini", min_length=1, max_length=100)


class VideoRequest(BaseModel):
    mode: Literal["burn", "soft"] = "burn"


class PreferencesRequest(BaseModel):
    job: JobRequest | None = None
    translation_model: str = Field(default="gpt-4o-mini", max_length=100)
    target_language: str = Field(default="en", max_length=80)


def atomic_json(path, data):
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


@dataclass
class Media:
    path: Path
    name: str
    info: MediaInfo
    size: int = 0
    created: float = field(default_factory=time.time)


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
    updated: float = field(default_factory=time.time)
    media_id: str = ""
    revision: int = 0
    operation: dict = field(default_factory=dict)
    operation_cancel: threading.Event = field(default_factory=threading.Event)

    def public(self) -> dict:
        return {
            "id": self.id,
            "media_id": self.media_id,
            "media_available": self.media.path.is_file(),
            "updated": self.updated,
            "revision": self.revision,
            "request": self.request.model_dump(),
            "operation": self.operation,
            "draft": (self.directory / "draft.json").is_file(),
            "videos": [
                {"mode": mode, "url": f"/api/jobs/{self.id}/video/{mode}"}
                for mode in ("burn", "soft")
                if (self.directory / f"video-{mode}.mp4").is_file()
            ],
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
    def __init__(self, root: Path, max_upload: int, max_storage: int, persistent=False):
        self.root = root
        self.persistent = persistent
        self.model_downloads = {}
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_upload = max_upload
        self.max_storage = max_storage
        self.upload_sizes: dict[str, int] = {}
        self.media: dict[str, Media] = {}
        self.jobs: dict[str, Job] = {}
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="shengmu")
        if persistent:
            self.restore()

    def save_media(self, media_id):
        if self.persistent:
            media = self.media[media_id]
            atomic_json(
                media.path.parent / ".media.json",
                {
                    "id": media_id,
                    "name": media.name,
                    "size": media.size,
                    "filename": media.path.name,
                    "info": media.info.to_dict(),
                },
            )

    def save_job(self, job):
        if self.persistent:
            atomic_json(
                job.directory / ".job.json",
                {
                    **job.public(),
                    "transcript": job.transcript.to_dict() if job.transcript else None,
                    "media": {
                        "name": job.media.name,
                        "filename": job.media.path.name,
                        "info": job.media.info.to_dict(),
                    },
                },
            )

    def restore(self):
        from .media import AudioTrack

        def info(data):
            return MediaInfo(data["duration"], [AudioTrack(**t) for t in data["audio_tracks"]])

        for manifest in self.root.glob("*/.media.json"):
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                if (
                    Path(data["filename"]).name != data["filename"]
                    or data["id"] != manifest.parent.name
                ):
                    raise ValueError("invalid manifest")
                path = manifest.parent / data["filename"]
                if path.is_file():
                    self.media[data["id"]] = Media(
                        path, data["name"], info(data["info"]), data["size"]
                    )
            except (OSError, ValueError, KeyError, TypeError):
                logger.error("无法恢复媒体记录 %s", manifest)
        for manifest in self.root.glob("*/.job.json"):
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                if (
                    data["id"] != manifest.parent.name
                    or not all(c in "0123456789abcdef" for c in data["media_id"])
                    or Path(data["media"]["filename"]).name != data["media"]["filename"]
                ):
                    raise ValueError("invalid manifest")
                media = self.media.get(data["media_id"]) or Media(
                    self.root / data["media_id"] / data["media"]["filename"],
                    data["media"]["name"],
                    info(data["media"]["info"]),
                )
                job = Job(
                    data["id"],
                    media,
                    manifest.parent,
                    JobRequest(**data["request"]),
                    media_id=data["media_id"],
                )
                job.status, job.progress, job.message = (
                    data["status"],
                    data["progress"],
                    data["message"],
                )
                job.updated, job.revision = data["updated"], data.get("revision", 0)
                job.transcript = (
                    Transcript.from_dict(data["transcript"]) if data["transcript"] else None
                )
                job.files = [
                    manifest.parent / f["name"]
                    for f in data["files"]
                    if Path(f["name"]).name == f["name"] and (manifest.parent / f["name"]).is_file()
                ]
                job.operation = data.get("operation", {})
                if job.status not in TERMINAL:
                    job.status, job.message = "error", "上次处理被服务中断，可重试。"
                if job.operation.get("status") not in TERMINAL:
                    job.operation = {}
                self.jobs[job.id] = job
                self.save_job(job)
            except (OSError, ValueError, KeyError, TypeError, SubtitleError):
                logger.error("无法恢复任务记录 %s", manifest)

    def check_edit(self, job, revision=None):
        if job.status != "done" or job.transcript is None:
            raise HTTPException(409, "字幕尚未生成。")
        if job.operation.get("status") in {"queued", "running"}:
            raise HTTPException(409, "任务正在翻译或导出视频，请等待完成。")
        if revision is not None and revision != job.revision:
            raise HTTPException(409, "字幕已在其他页面更新，请重新打开项目后编辑。")

    def start_operation(self, job, kind, action):
        with self.lock:
            self.check_edit(job)
            if (job.directory / "draft.json").is_file():
                raise HTTPException(409, "请先保存并导出草稿，再执行此操作。")
            job.operation_cancel = threading.Event()
            job.operation = {
                "kind": kind,
                "status": "queued",
                "progress": 0,
                "message": "等待处理…",
            }
            self.save_job(job)

        def run():
            def update(amount, message):
                with self.lock:
                    job.operation.update(status="running", progress=amount, message=message)

            try:
                update(0.01, "正在处理…")
                result = action(update, job.operation_cancel)
                check_cancel(job.operation_cancel)
                with self.lock:
                    if isinstance(result, Transcript):
                        files = export_files(
                            result,
                            job.directory,
                            Path(job.media.name).stem,
                            job.request.formats,
                            overwrite=True,
                        )
                        job.transcript, job.files = result, files
                        job.revision += 1
                    job.operation.update(status="done", progress=1, message="处理完成")
                    if isinstance(result, Path):
                        job.operation["url"] = f"/api/jobs/{job.id}/video/{kind}"
                    job.updated = time.time()
                    self.save_job(job)
            except Cancelled:
                with self.lock:
                    job.operation.update(status="cancelled", message="操作已取消")
                    self.save_job(job)
            except Exception as exc:
                logger.error("操作失败\n%s", redacted_traceback())
                with self.lock:
                    job.operation.update(
                        status="error",
                        message=str(exc)
                        if isinstance(exc, SubtitleError)
                        else "操作失败，请查看服务日志。",
                    )
                    self.save_job(job)

        self.executor.submit(run)
        return job.public()

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
            if any(
                job.media is media
                and (
                    job.status not in TERMINAL
                    or job.operation.get("status") in {"queued", "running"}
                )
                for job in self.jobs.values()
            ):
                raise HTTPException(409, "文件仍用于排队或处理中的任务，暂时不能删除。")
            shutil.rmtree(media.path.parent)
            del self.media[media_id]

    def cleanup(self) -> None:
        if self.persistent:
            return
        # Session results expire after 24 hours. Only unreferenced uploads can be removed.
        cutoff = time.time() - 86400
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
                job.updated = time.time()

        try:
            r = job.request
            result = transcribe(
                job.media.path,
                EngineOptions(
                    r.engine,
                    str(model_manager.model_path(model_manager.model_root(self.root), r.model))
                    if (
                        model_manager.model_path(model_manager.model_root(self.root), r.model)
                        / ".complete"
                    ).is_file()
                    else r.model,
                    r.language,
                    r.device,
                    r.compute_type,
                    r.prompt,
                    condition_on_previous_text=r.condition_on_previous_text,
                    filter_hallucinations=r.filter_hallucinations,
                    chunk_seconds=r.chunk_seconds,
                    captions=CaptionOptions(**r.captions.model_dump()),
                    diarize=r.diarize,
                    num_speakers=r.num_speakers,
                ),
                r.track,
                update,
                job.cancel,
            )
            result.model = r.model if r.engine == "local" else result.model
            result.source = job.media.name
            result.metadata.update(
                r.model_dump(
                    include={"captions", "style", "export_mode", "speaker_labels", "word_highlight"}
                )
            )
            with self.lock:
                if job.cancel.is_set():
                    raise Cancelled("任务已取消。")
                update("export", 0.97, "正在导出字幕…")
                job.files = export_files(
                    result, job.directory, Path(job.media.name).stem, r.formats, overwrite=True
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
        finally:
            with self.lock:
                self.save_job(job)

    def close(self) -> None:
        for state in self.model_downloads.values():
            state["cancel"].set()
        for job in self.jobs.values():
            job.cancel.set()
            job.operation_cancel.set()
        # Model load/in-flight SDK requests are cooperative and may need time to return.
        self.executor.shutdown(wait=True, cancel_futures=True)


def create_app(
    max_upload: int = 2 * 1024**3, max_storage: int | None = None, workspace_dir: Path | None = None
) -> FastAPI:
    max_storage = max_upload * 2 if max_storage is None else max_storage
    token = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from contextlib import nullcontext

        with (
            nullcontext(str(workspace_dir))
            if workspace_dir
            else tempfile.TemporaryDirectory(prefix="shengmu-gui-")
        ) as directory:
            with workspace_lock(Path(directory)) if workspace_dir else nullcontext():
                workspace = Workspace(
                    Path(directory), max_upload, max_storage, persistent=workspace_dir is not None
                )
                app.state.workspace = workspace
                try:
                    yield
                finally:
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
            "persistent": workspace_dir is not None,
            "workspace": str(workspace_dir.resolve()) if workspace_dir else None,
            "diarization_installed": __import__("importlib.util", fromlist=["find_spec"]).find_spec(
                "pyannote"
            )
            is not None,
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
                raise HTTPException(429, "本次会话已达到 30 个文件，请释放旧源文件。")
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
                workspace.save_media(media_id)
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
            if data.export_mode != "original":
                raise HTTPException(400, "生成时请选择原文，完成翻译后可导出译文或双语。")
            if not data.formats:
                raise HTTPException(400, "请至少选择一种字幕格式。")
            if data.track is not None and data.track not in {
                t.index for t in media.info.audio_tracks
            }:
                raise HTTPException(400, "所选音轨不存在。")
            if sum(j.status not in TERMINAL for j in workspace.jobs.values()) >= 5:
                raise HTTPException(429, "最多允许 5 个任务等待或处理。")
            if len(workspace.jobs) >= 100:
                raise HTTPException(429, "本次会话已达到 100 个任务，请下载结果后删除旧任务。")
            job_id = uuid.uuid4().hex
            job = Job(job_id, media, workspace.root / job_id, data, media_id=data.media_id)
            workspace.jobs[job_id] = job
            workspace.save_job(job)
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
            workspace.check_edit(job, data.revision)
            try:
                result = replace(
                    job.transcript,
                    segments=[
                        Segment(
                            c.start,
                            c.end,
                            c.text,
                            [Word(**w.model_dump()) for w in c.words],
                            c.speaker,
                            c.translation,
                        )
                        for c in data.segments
                    ],
                    allow_overlap=data.allow_overlap,
                    metadata={
                        **job.transcript.metadata,
                        **data.model_dump(
                            include={
                                "captions",
                                "style",
                                "export_mode",
                                "speaker_labels",
                                "word_highlight",
                            }
                        ),
                    },
                )
                job.files = export_files(
                    result, job.directory, Path(job.media.name).stem, data.formats, overwrite=True
                )
            except SubtitleError as exc:
                raise HTTPException(400, str(exc)) from exc
            job.transcript = result
            job.updated = time.time()
            job.revision += 1
            job.request = JobRequest(
                **{**job.request.model_dump(), **data.model_dump(exclude={"segments", "revision"})}
            )
            (job.directory / "draft.json").unlink(missing_ok=True)
            workspace.save_job(job)
            return job.public()

    @app.get("/api/jobs")
    def list_jobs():
        workspace = app.state.workspace
        with workspace.lock:
            return sorted(
                [job.public() for job in workspace.jobs.values()],
                key=lambda j: j["updated"],
                reverse=True,
            )

    @app.get("/api/media")
    def list_media():
        workspace = app.state.workspace
        with workspace.lock:
            return [
                {
                    "id": media_id,
                    "name": media.name,
                    "size": media.size,
                    "active": any(
                        j.media_id == media_id
                        and (
                            j.status not in TERMINAL
                            or j.operation.get("status") in {"queued", "running"}
                        )
                        for j in workspace.jobs.values()
                    ),
                }
                for media_id, media in workspace.media.items()
            ]

    @app.get("/api/media/{media_id}")
    def media_info(media_id: str):
        workspace = app.state.workspace
        with workspace.lock:
            media = workspace.media.get(media_id)
            if not media:
                raise HTTPException(404, "源文件已释放。")
            return {
                "id": media_id,
                "name": media.name,
                "size": media.size,
                **media.info.to_dict(),
                "url": f"/api/media/{media_id}/file",
            }

    @app.get("/api/media/{media_id}/file")
    def media_file(media_id: str):
        media = app.state.workspace.media.get(media_id)
        if not media or not media.path.is_file():
            raise HTTPException(404, "源文件已释放。")
        return FileResponse(media.path)

    @app.get("/api/media/{media_id}/waveform")
    def media_waveform(media_id: str, track: int | None = None):
        workspace = app.state.workspace
        with workspace.lock:
            media = workspace.media.get(media_id)
            if not media:
                raise HTTPException(404, "源文件已释放。")
            track = media.info.audio_tracks[0].index if track is None else track
            if track not in {t.index for t in media.info.audio_tracks}:
                raise HTTPException(400, "音轨不存在。")
            cache = media.path.parent / f".waveform-{track}.json"
            if cache.is_file():
                return {
                    **json.loads(cache.read_text(encoding="utf-8")),
                    "offset": next(t.offset for t in media.info.audio_tracks if t.index == track),
                }
        try:
            data = waveform(media.path, track, workspace.root)
            with workspace.lock:
                if media_id in workspace.media:
                    atomic_json(cache, data)
            return {
                **data,
                "offset": next(t.offset for t in media.info.audio_tracks if t.index == track),
            }
        except SubtitleError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/jobs/{job_id}/draft")
    def get_draft(job_id: str):
        workspace = app.state.workspace
        with workspace.lock:
            job = workspace.get_job(job_id)
            path = job.directory / "draft.json"
            return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    @app.put("/api/jobs/{job_id}/draft")
    def save_draft(job_id: str, data: DraftRequest):
        workspace = app.state.workspace
        with workspace.lock:
            job = workspace.get_job(job_id)
            workspace.check_edit(job, data.revision)
            atomic_json(job.directory / "draft.json", data.model_dump())
            return {"saved": True, "revision": job.revision}

    @app.post("/api/jobs/{job_id}/retry", status_code=202)
    def retry_job(job_id: str):
        workspace = app.state.workspace
        with workspace.lock:
            job = workspace.get_job(job_id)
            if job.status not in {"error", "cancelled"}:
                raise HTTPException(409, "只能重试失败或取消的任务。")
            if not job.media.path.is_file() or job.media_id not in workspace.media:
                raise HTTPException(409, "源文件已释放，无法重试。")
            if sum(j.status not in TERMINAL for j in workspace.jobs.values()) >= 5:
                raise HTTPException(429, "最多允许 5 个任务等待或处理。")
            job.cancel = threading.Event()
            job.status, job.progress, job.message = "queued", 0, "等待重试…"
            workspace.save_job(job)
            workspace.executor.submit(workspace.process, job)
            return job.public()

    @app.delete("/api/jobs/{job_id}")
    def delete_job(job_id: str):
        workspace = app.state.workspace
        with workspace.lock:
            job = workspace.get_job(job_id)
            if job.status not in TERMINAL or job.operation.get("status") in {"queued", "running"}:
                raise HTTPException(409, "请先取消正在运行的任务。")
            shutil.rmtree(job.directory, ignore_errors=True)
            del workspace.jobs[job_id]
            return {"deleted": True}

    @app.get("/api/jobs/{job_id}/quality")
    def quality(job_id: str):
        job = app.state.workspace.get_job(job_id)
        if not job.transcript:
            raise HTTPException(409, "字幕尚未生成。")
        return quality_report(
            job.transcript.segments,
            CaptionOptions(**job.transcript.metadata.get("captions", {})),
            job.transcript.allow_overlap,
        )

    @app.post("/api/jobs/{job_id}/reflow")
    def reflow(job_id: str, data: CaptionRequest):
        from .captions import split_caption

        workspace = app.state.workspace
        with workspace.lock:
            job = workspace.get_job(job_id)
            workspace.check_edit(job)
            if (job.directory / "draft.json").is_file():
                raise HTTPException(409, "请先保存草稿。")
            options = CaptionOptions(**data.model_dump())
            try:
                options.validate()
                cues = []
                for cue in job.transcript.segments:
                    split = split_caption(
                        cue.start,
                        cue.end,
                        cue.text.replace(
                            "\n", " " if job.transcript.language not in {"zh", "ja", "ko"} else ""
                        ),
                        cue.words,
                        options,
                    )
                    cues.extend(
                        replace(
                            c,
                            speaker=cue.speaker,
                            translation=cue.translation if len(split) == 1 else None,
                        )
                        for c in split
                    )
                result = replace(
                    job.transcript,
                    segments=sorted(cues, key=lambda c: c.start),
                    metadata={
                        **job.transcript.metadata,
                        "captions": data.model_dump(),
                        "export_mode": "original",
                    },
                )
                files = export_files(
                    result,
                    job.directory,
                    Path(job.media.name).stem,
                    job.request.formats,
                    overwrite=True,
                )
            except SubtitleError as exc:
                raise HTTPException(400, str(exc)) from exc
            job.transcript, job.files = result, files
            job.request = JobRequest(
                **{
                    **job.request.model_dump(),
                    "captions": data.model_dump(),
                    "export_mode": "original",
                }
            )
            job.revision += 1
            job.updated = time.time()
            workspace.save_job(job)
            return job.public()

    @app.post("/api/jobs/{job_id}/translate", status_code=202)
    def translate_job(job_id: str, data: TranslationRequest):
        workspace = app.state.workspace
        job = workspace.get_job(job_id)
        return workspace.start_operation(
            job,
            "translate",
            lambda update, cancel: translate(
                job.transcript, data.target, data.model, update, cancel
            ),
        )

    @app.post("/api/jobs/{job_id}/video", status_code=202)
    def render_video(job_id: str, data: VideoRequest):
        workspace = app.state.workspace
        job = workspace.get_job(job_id)
        if not job.media.path.is_file():
            raise HTTPException(409, "视频导出需要源文件，请保留源文件缓存。")
        return workspace.start_operation(
            job,
            data.mode,
            lambda update, cancel: export_video(
                job.media.path, job.transcript, job.directory, data.mode, cancel
            ),
        )

    @app.post("/api/jobs/{job_id}/operation/cancel")
    def cancel_operation(job_id: str):
        job = app.state.workspace.get_job(job_id)
        job.operation_cancel.set()
        return job.public()

    @app.get("/api/jobs/{job_id}/video/{mode}")
    def download_video(job_id: str, mode: Literal["burn", "soft"]):
        job = app.state.workspace.get_job(job_id)
        path = job.directory / f"video-{mode}.mp4"
        if not path.is_file():
            raise HTTPException(404, "视频尚未导出。")
        return FileResponse(
            path, media_type="video/mp4", filename=f"{Path(job.media.name).stem}-{mode}.mp4"
        )

    @app.get("/api/downloads.zip")
    def batch_download(ids: str = ""):
        workspace = app.state.workspace
        selected = list(dict.fromkeys(ids.split(","))) if ids else list(workspace.jobs)
        if len(selected) > 100:
            raise HTTPException(400, "批量下载最多 100 个任务。")
        fd, name = tempfile.mkstemp(suffix=".zip", prefix="download-", dir=workspace.root)
        os.close(fd)
        try:
            with workspace.lock, zipfile.ZipFile(name, "w", zipfile.ZIP_DEFLATED) as archive:
                count = 0
                for job_id in selected:
                    job = workspace.get_job(job_id)
                    if job.status == "done":
                        for path in job.files:
                            if path.is_file():
                                archive.write(path, f"{job.id[:8]}/{path.name}")
                                count += 1
                if not count:
                    raise HTTPException(400, "没有可下载的已完成字幕。")
            return FileResponse(
                name,
                filename="shengmu-subtitles.zip",
                background=BackgroundTask(Path(name).unlink, missing_ok=True),
            )
        except BaseException:
            Path(name).unlink(missing_ok=True)
            raise

    @app.get("/api/preferences")
    def preferences():
        path = app.state.workspace.root / "preferences.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

    @app.put("/api/preferences")
    def save_preferences(data: PreferencesRequest):
        workspace = app.state.workspace
        with workspace.lock:
            atomic_json(workspace.root / "preferences.json", data.model_dump())
        return {"saved": True}

    @app.get("/api/models")
    def models():
        workspace = app.state.workspace
        root = model_manager.model_root(workspace.root)
        with workspace.lock:
            inventory = model_manager.model_inventory(root)
            for item in inventory:
                state = workspace.model_downloads.get(item["name"])
                if state:
                    item["download"] = {k: v for k, v in state.items() if k != "cancel"}
                    total = state.get("total", 0)
                    if total and state["status"] == "running":
                        item["download"]["progress"] = min(0.99, item["size"] / total)
            return inventory

    @app.post("/api/models/{name}/download", status_code=202)
    def download_model(name: str):
        workspace = app.state.workspace
        root = model_manager.model_root(workspace.root)
        try:
            model_manager.model_path(root, name)
        except SubtitleError as exc:
            raise HTTPException(400, str(exc)) from exc
        with workspace.lock:
            if any(
                state["status"] in {"queued", "running"}
                for state in workspace.model_downloads.values()
            ):
                raise HTTPException(409, "已有模型在下载，请等待或取消。")
            state = {
                "status": "queued",
                "progress": 0,
                "total": 0,
                "message": "等待下载…",
                "cancel": threading.Event(),
            }
            workspace.model_downloads[name] = state

        def run():
            def update(done, total, message):
                with workspace.lock:
                    state.update(
                        status="running",
                        progress=done / max(1, total),
                        total=total,
                        message=message,
                    )

            try:
                model_manager.download(root, name, update, state["cancel"])
                with workspace.lock:
                    state.update(status="done", progress=1, message="下载完成")
            except Cancelled:
                with workspace.lock:
                    state.update(status="cancelled", message="下载已取消，可重试续传")
            except Exception as exc:
                logger.error("模型下载失败\\n%s", redacted_traceback())
                with workspace.lock:
                    state.update(
                        status="error",
                        message=str(exc)
                        if isinstance(exc, SubtitleError)
                        else "模型下载失败，请检查日志。",
                    )

        workspace.executor.submit(run)
        return {"queued": True}

    @app.post("/api/models/{name}/cancel")
    def cancel_model(name: str):
        state = app.state.workspace.model_downloads.get(name)
        if not state:
            raise HTTPException(404, "下载不存在。")
        state["cancel"].set()
        return {"cancelled": True}

    @app.delete("/api/models/{name}")
    def remove_model(name: str):
        workspace = app.state.workspace
        with workspace.lock:
            if any(
                j.status not in TERMINAL and j.request.engine == "local"
                for j in workspace.jobs.values()
            ) or workspace.model_downloads.get(name, {}).get("status") in {"queued", "running"}:
                raise HTTPException(409, "模型正在使用或下载。")
            try:
                path = model_manager.model_path(model_manager.model_root(workspace.root), name)
            except SubtitleError as exc:
                raise HTTPException(400, str(exc)) from exc
            from .engines import LocalEngine

            with LocalEngine._lock:
                if LocalEngine._cached_key and LocalEngine._cached_key[0] == str(path):
                    LocalEngine._cached_key = LocalEngine._cached_model = None
                shutil.rmtree(path, ignore_errors=True)
            workspace.model_downloads.pop(name, None)
        return {"deleted": True}

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
