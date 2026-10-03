from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import asdict, replace
from pathlib import Path
from threading import Timer

from . import __version__
from .diagnostics import doctor, redacted_traceback
from .engines import EngineOptions
from .exporters import FORMATS, export_files, output_paths
from .media import probe
from .models import SubtitleError, Transcript
from .pipeline import transcribe, transcribe_to_files
from .storage import atomic_json
from .translation import TranslationOptions, checkpoint_path, translate


def translation_arguments(command, prefix=""):
    def add(name, **kwargs):
        command.add_argument(
            "--" + prefix + name, dest="translation_" + name.replace("-", "_"), **kwargs
        )

    add("provider", choices=("lmstudio", "openai"), default="lmstudio")
    add("model", required=not prefix, help="文字翻译模型标识，与 Whisper 模型不同")
    add("concurrency", type=int, choices=(1, 2), default=1)
    add("retries", type=int, default=2)
    add("batch-size", type=int, default=8)
    add("max-chars", type=int, default=2000)
    add("timeout", type=float, default=None)
    add("max-tokens", type=int, default=4096)
    add("reasoning", choices=("auto", "off", "on", "low", "medium", "high"), default="auto")
    command.add_argument(
        "--resume",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="恢复相同字幕 / 翻译设置的已完成结果；--no-resume 重新处理",
    )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="shengmu", description="声幕：自动生成带时间轴的视频字幕")
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--verbose", action="store_true", help="显示经过敏感信息脱敏的异常链")
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
    run.add_argument(
        "--condition-on-previous-text",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="使用前文；默认关闭以减少重复",
    )
    run.add_argument(
        "--filter-hallucinations",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="过滤已知的孤立幻觉字幕",
    )
    run.add_argument(
        "--asr-profile", choices=("standard", "less-repetition", "soft-speech"), default="standard"
    )
    run.add_argument("--compression-ratio-threshold", type=float, default=None)
    run.add_argument("--log-prob-threshold", type=float, default=None)
    run.add_argument("--no-speech-threshold", type=float, default=None)
    run.add_argument("--vad-threshold", type=float, default=None)
    run.add_argument("--chunk-seconds", type=int, default=600, help="API 分块长度，30–600 秒")
    run.add_argument("--translate-to", dest="translation_target", help="转写后继续翻译，例如 zh")
    translation_arguments(run, "translation-")
    run.add_argument("--export-mode", choices=("original", "translated", "bilingual"), default=None)
    run.add_argument("--format", choices=FORMATS, nargs="+", default=["srt"], dest="formats")
    run.add_argument("--output-dir", type=Path, default=Path("subtitles"))
    run.add_argument("--overwrite", action="store_true")
    run.add_argument("--quiet", action="store_true", help="仅输出结果 JSON，便于脚本调用")
    inspect = commands.add_parser("inspect", help="查看媒体时长和可用音轨")
    inspect.add_argument("input", type=Path)
    convert = commands.add_parser("export", help="从声幕 JSON 转换字幕格式，无需再次调用 AI")
    convert.add_argument("input", type=Path)
    convert.add_argument(
        "--export-mode", choices=("original", "translated", "bilingual"), default=None
    )
    convert.add_argument("--format", choices=FORMATS, nargs="+", default=["srt"], dest="formats")
    convert.add_argument("--output-dir", type=Path, default=Path("subtitles"))
    convert.add_argument("--overwrite", action="store_true")
    translation = commands.add_parser("translate", help="翻译声幕 JSON，支持检查点续跑和双语导出")
    translation.add_argument("input", type=Path)
    translation.add_argument("--target", dest="translation_target", required=True)
    translation_arguments(translation)
    translation.add_argument(
        "--export-mode", choices=("original", "translated", "bilingual"), default="bilingual"
    )
    translation.add_argument(
        "--format", choices=FORMATS, nargs="+", default=["srt", "json"], dest="formats"
    )
    translation.add_argument("--output-dir", type=Path, default=Path("subtitles"))
    translation.add_argument("--overwrite", action="store_true")
    translation.add_argument("--quiet", action="store_true")
    gui = commands.add_parser("gui", help="启动本地浏览器图形界面")
    gui.add_argument(
        "--workspace",
        type=Path,
        default=Path(os.environ.get("SHENGMU_WORKSPACE", ".shengmu/workspace")),
        help="持久项目目录",
    )
    gui.add_argument("--port", type=int, default=8765)
    gui.add_argument("--no-browser", action="store_true", help="仅启动服务，不自动打开浏览器")
    diagnostic = commands.add_parser("doctor", help="检查工具、烧录能力和 AI 引擎")
    diagnostic.add_argument(
        "--network", action="store_true", help="额外探测模型下载服务及 LM Studio，不下载或推理"
    )
    for command in commands.choices.values():
        command.add_argument(
            "--verbose",
            action="store_true",
            default=argparse.SUPPRESS,
            help="显示经过敏感信息脱敏的异常链",
        )
    return root


