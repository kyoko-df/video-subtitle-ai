from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path
from threading import Timer

from . import __version__
from .engines import EngineOptions
from .exporters import FORMATS, export_files
from .media import probe
from .models import SubtitleError, Transcript
from .pipeline import transcribe_to_files


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="shengmu", description="声幕：自动生成带时间轴的视频字幕")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    run = commands.add_parser("transcribe", help="提取音轨并调用 AI 生成字幕")
    run.add_argument("input", type=Path, help="视频或音频文件")
    run.add_argument("--engine", choices=("local", "openai"), default="local")
    run.add_argument("--model", default="small", help="本地 Whisper 模型名或模型目录")
    run.add_argument("--language", default=None, help="语言代码，例如 zh/en；省略时自动检测")
    run.add_argument("--track", type=int, default=None, help="ffprobe 音轨流编号；默认第一条音轨")
    run.add_argument("--device", choices=("cpu", "cuda", "auto"), default="cpu")
    run.add_argument("--compute-type", default="int8", help="本地模型计算精度，例如 int8/float16")
    run.add_argument("--prompt", default="", help="人名、专有名词等识别提示")
    run.add_argument("--chunk-seconds", type=int, default=600, help="API 分块长度，30–600 秒")
    run.add_argument("--format", choices=FORMATS, nargs="+", default=["srt"], dest="formats")
    run.add_argument("--output-dir", type=Path, default=Path("subtitles"))
    run.add_argument("--overwrite", action="store_true")
    run.add_argument("--quiet", action="store_true", help="仅输出结果 JSON，便于脚本调用")
    inspect = commands.add_parser("inspect", help="查看媒体时长和可用音轨")
    inspect.add_argument("input", type=Path)
    convert = commands.add_parser("export", help="从声幕 JSON 转换字幕格式，无需再次调用 AI")
    convert.add_argument("input", type=Path)
    convert.add_argument("--format", choices=FORMATS, nargs="+", default=["srt"], dest="formats")
    convert.add_argument("--output-dir", type=Path, default=Path("subtitles"))
    convert.add_argument("--overwrite", action="store_true")
    gui = commands.add_parser("gui", help="启动本地浏览器图形界面")
    gui.add_argument("--port", type=int, default=8765)
    gui.add_argument("--no-browser", action="store_true", help="仅启动服务，不自动打开浏览器")
    commands.add_parser("doctor", help="检查工具和 AI 引擎是否已安装")
    return root


def doctor() -> dict:
    return {
        "python": sys.version.split()[0],
        "ffmpeg": shutil.which(os.environ.get("FFMPEG_BINARY", "ffmpeg")),
        "ffprobe": shutil.which(os.environ.get("FFPROBE_BINARY", "ffprobe")),
        "local_engine_installed": importlib.util.find_spec("faster_whisper") is not None,
        "openai_engine_installed": importlib.util.find_spec("openai") is not None,
        "openai_key_configured": bool(os.environ.get("OPENAI_API_KEY", "").strip()),
    }


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "doctor":
            print(json.dumps(doctor(), ensure_ascii=False, indent=2))
        elif args.command == "inspect":
            print(json.dumps(probe(args.input).to_dict(), ensure_ascii=False, indent=2))
        elif args.command == "export":
            transcript = Transcript.from_dict(json.loads(args.input.read_text(encoding="utf-8")))
            paths = export_files(
                transcript,
                args.output_dir,
                args.input.stem,
                args.formats,
                args.overwrite,
                protected=args.input,
            )
            print(json.dumps({"files": [str(path) for path in paths]}, ensure_ascii=False))
        elif args.command == "gui":
            if not 1 <= args.port <= 65535:
                raise SubtitleError("端口必须在 1 到 65535 之间。")
            import uvicorn

            from .server import create_app

            if not args.no_browser:
                import webbrowser

                timer = Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{args.port}"))
                timer.daemon = True
                timer.start()
            print(f"声幕 GUI：http://127.0.0.1:{args.port}（Ctrl+C 停止）")
            uvicorn.run(create_app(), host="127.0.0.1", port=args.port, log_level="warning")
        else:
            options = EngineOptions(
                args.engine,
                args.model,
                args.language,
                args.device,
                args.compute_type,
                args.prompt,
                args.chunk_seconds,
            )

            def progress(stage: str, amount: float, message: str) -> None:
                if not args.quiet:
                    print(f"[{amount:5.0%}] {message}", file=sys.stderr)

            transcript, paths = transcribe_to_files(
                args.input,
                options,
                args.output_dir,
                args.formats,
                args.track,
                args.overwrite,
                progress,
            )
            print(
                json.dumps(
                    {
                        "segments": len(transcript.segments),
                        "language": transcript.language,
                        "files": [str(path) for path in paths],
                    },
                    ensure_ascii=False,
                )
            )
        return 0
    except KeyboardInterrupt:
        print("任务已取消。", file=sys.stderr)
        return 130
    except (SubtitleError, OSError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
