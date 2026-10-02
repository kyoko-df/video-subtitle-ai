<a id="chinese"></a>

# 声幕 Shengmu · AI 视频字幕

[简体中文](#chinese) · [English](#english) · [日本語](#日本語)

一个可以直接开发和运行的 Python 项目：使用 FFmpeg 提取指定音轨，以 **本地 faster-whisper** 或 **OpenAI API** 生成带时间轴的字幕；提供本地浏览器 GUI 与 CLI，二者共用处理流程。

## 已实现

- 通过 ffprobe 读取时长、音轨编号、语言、声道和起始偏移；通过 FFmpeg 提取 16 kHz 单声道 PCM 音频。
- 本地 Whisper 模型转写，支持自动识别语言、CPU / NVIDIA CUDA、词级时间戳、识别提示及静音过滤；复用最近使用的一个模型。
- 按词级时间戳重新切分字幕，优先在标点处断开；中文 / 日文每行最多 20 字符，其他语言每行最多 42 字符，每条最多 2 行、7 秒。
- OpenAI `whisper-1` 转写，请求词级和片段时间戳；长音频按最多 600 秒分块，优先在边界前 5 秒内的静音处切分，下一块携带前文提示。
- 导出 **SRT、WebVTT、ASS、TXT、JSON**；TXT 为纯文本，其余保留时间信息。
- GUI 支持拖放文件、音轨选择、进度显示、取消、视频字幕预览、修改文本 / 时间、新增、拆分、合并、删除、撤销和重新导出；替换文件时释放旧上传。
- CLI 支持转写、查看音轨、JSON 格式转换和环境检查；拒绝意外覆盖输出或输入文件。
- GUI 仅监听本机地址；API Key 从服务器环境变量读取，前端不会收到 Key。

## 安装

建议使用 **Python 3.11 或 3.12**。项目声明 Python 3.10+，较新 Python 的本地模型依赖需有对应平台的安装包。Windows、macOS、Linux 均使用同一份源码。

本地引擎约束了 PyAV 兼容版本，避免新版本移除 `metadata_errors` 参数导致转写失败；请使用项目声明的依赖安装。

先安装 FFmpeg，确保 `ffmpeg` 和 `ffprobe` 都可在终端中运行：

```bash
# macOS
brew install ffmpeg

# Debian / Ubuntu
sudo apt-get update
sudo apt-get install ffmpeg

# Windows PowerShell（安装后重新打开终端）
winget install Gyan.FFmpeg
```

在本项目目录安装 Python 依赖：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[all]"
shengmu doctor
```

Windows 对应命令：

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[all]"
shengmu doctor
```

只使用一种引擎时，将 `[all]` 改成 `[local]` 或 `[openai]`。基础依赖仅为 GUI 服务依赖；AI 依赖按选项安装。

如果 FFmpeg 不在 PATH，可设置 `FFMPEG_BINARY`、`FFPROBE_BINARY` 为可执行文件的绝对路径。`.env.example` 仅是配置示例，项目**不会自动加载** `.env`。

## GUI

```bash
shengmu gui
```

自动打开 <http://127.0.0.1:8765>。选择视频 → 选择音轨和转写引擎 → 勾选格式 → 生成字幕 → 校对 → 保存并导出 → 下载文件。

可先选择 `examples/local-demo.mp4`，用 Tiny 模型和英语验证完整流程；该文件是用于测试的合成语音视频，同目录有实际本地识别生成的 SRT。

按上文安装到项目的 `.venv` 后，也可在 macOS 双击 `start-gui.command`、Windows 双击 `start-gui.bat` 启动。启动脚本默认将模型缓存放在项目的 `models` 目录；已有 `SHENGMU_MODEL_DIR` 配置时保留该配置。

```bash
shengmu gui --port 9000 --no-browser
```

GUI 是运行在本机的浏览器应用，文件上传到本机临时目录；本地模式不会上传音频到 AI 服务。单个文件上限为 2 GB，较大的视频使用 CLI 直接处理。模型首次下载需要网络，下载后可离线使用。浏览器不支持某个视频编码时仍能转写，但无法在 GUI 中播放该视频。

GUI 的文件、任务和编辑结果保存在当前服务会话，停止服务后会清理，务必先下载字幕。会话最多保留 30 个源文件、100 个任务；所有上传合计最多 4 GB，包含正在上传的文件。最多 5 个任务处理中或排队，实际转写串行运行，避免模型争用内存。替换文件会立即删除旧上传，也可点击“释放源文件缓存”；保留已完成任务的字幕和下载，但不允许删除仍用于活动任务的文件。超过 24 小时的已完成结果与不再使用的上传会在后续上传或创建任务时清理。

字幕编辑：点击“新增”在播放位置之后的空白区间插入字幕；将文字光标放在字幕中间后点击“拆”，优先使用字幕内的当前播放时间，否则按文字比例分配时间；“并”合并下一条。撤销保留最近 100 次操作，支持在文本输入框外按 Ctrl / Cmd + Z。修改时间或文本只更新相关行与时间轴块；播放定位使用缓存及二分查找。

GUI 模型只允许 `tiny`、`base`、`small`、`medium`、`large-v3` 和 `turbo`；自定义模型目录或仓库请使用 CLI。

## CLI

### 本地模型

```bash
shengmu transcribe "video.mp4" --engine local --model small --language zh \
  --format srt vtt ass txt json --output-dir subtitles
```

本机首次运行建议先使用 `tiny` 或 `base` 验证流程，再根据准确度需求选择 `small` / `medium` / `large-v3`。本地转写速度和内存取决于模型及硬件。

```bash
# Mac / 通用 CPU 默认使用 int8；不使用 CUDA 或 MPS
shengmu transcribe video.mp4 --model small --device cpu --compute-type int8

# NVIDIA GPU；需另行配置兼容的 CUDA / cuDNN
shengmu transcribe video.mp4 --model large-v3 --device cuda --compute-type float16

# 使用已下载的 faster-whisper / CTranslate2 模型目录
shengmu transcribe video.mp4 --model /path/to/model
```

可通过 `SHENGMU_MODEL_DIR` 指定模型缓存目录。默认使用模型库自己的缓存位置。

### OpenAI API

在启动 GUI 或 CLI **之前**配置 Key：

```bash
export OPENAI_API_KEY="你的 API Key"
shengmu transcribe video.mp4 --engine openai --language zh --format srt json
```

```powershell
$env:OPENAI_API_KEY="你的 API Key"
shengmu gui
```

API 模式固定使用 `whisper-1`，因为它支持字幕需要的结构化时间戳。`--model` 是本地模型选项，不会改变 API 模型。音频将发送到配置的 OpenAI 服务并消耗 API 额度。

可使用 `OPENAI_BASE_URL` 接入兼容服务，但服务必须实现 `whisper-1`、`verbose_json`、`segments` 和 `timestamp_granularities`。兼容网关的功能由服务方决定。

API 请求设有 120 秒超时及最多 2 次重试。以 16 kHz、单声道、16 位 WAV 分块，每块不超过约 19.2 MB；`--chunk-seconds 30` 至 `600` 可以调整最大分块长度。在切点前 5 秒内寻找至少 0.3 秒的低音量区间，优先在静音中间切分；全程保留所有音频采样，恢复完整时间轴。找不到静音时退回长度上限，因此连续语音的边界仍需校对。下一块的 prompt 包含用户提示及上一块最后最多 400 字符。兼容服务没有词级时间戳时使用片段时间按文本比例切分，精度低于真实词级对齐。

### 中文与质量选项

明确选择 `--language zh` 且没有自定义 prompt 时，默认提示使用简体普通话并添加标点。自动检测语言时不强加中文 prompt；这是识别提示而不是强制简繁转换，输出仍需校对。设置 `--prompt` 会替换默认提示。

本地引擎默认 `condition_on_previous_text=False`，减少长音频的重复；如需使用前文可加 `--condition-on-previous-text`，GUI 中也有对应选项。默认只过滤与已知幻觉短语完全匹配的孤立片段，例如 Amara.org 社区字幕和特定点赞订阅结尾；不会删除包含这些词语的正常对话。真实音频确实包含这些结尾时，用 `--no-filter-hallucinations` 或取消 GUI 勾选保留。该过滤不是通用幻觉检测。

字幕目标时长约 1–7 秒；短语音不会强行延长，避免覆盖后续语音。缺少词时间戳、异常词时间戳或超长单词时使用比例对齐兜底，不保证逐词精确。

### 多音轨、提示与脚本

```bash
shengmu inspect video.mkv
# --track 是 ffprobe 的流编号，不是第几个音轨；视频流也占编号
shengmu transcribe video.mkv --track 2 --language zh --prompt "声幕, 专业术语" --format srt

# --quiet：标准输出为结果 JSON，错误到标准错误
shengmu transcribe video.mp4 --quiet --format srt json

# 已有字幕转格式，不再次调用 AI
shengmu export subtitles/video.json --format vtt ass --output-dir converted

# 允许覆盖已有字幕，仍禁止覆盖输入文件
shengmu transcribe video.mp4 --format srt --overwrite
```

输出示例：

```json
{"segments": 42, "language": "zh", "files": ["/path/to/subtitles/video.srt"]}
```

命令成功返回 0，输入 / 依赖 / 转写错误返回 1，用户中断返回 130。也可使用 `python -m shengmu` 代替 `shengmu`。诊断时加 `--verbose`（放在子命令前后均可）输出经过 API Key / URL 脱敏的异常链。GUI 后台失败也会记录脱敏堆栈，不会向前端返回堆栈。

## 字幕数据格式

统一的 `Transcript` / `Segment` 使用**秒**作为时间单位。JSON 可以保存和重新转换，不依赖原视频或 AI：

```json
{
  "schema_version": 1,
  "source": "video.mp4",
  "duration": 10.0,
  "language": "zh",
  "engine": "local",
  "model": "small",
  "segments": [{"start": 0.5, "end": 2.1, "text": "你好，世界。"}],
  "metadata": {"audio_track": 1, "audio_offset": 0.0, "audio_duration": 10.0}
}
```

SRT / VTT 精度为毫秒，ASS 精度为百分之一秒。识别结果会重新切成适合显示的短字幕，不包含翻译、说话人分离或逐词高亮。**数据模型不支持重叠字幕**，包括两人同时说话的独立字幕轨：识别边界重叠会被规整，GUI 手动编辑会拒绝重叠。SRT 保留 `AT&T` 等原始文本，不做 HTML 转义；VTT 转义标记字符。ASS 样式字符会转换为全角字符，避免识别文本被误当作样式指令。

## 项目结构

```text
src/shengmu/
  media.py          ffprobe、FFmpeg、音轨和起始偏移
  engines.py        本地 / OpenAI 引擎、模型缓存与静音分块
  captions.py       词级字幕切分、换行及孤立幻觉过滤
  diagnostics.py    环境检查与脱敏诊断堆栈
  models.py         统一字幕数据和校验
  pipeline.py       两种入口共用的处理流程
  exporters.py      五种格式、临时写入和覆盖保护
  cli.py            命令行入口
  server.py         本地 API、任务队列、编辑和下载
  web/              无需前端构建的 GUI
tests/              格式、真实 FFmpeg、API 合约、GUI 接口测试
examples/           示例 JSON 及 SRT
```

添加新的 AI 服务时，实现 `Engine` 协议，并在 `get_engine` 注册即可。CLI 和 GUI 自动复用后续流程。

## 开发与测试

```bash
python -m pip install -e ".[all,dev]"
pytest -q --cov=shengmu --cov-report=term-missing --cov-fail-under=80 -W error
ruff check .
ruff format --check .
node --test tests/test_editor.cjs
node --check src/shengmu/web/app.js
node --check src/shengmu/web/editor.js
python -m build
```

测试使用合成的多音轨媒体和模拟的 API 响应，不会上传用户文件或调用付费 API。媒体测试需要 FFmpeg / ffprobe；OpenAI SDK 合约测试在未安装 SDK 时跳过。JavaScript 单元测试使用 Node.js 22 内置测试运行器，无需 npm 依赖或前端构建。开发依赖同时安装 httpx2 供新版 Starlette TestClient 使用，并保留 httpx 供 OpenAI SDK 合约测试使用。

GitHub Actions 在 Ubuntu、macOS、Windows × Python 3.10 / 3.12 上安装 FFmpeg、运行 pytest / ruff / JavaScript 测试，并构建发布包。配置 CI 不等于已经在全部平台实测；实际验证结果见 [验证记录](VALIDATION.md)。

## 实用边界

- 取消任务是协作式的：FFmpeg 可直接停止；本地模型加载 / 推理和已发出的 API 请求需等当前操作返回。已经开始的云请求可能计费。
- 首版为单用户本地工具，不适合作为公网多用户服务。没有持久任务数据库；CLI 输出持久保存在指定目录。
- 对音乐、口音、多人重叠语音和噪声较大的音频，字幕准确度及时间对齐仍需人工校对。
- 音轨起始偏移会恢复到视频时间轴；特殊时间戳断续或损坏的媒体建议先转换为常规格式。

## 参考文档

- [OpenAI 官方语音转写与时间戳文档](https://developers.openai.com/api/docs/guides/speech-to-text)
- [faster-whisper 官方项目](https://github.com/SYSTRAN/faster-whisper)
- [FFmpeg 官方文档](https://ffmpeg.org/ffmpeg.html)

MIT License。

---

## English

### Shengmu — AI subtitles for video and audio

Shengmu is a local browser application and CLI built on one Python transcription pipeline. FFmpeg extracts the selected audio track; **faster-whisper** runs locally, or **OpenAI whisper-1** transcribes through the configured API. Export **SRT, WebVTT, ASS, TXT, and JSON**.

### Features

- Inspect duration, audio stream IDs, languages, channel counts, and delayed-track offsets with ffprobe.
- Local CPU / NVIDIA CUDA transcription, language detection, vocabulary prompts, voice activity detection, and word timestamps. Reuse the most recently loaded model in the same process.
- Readable captions: prefer punctuation boundaries, at most two lines and seven seconds per cue; up to 20 characters per line for Chinese / Japanese / Korean and 42 for other text.
- API chunks are bounded to 30–600 seconds. Look for at least 0.3 seconds of quiet audio within the last five seconds before the limit, preserve every sample, and carry the previous chunk's final 400 characters into the next prompt.
- GUI drag-and-drop, track selection, progress, cancellation, video preview, text / timing edits, add, split, merge, delete, undo, save, and download. Playback uses a cached position and binary search; edits update only affected rows and timeline blocks.
- Local-only service, session-token protection for write requests, and server-side API keys. Uploaded source files can be released without losing finished subtitles.

### Installation

Python **3.11 or 3.12** is recommended; Python 3.10+ is supported by the project. Install FFmpeg and ffprobe first:

```bash
# macOS
brew install ffmpeg
# Debian / Ubuntu
sudo apt-get update
sudo apt-get install ffmpeg
```

On Windows, use `winget install Gyan.FFmpeg` in PowerShell and reopen the terminal.

From the repository directory, on macOS / Linux:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[all]"
shengmu doctor
shengmu gui
```

On Windows:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[all]"
shengmu doctor
shengmu gui
```

Replace `[all]` with `[local]` or `[openai]` to install only one engine. Use the declared dependencies: the PyAV version range avoids a known faster-whisper decoder incompatibility. Set `FFMPEG_BINARY` / `FFPROBE_BINARY` to absolute executable paths when they are not on PATH. `.env.example` is a reference only; `.env` is **not automatically loaded**.

### Browser workflow

The GUI labels remain in Chinese; this README is available in three languages. `shengmu gui` opens <http://127.0.0.1:8765>. Select a file → choose an audio track and engine → choose output formats → generate → proofread → save → download. Try `examples/local-demo.mp4` with Tiny and English. After installing into `.venv`, double-click `start-gui.command` on macOS or `start-gui.bat` on Windows. These scripts use the repository's `models` cache unless `SHENGMU_MODEL_DIR` is already set.

Use `shengmu gui --port 9000 --no-browser` to change the port or skip opening a browser. Unsupported browser codecs do not prevent transcription, but cannot be previewed.

GUI uploads stay in a local temporary directory: **2 GB per file, 4 GB total uploaded source storage**, including in-flight uploads; up to 30 source files, 100 jobs, and five active / queued jobs. Transcription runs serially. Replacing a source deletes its old upload; “释放源文件缓存” releases it manually. Active-job sources cannot be deleted. Finished subtitles remain editable and downloadable after release. Results and unused sources expire after 24 hours when another upload or job triggers cleanup. Stopping the service removes session data: download your results first.

“新增” inserts a cue into free time at or after the playback position. Place the text cursor inside a cue and press “拆” to split: use the current playback time if inside the cue, otherwise estimate by text position. “并” merges with the next cue. “撤销” restores the last operation, up to 100 operations; Ctrl / Cmd + Z works outside editable fields. “保存并导出” updates downloads. GUI models are restricted to `tiny`, `base`, `small`, `medium`, `large-v3`, and `turbo`; use the CLI for custom model paths or repositories.

### CLI examples

```bash
shengmu transcribe video.mp4 --model small --language zh \
  --format srt vtt ass txt json --output-dir subtitles
shengmu transcribe video.mp4 --device cpu --compute-type int8
shengmu transcribe video.mp4 --model large-v3 --device cuda --compute-type float16
shengmu transcribe video.mp4 --model /path/to/model
shengmu inspect video.mkv
shengmu transcribe video.mkv --track 2 --prompt "Names and terminology"
shengmu export subtitles/video.json --format vtt ass --output-dir converted
shengmu transcribe video.mp4 --quiet --format srt json
shengmu transcribe video.mp4 --overwrite --verbose
```

`--track` is the ffprobe stream ID, not the audio-track ordinal. Mac / CPU defaults are CPU + INT8; CUDA requires compatible CUDA / cuDNN and NVIDIA hardware. `SHENGMU_MODEL_DIR` selects the model download cache. Existing output files are protected unless `--overwrite` is given; the input file is always protected. `--quiet` writes result JSON to stdout; errors go to stderr. Exit codes: 0 success, 1 failure, 130 interruption. `python -m shengmu` is also supported. `--verbose`, before or after the subcommand, prints a redacted exception chain; GUI failures log redacted traces without returning stacks to the browser.

For OpenAI, set the key **before starting** the CLI or GUI:

```bash
export OPENAI_API_KEY="your API key"
shengmu transcribe video.mp4 --engine openai --language en --format srt json
```

PowerShell: `$env:OPENAI_API_KEY="your API key"`. API mode always uses `whisper-1`; `--model` only affects local transcription. Audio is sent to the configured service and may incur charges. `OPENAI_BASE_URL` may point to a compatible gateway, which must support `whisper-1`, `verbose_json`, segment timestamps, and `timestamp_granularities`. Requests use a 120-second timeout and up to two retries. `--chunk-seconds` sets the maximum chunk length (30–600); a 600-second mono 16 kHz / 16-bit WAV is about 19.2 MB. Without a nearby quiet interval, chunking falls back to the length limit: continuous-speech boundaries still need review. Without word timestamps, caption splitting estimates timing from segment text.

### Chinese and quality options

With `--language zh` and no custom prompt, the default prompt requests punctuated Simplified Mandarin. Language auto-detection does not impose a Chinese prompt. This is guidance, **not guaranteed Traditional-to-Simplified conversion**; `--prompt` replaces it.

Local `condition_on_previous_text` defaults to false to reduce repetition. Enable it with `--condition-on-previous-text` or the GUI checkbox. Known isolated hallucination phrases are filtered only on a complete normalized match, not when mentioned inside normal dialogue. Disable with `--no-filter-hallucinations` if the recording genuinely contains those phrases. This is not general-purpose hallucination detection.

Short speech is not artificially extended to one second. Missing / invalid word timestamps and unusually long words use proportional timing as a fallback; manual review is still necessary.

### Data, development, and limitations

JSON uses schema version 1 and timestamps in seconds; it can be edited and re-exported without the original media or another AI request. SRT / VTT use milliseconds, ASS uses centiseconds, and TXT has no timestamps. SRT retains raw text such as `AT&T`; VTT escapes markup; ASS style-control characters are neutralized.

**Overlapping captions are not supported**, including separate simultaneous-speaker cues. ASR overlaps are normalized, and manual overlaps are rejected. No translation, speaker diarization, or word highlighting is provided. Music, accents, noisy recordings, and overlapping speech require proofreading. Delayed audio offsets are restored; damaged or discontinuous media timestamps may need conversion first.

Cancellation is cooperative: FFmpeg can stop immediately, while model loading / inference or an in-flight API request must return first. Already-sent requests may be billed. This is a single-user local tool, not a public multi-user service; CLI outputs persist, GUI session data does not.

```bash
python -m pip install -e ".[all,dev]"
pytest -q --cov=shengmu --cov-report=term-missing --cov-fail-under=80 -W error
ruff check .
ruff format --check .
node --test tests/test_editor.cjs
node --check src/shengmu/web/app.js
node --check src/shengmu/web/editor.js
python -m build
```

Tests use synthetic media and mocked API responses, never paid API requests. FFmpeg is required for media tests. Node.js 22 runs editor tests without npm dependencies or a frontend build. `httpx2` supports the newer Starlette TestClient; `httpx` remains for OpenAI SDK contract tests. GitHub Actions covers Ubuntu / macOS / Windows and Python 3.10 / 3.12; configured CI is not a claim of completed cross-platform testing. See [validation records](VALIDATION.md) for what was actually exercised.

The pipeline is organized into `media.py`, `engines.py`, `captions.py`, `models.py`, `pipeline.py`, `exporters.py`, and `diagnostics.py`, with `cli.py`, `server.py`, and plain browser scripts as entry points. Implement the `Engine` protocol and register it in `get_engine` to add another provider. Licensed under MIT.

---

## 日本語

### 声幕 Shengmu — 動画・音声の AI 字幕ツール

声幕は、共通の Python 処理パイプラインを使うローカルブラウザー GUI と CLI です。FFmpeg で指定した音声トラックを抽出し、**ローカルの faster-whisper** または **OpenAI whisper-1 API** で文字起こしします。**SRT、WebVTT、ASS、TXT、JSON** に出力できます。

### 主な機能

- ffprobe による再生時間、音声ストリーム番号、言語、チャンネル数、音声開始オフセットの取得。
- CPU / NVIDIA CUDA、言語自動判定、固有名詞プロンプト、無音除外、単語タイムスタンプ。直近のモデルを同じプロセス内で再利用します。
- 句読点を優先して字幕を分割。字幕ごとに最大 2 行・7 秒、中国語 / 日本語 / 韓国語は 1 行 20 文字、それ以外は 42 文字まで。
- API 音声の最大分割時間は 30〜600 秒。上限直前の 5 秒間から 0.3 秒以上の静かな区間を探し、全サンプルを保持して分割します。前のブロックの末尾 400 文字を次のプロンプトに渡します。
- ドラッグ＆ドロップ、トラック選択、進捗、キャンセル、動画プレビュー、字幕テキスト / 時間編集、追加、分割、結合、削除、元に戻す、再出力。
- 再生中の字幕検索はキャッシュと二分探索を使用。編集時は変更した行とタイムラインだけを更新します。
- ローカル専用サービス、書き込み API のセッショントークン保護。API キーはサーバー側で保持し、ブラウザーに渡しません。

### インストール

**Python 3.11 または 3.12** を推奨します。プロジェクトの対応範囲は Python 3.10 以降です。最初に FFmpeg と ffprobe をインストールしてください。

```bash
# macOS
brew install ffmpeg
# Debian / Ubuntu
sudo apt-get update
sudo apt-get install ffmpeg
```

Windows では PowerShell で `winget install Gyan.FFmpeg` を実行し、ターミナルを開き直します。

プロジェクトのディレクトリで、macOS / Linux の場合：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[all]"
shengmu doctor
shengmu gui
```

Windows の場合：

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[all]"
shengmu doctor
shengmu gui
```

片方のエンジンだけ使う場合は `[all]` を `[local]` または `[openai]` に変更します。faster-whisper と PyAV の既知の互換性問題を避けるため、プロジェクトで指定した依存関係を使用してください。PATH にない場合、`FFMPEG_BINARY` / `FFPROBE_BINARY` に実行ファイルの絶対パスを指定できます。`.env.example` は設定例です。`.env` は**自動読み込みされません**。

### GUI の使い方

`shengmu gui` で <http://127.0.0.1:8765> が開きます。ファイル選択 → 音声トラック / エンジン選択 → 出力形式選択 → 字幕生成 → 校正 → 保存 → ダウンロードの順に操作します。動作確認には `examples/local-demo.mp4`、Tiny モデル、英語を使用できます。

プロジェクトの `.venv` にインストール済みなら、macOS の `start-gui.command` または Windows の `start-gui.bat` をダブルクリックして起動できます。これらは `SHENGMU_MODEL_DIR` が未設定の場合、プロジェクト内の `models` をキャッシュに使用します。`shengmu gui --port 9000 --no-browser` でポート変更 / ブラウザー自動起動の無効化ができます。ブラウザー非対応のコーデックでも文字起こしは可能ですが、動画プレビューはできません。

アップロード先は本機の一時ディレクトリです。**1 ファイル 2 GB、アップロード元ファイルの合計 4 GB**（アップロード中も含む）、最大 30 ファイル・100 タスク、処理中 / 待機中は最大 5 タスクです。文字起こしは直列実行します。ファイルの変更時は古いアップロードを削除し、「释放源文件缓存」で手動解放もできます。処理中のタスクが使用するファイルは削除できません。解放後も完成済み字幕の編集とダウンロードは可能です。

24 時間以上経過した完了結果や未使用ファイルは、次のアップロード / タスク作成時に整理します。サービス停止時にはセッションデータを削除するため、先に字幕をダウンロードしてください。

GUI の表示言語は中国語です。「新增」で再生位置以降の空き時間に字幕を追加します。文字カーソルを字幕の途中に置き、「拆」で分割します。現在の再生位置が字幕内ならその時刻を使い、それ以外では文字数比で時間を配分します。「并」で次の字幕と結合、「撤销」で直近 100 操作まで元に戻します。入力欄の外では Ctrl / Cmd + Z も使えます。「保存并导出」でダウンロード内容を更新します。

GUI で使用できるモデルは `tiny`、`base`、`small`、`medium`、`large-v3`、`turbo` のみです。任意のモデルディレクトリやリポジトリは CLI から指定してください。

### CLI の例

```bash
shengmu transcribe video.mp4 --model small --language ja \
  --format srt vtt ass txt json --output-dir subtitles
shengmu transcribe video.mp4 --device cpu --compute-type int8
shengmu transcribe video.mp4 --model large-v3 --device cuda --compute-type float16
shengmu transcribe video.mp4 --model /path/to/model
shengmu inspect video.mkv
shengmu transcribe video.mkv --track 2 --prompt "人名、製品名、専門用語"
shengmu export subtitles/video.json --format vtt ass --output-dir converted
shengmu transcribe video.mp4 --quiet --format srt json
shengmu transcribe video.mp4 --overwrite --verbose
```

`--track` は ffprobe のストリーム番号で、音声トラックの通し番号ではありません。Mac / CPU では CPU + INT8 が標準です。CUDA には NVIDIA GPU と対応する CUDA / cuDNN が必要です。`SHENGMU_MODEL_DIR` でモデルのダウンロードキャッシュを指定できます。

既存出力は `--overwrite` を指定した場合のみ上書きします。入力ファイルの上書きは常に禁止します。`--quiet` は結果 JSON を標準出力、エラーを標準エラーに出します。終了コードは成功 0、失敗 1、中断 130 です。`python -m shengmu` も使用できます。`--verbose` はサブコマンドの前後どちらにも指定でき、API キー / URL を伏せた例外チェーンを表示します。GUI の失敗も伏字処理したスタックをログに記録し、ブラウザーには返しません。

OpenAI を使う場合は、GUI / CLI を起動する**前**にキーを設定します。

```bash
export OPENAI_API_KEY="自分の API キー"
shengmu transcribe video.mp4 --engine openai --language ja --format srt json
```

PowerShell では `$env:OPENAI_API_KEY="自分の API キー"` を使います。API モードは常に `whisper-1` を使用し、`--model` はローカルモデルにのみ影響します。音声は設定したサービスに送信され、料金が発生する場合があります。

互換サービスには `OPENAI_BASE_URL` を設定できます。サービスは `whisper-1`、`verbose_json`、セグメントタイムスタンプ、`timestamp_granularities` に対応する必要があります。リクエストは 120 秒タイムアウト、最大 2 回再試行です。`--chunk-seconds` で最大分割時間を 30〜600 秒に設定します。600 秒の 16 kHz / モノラル / 16-bit WAV は約 19.2 MB です。近くに静かな区間がなければ時間上限で分割するため、連続発話の境界は校正してください。単語タイムスタンプがないサービスでは、セグメント内の文字数比から時間を推定します。

### 中国語と品質オプション

`--language zh` を指定し、独自プロンプトがない場合は、簡体字と句読点付きの普通話を促すプロンプトを使用します。言語自動判定時に中国語プロンプトを強制しません。これはヒントであり、**繁体字から簡体字への変換を保証するものではありません**。`--prompt` で既定値を置き換えられます。

繰り返しを抑えるため、ローカルの `condition_on_previous_text` は既定で false です。`--condition-on-previous-text` または GUI のチェックボックスで有効にできます。既知の幻覚字幕は、正規化した内容がフレーズ全体に一致する孤立セグメントのみ除外します。通常の会話に含まれる言及は削除しません。実際に録音に含まれる場合は `--no-filter-hallucinations` で無効化できます。汎用的な幻覚検出ではありません。

短い発話を無理に 1 秒まで延ばしません。単語タイムスタンプの欠落 / 異常や極端に長い単語は比例配分で補います。最終的な校正は必要です。

### データ・開発・制約

JSON はスキーマバージョン 1、時間単位は秒です。元動画や AI の再実行なしで編集 / 再出力できます。SRT / VTT はミリ秒、ASS は 1/100 秒の精度、TXT は時間情報なしです。SRT は `AT&T` などの元テキストを保持し、VTT はマークアップをエスケープ、ASS はスタイル制御文字を無害化します。

**重複する時間帯の字幕には対応していません**。複数話者の同時発話を独立した字幕として表現することもできません。認識結果の境界重複は調整し、手動編集の重複は拒否します。翻訳、話者分離、単語ハイライトは未対応です。音楽、訛り、雑音、同時発話では特に校正が必要です。音声開始オフセットは動画タイムラインに復元しますが、破損 / 不連続なタイムスタンプのメディアは事前変換を推奨します。

キャンセルは協調的です。FFmpeg は停止できますが、モデル読み込み / 推論や送信済み API リクエストは処理が戻るまで待つ必要があります。送信済みリクエストは課金される場合があります。本ツールはローカルの単一ユーザー向けで、公開マルチユーザーサービスではありません。CLI の出力は保存され、GUI のデータはセッション限りです。

```bash
python -m pip install -e ".[all,dev]"
pytest -q --cov=shengmu --cov-report=term-missing --cov-fail-under=80 -W error
ruff check .
ruff format --check .
node --test tests/test_editor.cjs
node --check src/shengmu/web/app.js
node --check src/shengmu/web/editor.js
python -m build
```

テストは合成メディアと模擬 API 応答を使い、有料 API を呼びません。メディアテストには FFmpeg が必要です。Node.js 22 の標準テスト機能でエディターを検証し、npm 依存やフロントエンドビルドは不要です。新版 Starlette の TestClient 用に `httpx2`、OpenAI SDK 契約テスト用に `httpx` を使用します。

GitHub Actions は Ubuntu / macOS / Windows と Python 3.10 / 3.12 を対象にします。ただし、CI 設定の追加は全 OS の実測完了を意味しません。実際の確認内容は [検証記録](VALIDATION.md) を参照してください。

構成は `media.py`、`engines.py`、`captions.py`、`models.py`、`pipeline.py`、`exporters.py`、`diagnostics.py` と、入口の `cli.py` / `server.py` / ブラウザースクリプトです。`Engine` プロトコルを実装して `get_engine` に登録すると、新しい認識サービスを追加できます。MIT ライセンスです。