def translation_options(args):
    return TranslationOptions(
        **{
            name: getattr(args, "translation_" + name)
            for name in (
                "concurrency",
                "retries",
                "batch_size",
                "max_chars",
                "timeout",
                "max_tokens",
                "reasoning",
            )
        }
    )


def translate_for_cli(transcript, args, progress):
    options = translation_options(args)
    checkpoint = checkpoint_path(
        args.output_dir,
        transcript,
        args.translation_target,
        args.translation_model,
        args.translation_provider,
        options,
    )
    result = translate(
        transcript,
        args.translation_target,
        args.translation_model,
        lambda amount, message: progress("translate", amount, message),
        provider=args.translation_provider,
        options=options,
        checkpoint=checkpoint,
        resume=args.resume,
    )
    return replace(
        result, metadata={**result.metadata, "export_mode": args.export_mode or "bilingual"}
    )


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:

        def progress(stage, amount, message):
            if not getattr(args, "quiet", False):
                print(f"[{stage} {amount:5.0%}] {message}", file=sys.stderr)

        if args.command == "doctor":
            print(json.dumps(doctor(network=args.network), ensure_ascii=False, indent=2))
        elif args.command == "inspect":
            print(json.dumps(probe(args.input).to_dict(), ensure_ascii=False, indent=2))
        elif args.command in {"export", "translate"}:
            transcript = Transcript.from_dict(json.loads(args.input.read_text(encoding="utf-8")))
            stem = args.input.stem + (".translated" if args.command == "translate" else "")
            output_paths(args.output_dir, stem, args.formats, args.overwrite, protected=args.input)
            if args.command == "translate":
                transcript = translate_for_cli(transcript, args, progress)
            elif args.export_mode:
                transcript = replace(
                    transcript, metadata={**transcript.metadata, "export_mode": args.export_mode}
                )
            paths = export_files(
                transcript,
                args.output_dir,
                stem,
                args.formats,
                args.overwrite,
                protected=args.input,
            )
            print(
                json.dumps(
                    {
                        "files": [str(path) for path in paths],
                        "segments": len(transcript.segments),
                        "translation": transcript.metadata.get("translation_stats"),
                    },
                    ensure_ascii=False,
                )
            )
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
            uvicorn.run(
                create_app(workspace_dir=args.workspace.expanduser().resolve()),
                host="127.0.0.1",
                port=args.port,
                log_level="info" if args.verbose else "warning",
            )
        else:
            options = EngineOptions(
                args.engine,
                args.model,
                args.language,
                args.device,
                args.compute_type,
                args.prompt,
                args.chunk_seconds,
                condition_on_previous_text=args.condition_on_previous_text,
                filter_hallucinations=args.filter_hallucinations,
                asr_profile=args.asr_profile,
                compression_ratio_threshold=args.compression_ratio_threshold,
                log_prob_threshold=args.log_prob_threshold,
                no_speech_threshold=args.no_speech_threshold,
                vad_threshold=args.vad_threshold,
            )
            if args.translation_target:
                if not args.translation_model:
                    raise SubtitleError("转写后翻译需要 --translation-model 指定文字模型。")
                translation_options(args).validate()
                output_paths(
                    args.output_dir,
                    args.input.stem,
                    args.formats,
                    args.overwrite,
                    protected=args.input,
                )
                source = args.input.expanduser().resolve()
                state = source.stat()
                signature = hashlib.sha256(
                    json.dumps(
                        [
                            str(source),
                            state.st_size,
                            state.st_mtime_ns,
                            asdict(options),
                            args.track,
                        ],
                        sort_keys=True,
                    ).encode()
                ).hexdigest()
                cache = args.output_dir / ".translation-cache" / (signature + ".source.json")
                if args.resume and cache.is_file():
                    transcript = Transcript.from_dict(json.loads(cache.read_text(encoding="utf-8")))
                    progress("transcribe", 1, "已恢复上次完成的转写，继续翻译…")
                else:
                    transcript = transcribe(args.input, options, args.track, progress)
                    atomic_json(cache, transcript.to_dict())
                transcript = translate_for_cli(transcript, args, progress)
                paths = export_files(
                    transcript,
                    args.output_dir,
                    args.input.stem,
                    args.formats,
                    args.overwrite,
                    protected=args.input,
                )
            else:
                if args.export_mode in {"translated", "bilingual"}:
                    raise SubtitleError(
                        "双语 / 译文导出需要 --translate-to 和 --translation-model。"
                    )
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
                        "translation": transcript.metadata.get("translation_stats"),
                    },
                    ensure_ascii=False,
                )
            )
        return 0
    except KeyboardInterrupt:
        print("任务已取消，已保存的翻译检查点可续跑。", file=sys.stderr)
        return 130
    except (SubtitleError, OSError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        if args.verbose:
            print(redacted_traceback(), file=sys.stderr)
        return 1
    except Exception:
        print("错误：任务失败，请使用 --verbose 查看诊断信息。", file=sys.stderr)
        if args.verbose:
            print(redacted_traceback(), file=sys.stderr)
        return 1
