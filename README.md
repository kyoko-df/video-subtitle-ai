# 声幕 Shengmu · AI 视频字幕

一个可以直接开发和运行的 Python 项目：使用 FFmpeg 提取指定音轨，以 **本地 faster-whisper** 或 **OpenAI API** 生成带时间轴的字幕；提供本地浏览器 GUI 与 CLI，二者共用处理流程。

## 已实现

- 通过 ffprobe 读取时长、音轨编号、语言、声道和起始偏移；通过 FFmpeg 提取 16 kHz 单声道 PCM 音频。
- 本地 Whisper 模型转写，支持自动识别语言、CPU / NVIDIA CUDA、识别提示及静音过滤。
- OpenAI `whisper-1` 转写，请求结构化片段时间戳；长音频按最多 600 秒切分，恢复完整时间轴。
- 导出 **SRT、WebVTT、ASS、TXT、JSON**；TXT 为纯文本，其余保留时间信息。
- GUI 支持拖放文件、音轨选择、进度显示、取消、视频字幕预览、修改文本 / 时间、删除条目和重新导出。
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

GUI 的文件、任务和编辑结果保存在当前服务会话，停止服务后会清理，务必先下载字幕。会话最多保留 30 个源文件、100 个任务；最多 5 个任务处理中或排队，实际转写串行运行，避免模型争用内存。超过 24 小时的已完成结果会在后续上传或创建任务时清理。

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

API 请求设有 120 秒超时及最多 2 次重试。以 16 kHz、单声道、16 位 WAV 按 600 秒分块，每块约 19.2 MB；`--chunk-seconds 30` 至 `600` 可以调整分块长度。固定分块可能截断边界处的词语，校对时应关注分块交界处。

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

命令成功返回 0，输入 / 依赖 / 转写错误返回 1，用户中断返回 130。也可使用 `python -m shengmu` 代替 `shengmu`。

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

SRT / VTT 精度为毫秒，ASS 精度为百分之一秒。按实际识别片段输出，不包含翻译、说话人分离或逐词高亮。识别产生的边界小幅重叠会被规整，GUI 手动编辑会拒绝无效时间或重叠。ASS 样式字符会转换为全角字符，避免识别文本被误当作样式指令。

## 项目结构

```text
src/shengmu/
  media.py          ffprobe、FFmpeg、音轨和起始偏移
  engines.py        本地 / OpenAI 引擎与接口协议
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
pytest -q
ruff check .
```

测试使用合成的多音轨媒体和模拟的 API 响应，不会上传用户文件或调用付费 API。媒体测试需要 FFmpeg / ffprobe；OpenAI SDK 合约测试在未安装 SDK 时跳过。实际本地模型测试记录见 [验证记录](VALIDATION.md)。

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
