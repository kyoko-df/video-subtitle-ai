# 声幕 Shengmu · AI 视频字幕

[English](README.md) · **简体中文** · [日本語](README.ja.md)

一个可以直接开发和运行的 Python 项目：使用 FFmpeg 提取指定音轨，以 **本地 faster-whisper** 或 **OpenAI API** 生成带时间轴的字幕；提供本地浏览器 GUI 与 CLI，二者共用处理流程。

## 已实现

- 通过 ffprobe 读取时长、音轨编号、语言、声道和起始偏移；通过 FFmpeg 提取 16 kHz 单声道 PCM 音频。
- 本地 Whisper 模型转写，支持自动识别语言、CPU / NVIDIA CUDA、词级时间戳、识别提示及静音过滤；复用最近使用的一个模型。
- 按词级时间戳重新切分字幕，优先在标点处断开；中文 / 日文每行最多 20 字符，其他语言每行最多 42 字符，每条最多 2 行、7 秒。
- OpenAI `whisper-1` 转写，请求词级和片段时间戳；长音频按最多 600 秒分块，优先在边界前 5 秒内的静音处切分，下一块携带前文提示。
- 导出 **SRT、WebVTT、ASS、TXT、JSON**；TXT 为纯文本，其余保留时间信息。
- GUI 支持拖放文件、批量转写、任务历史 / 重试 / ZIP 下载、项目持久化和自动草稿恢复。
- 预览与校对支持新增 / 拆分 / 合并 / 删除 / 撤销 / 重做、查找替换、循环播放、播放速度、整体偏移、波形定位、时间轴缩放与拖动调时。
- 自定义排版规则与质量提示，字幕样式、双语字幕、说话人标注、逐词高亮，以及烧录视频 / 可切换字幕轨导出。
- 可选 OpenAI / 本机 LM Studio 文本翻译与本地 pyannote 说话人识别；模型下载、进度、管理和常用配置。
- CLI 支持转写、翻译、一步双语导出、查看音轨、JSON 格式转换和环境检查；拒绝意外覆盖输出或输入文件。
- 长视频翻译支持逐批检查点、恢复、有限重试、递归拆批和并发 1 / 2；工作台可配置超时、输出上限与模型支持的思考模式。
- ASR 提供减少重复 / 轻声方案与高级阈值；疑似重复循环先标记，支持试听、批量删除和撤销。
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

GUI 是运行在本机的浏览器应用，普通上传保存到本机项目目录；也可直接读取已连接的 SMB / 本地视频。本地模式不会上传音频到 AI 服务。单个上传文件上限为 2 GB，更大的视频可直接填写路径或使用 CLI。模型首次下载需要网络，下载后可离线使用。浏览器不支持某个视频编码时仍能转写，但无法在 GUI 中播放该视频。

GUI 默认把项目、上传源文件、字幕和草稿保存在当前目录的 `.shengmu/workspace`，停止和重启服务后仍可从任务列表继续校对。用 `shengmu gui --workspace /path/to/projects` 或 `SHENGMU_WORKSPACE` 更改目录；同一项目目录只允许一个服务进程。处理中断的任务会标记为失败，可在任务列表重试。

最多保留 30 个源文件、100 个任务；上传源文件合计最多 4 GB，包含正在上传的文件。最多 5 个转写任务处理中或排队，处理串行运行。更换文件会保留旧项目；在“源文件缓存”中释放不用的源文件，或删除旧任务腾出任务名额。持久项目不会自动过期。释放源文件后保留已完成字幕，但无法播放、重试或导出视频；字幕、视频、模型和波形缓存另占磁盘空间。

### SMB / NAS 视频

在“源文件 → 直接读取本地 / SMB 视频”中填写完整视频路径，点击“直接读取视频”。程序只保存路径引用，预览、音轨提取和视频导出直接读取源文件；字幕、草稿、波形缓存和导出视频保存到本机项目目录。源视频不复制到上传缓存，因此不受 2 GB 单文件 / 4 GB 上传空间限制，仍计入 30 个源文件名额；临时提取音频和导出视频需要本机可用空间。

- **macOS**：先在 Finder 按 ⌘K，连接 `smb://服务器/共享名` 并登录，再填写挂载后的文件路径，例如 `/Volumes/共享名/视频.mp4`。可在 Finder 选中文件，按 ⌥⌘C 复制路径。连接方式见 [Apple 官方说明](https://support.apple.com/en-sg/guide/mac-help/mchlp1140/mac)。
- **Windows**：先在文件资源管理器中连接并登录共享，再填写 `\\服务器\共享名\视频.mp4`，或映射盘路径 `Z:\视频.mp4`。使用启动本工具的同一系统账号连接共享，避免映射盘在其他账号 / 提权进程中不可见。路径格式见 [Microsoft 官方说明](https://learn.microsoft.com/dotnet/standard/io/file-path-formats)。
- **Linux**：先用系统挂载 SMB 共享，再填写挂载点下的完整文件路径。

账号密码由系统管理，工具不接收或保存 SMB 凭据，也不直接连接 `smb://` 地址。共享断开时仍可校对和下载已生成字幕；重新连接同一路径后恢复读取。源文件大小或修改时间改变时，旧引用停止读取，请再次添加视频以刷新音轨 / 时长。点击“移除视频引用”只删除本机引用和波形缓存，不删除共享盘或本地原视频。源文件列表可重新打开已有引用。

字幕编辑：点击“新增”在播放位置之后的空白区间插入字幕；将文字光标放在字幕中间后点击“拆”，优先使用字幕内的当前播放时间，否则按文字比例分配时间；“并”合并下一条。撤销保留最近 100 次操作，支持在文本输入框外按 Ctrl / Cmd + Z。修改时间或文本只更新相关行与时间轴块；播放定位使用缓存及二分查找。

GUI 模型只允许 `tiny`、`base`、`small`、`medium`、`large-v3` 和 `turbo`；自定义模型目录或仓库请使用 CLI。

### 工作台新功能

- **自动草稿**：编辑后约 0.7 秒保存草稿，同时保留浏览器本地备份以应对断连。刷新或重启后恢复草稿；“保存并导出”才更新可下载字幕。多页面编辑通过版本号防止旧页面覆盖新版本，冲突时重新打开项目。撤销 / 重做历史仅保留在当前页面。
- **批量与历史**：“批量添加并转写”使用当前配置及各文件第一条音轨。队列满时等待空位；“停止添加”停止后续上传，已排队任务继续。任务列表可重新打开、取消、重试和删除。ZIP 包含已完成任务最近保存的字幕，各任务使用独立子目录。
- **校对**：文字查找替换使用精确匹配；整体时间偏移同时移动词时间，越界会拒绝。循环当前句、0.5–2× 播放、左右箭头切换字幕和空格播放均可用。波形基于所选音轨并恢复轨道偏移；拖字幕块移动整条、拖两端改变边界。文字修改、拆分及边界调整会清除不再有效的词对齐；文字修改会清除对应旧译文。
- **排版与质检**：设置 CJK / 其他语言每行长度、最多行数、最短 / 最长秒数和每秒字数。质检对原文和译文提示过长、过短、阅读过快及重叠，点击提示定位。重新排版沿用词时间切分；重新切开的字幕会清除译文，需要重新翻译。
- **样式与视频**：字体、字号、颜色、位置和边距用于预览、ASS 和烧录。导出内容可选原文 / 译文 / 双语，可显示说话人名称。烧录视频编码为 H.264 / AAC；封装字幕轨复制原视频编码、转换音频为 AAC，字幕为 MP4 mov_text。源视频编码若无法封装到 MP4，会给出 FFmpeg 错误，可改用烧录。两种方式保留原媒体全部音轨，长视频导出可取消。
- **逐词高亮**：保存真实词时间戳；缺失对齐时使用带 `estimated=true` 标记的估算时间。原文预览与 ASS / 烧录支持逐词高亮；译文不伪造词级对齐。手动改文后高亮会退回整句，重新识别可恢复。
- **模型**：展示模型是否可用、磁盘大小及下载进度。下载固定官方 / 已知 CTranslate2 仓库的指定版本，并可续传。管理器可删除其下载的模型；已有共享 Hugging Face 缓存只显示为可用，避免删除其他程序共享的模型。使用中的本地模型禁止删除。保存常用配置后下次启动自动加载。

### 翻译和自动说话人识别

翻译支持 **OpenAI API** 和 **LM Studio 本地**，在工作台的“翻译方式”中选择。两种方式都按字幕编号核对结果，不修改原文、时间、词时间或说话人；全部批次成功才更新译文，失败或取消保留原字幕及已有译文。也可直接手动填写 / 修改译文。

OpenAI 方式使用服务器已有 `OPENAI_API_KEY` / `OPENAI_BASE_URL`，通过 Responses API 的严格 JSON Schema 输出。默认文本模型为 `gpt-4o-mini`，可在界面更改。兼容服务需支持 **Responses / Structured Outputs**；转写兼容接口本身不代表支持翻译。字幕文本会发送到配置的服务并使用 API 额度。

LM Studio 方式先完成以下准备：

1. 从 [LM Studio 官网](https://lmstudio.ai/download) 安装应用，在其中下载并加载支持目标语言及结构化输出的**文字对话模型**。语音识别的 Whisper 模型不能用于这里的文本翻译。
2. 在 LM Studio 的 **Developer** 页面开启 **Start server**，默认端口为 `1234`，建议模型上下文至少 `8192`。模型是否支持结构化输出及实际翻译质量取决于所选模型，见 [LM Studio 结构化输出文档](https://lmstudio.ai/docs/developer/openai-compat/structured-output)。
3. 在工作台选择“LM Studio 本地”，点击“刷新本地模型”，选择或填写模型标识，设置目标语言，再点击“翻译字幕”。已知嵌入模型不会显示；可查看加载状态和服务能力。保存常用配置可记住翻译方式、模型、语言和翻译设置。

本地方式无需 `[openai]` 依赖或 OpenAI Key。默认地址是 `http://127.0.0.1:1234/v1`，`LM_STUDIO_BASE_URL` 仅接受本机 HTTP 的 `localhost`、`127.0.0.1` 或 `::1`；认证时设置 `LM_STUDIO_API_KEY`，Key 不返回浏览器。应用不代为安装 LM Studio 或下载文字模型；原有模型管理器用于语音识别模型。

模型列表优先使用原生 v1，旧版接口回退到 v0 / 兼容接口。思考采用“模型默认”时使用 `/v1/chat/completions` 的严格 JSON Schema；显式控制思考时使用模型声明支持的原生 `/api/v1/chat`，通过提示词要求 JSON，再严格核对字幕编号。原生接口未声明 JSON Schema 参数，因此这种模式依靠程序校验，格式失败会拆批。参见 [模型能力](https://lmstudio.ai/docs/developer/rest/list) 和 [原生思考设置](https://lmstudio.ai/docs/developer/rest/chat)。

### 长视频翻译与恢复

工作台展开“翻译设置与恢复”可调整以下参数，CLI 提供对应选项：

| 参数            | 默认                       | 范围 / 用途                                           |
| --------------- | -------------------------- | ----------------------------------------------------- |
| 并发            | 1                          | 1 / 2；服务报告容量不足时拒绝，未报告时可能由服务排队 |
| 每批字幕 / 字符 | 8 / 2000                   | 1–40 / 100–12000；单条字幕不拆开                      |
| 临时故障重试    | 2                          | 0–3；认证、配置和超时不会盲目重试                     |
| 超时            | 本地 180 秒、OpenAI 120 秒 | 5–3600 秒；本地可用 `LM_STUDIO_TIMEOUT` 改默认值      |
| 输出上限        | 4096 tokens                | 128–32768；过少可能使正文截断                         |
| 模型思考        | 模型默认                   | 仅开放服务报告支持的选项；旧版本可在 LM Studio 中设置 |
| 恢复            | 开启                       | 只翻译当前字幕 / 设置尚未完成的部分                   |

每批通过校验后保存续跑缓存；失败或取消不会修改正式字幕，已保存的译文下次可恢复。CLI 默认将译文检查点和一键流程的 ASR 缓存保存在系统用户缓存目录的 `shengmu/checkpoints` 中，改变 `--output-dir` 后仍可续跑，不向共享输出目录新增 `.translation-cache`。使用 `--checkpoint-dir <目录>` 或 `SHENGMU_CHECKPOINT_DIR` 指定缓存目录，命令行选项优先。工作台继续使用各项目目录的 `.translation-cache/`，随项目删除。

升级后首次在原输出目录续跑，会读取并复制该目录旧的 `.translation-cache`，保留原文件；之后可以更换输出目录。直接选择旧缓存位置可使用 `--checkpoint-dir subtitles/.translation-cache`。修改原文、时间、目标语言、模型标识或思考模式会隔离旧检查点；更换同一标识背后的权重 / 模板时请用 `--no-resume` 重新翻译。超时、输出上限、批大小、重试和并发可以调整后继续。缓存包含字幕文本，可删除指定缓存目录清理；不要让多个 CLI 进程同时写同一检查点。

上下文、截断和格式错误会拆半批次，单条仍失败时明确报出编号。进度显示完成字幕、恢复数量、运行时间和重试 / 拆批次数；取消需等待当前请求返回或超时，之后停止提交新批次。并发 2 的实际速度由模型格式、服务版本、内存和服务设置决定，不能保证报告中的 2.1 倍收益。

在“识别提示与质量选项”可选择 `standard`、`less-repetition` 或 `soft-speech`，并覆盖压缩率、平均概率、无语音和 VAD 阈值。默认保持 `standard`，切换预设不保证消除无语音幻觉。后两套方案需要试听验证：减少重复可能漏低置信度对白，轻声方案可能增加噪声字幕。检测保留诊断分数，同时检查跨字幕的短词 / 碎片循环；长时间低字速单独标为时间轴可疑，估算词时间会明确提示。正常拖长音不作为单独的低字速疑点。检测仅标记，不能代替试听；质量检查可定位，删除标记字幕可撤销。JSON v2 包含可选 `diagnostics` 和 `suspicions`，旧 JSON 仍可读取。

`shengmu doctor` 检查实际烧录滤镜和编码器，无网络请求；`shengmu doctor --network` 额外探测模型下载服务及本机 LM Studio，不下载模型或执行推理。烧录需要 `ass` / `libx264` / `aac`，字幕轨需要 `aac` / `mov_text`，不依赖 `drawtext`。缺少能力时工作台禁用相应导出，后端也提前拒绝。程序在导入引擎前默认设置 `ORT_DISABLE_TELEMETRY=1`，不会删除已有文件。

`shengmu doctor --output-dir <目录>` 使用自己的临时文件探测写入、替换、硬链接及独占创建，结束后清理；目录不存在时会创建。转写 / 翻译任务开始前也会自动预检输出目录。SMB 不支持硬链接时，不覆盖导出回退到独占创建，仍拒绝覆盖已有文件；复制失败会清理本次创建的半成品。回退期间其他程序可能暂时读到不完整文件，替换探测成功也不代表网络存储的持久性保证。翻译回调按实际进度变化发送，等待期间每两秒更新耗时；CLI 去重并节流，保留完成和重试 / 拆批提示。

脚本轮询 **转写看 `job.status`，翻译 / 烧录 / 封装看 `job.operation.status`**。翻译时转写状态仍为 `done`；不能据此判断翻译完成。操作终态为 `done` / `error` / `cancelled`，翻译详情在 `operation.stats`。重启后中断操作保留设置和进度，标记 `error` / `interrupted=true`，同设置继续可恢复检查点。

自动区分说话人是额外依赖，建议独立 Python 3.11 / 3.12 环境：

```bash
python -m pip install -e ".[local,openai,diarization]"
export HF_TOKEN="你的 Hugging Face Token"
shengmu gui
```

首次使用需在 [Community-1 模型页面](https://huggingface.co/pyannote/speaker-diarization-community-1) 接受访问条款，然后由程序下载模型并在本机推理；本项目不会代你接受条款。也可设置 `SHENGMU_DIARIZATION_MODEL=/path/to/community-1` 使用离线模型目录。已知人数时可填写说话人数，程序按词时间和说话人区间切分字幕；名称可在校对行中修改。未安装引擎或未配置权限时会给出具体提示，普通转写和手动标注仍可使用。基础 `[all]` 不含体积较大的 pyannote / PyTorch。

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

# 已有 JSON 翻译为中文，默认输出双语 SRT + JSON
shengmu translate subtitles/video.json --target zh --provider lmstudio --model your-model
# 同命令重跑默认恢复；可调整超时 / 并发后继续
shengmu translate subtitles/video.json --target zh --model your-model --timeout 600 --concurrency 2
# 一步：转写 → 翻译 → 双语导出，翻译失败后重跑可恢复 ASR
shengmu transcribe video.mp4 --language ja --model medium --translate-to zh \
  --translation-model your-model --translation-timeout 600 --export-mode bilingual --format srt json
# 从已有译文重新导出，不调用模型
shengmu export subtitles/video.translated.json --export-mode bilingual --format srt

# 允许覆盖已有字幕，仍禁止覆盖输入文件
shengmu transcribe video.mp4 --format srt --overwrite
```

输出示例：

```json
{ "segments": 42, "language": "zh", "files": ["/path/to/subtitles/video.srt"] }
```

命令成功返回 0，输入 / 依赖 / 转写错误返回 1，用户中断返回 130。也可使用 `python -m shengmu` 代替 `shengmu`。诊断时加 `--verbose`（放在子命令前后均可）输出经过 API Key / URL 脱敏的异常链。GUI 后台失败也会记录脱敏堆栈，不会向前端返回堆栈。

## 字幕数据格式

统一的 `Transcript` / `Segment` 使用**秒**作为时间单位。JSON 可以保存和重新转换，不依赖原视频或 AI：

```json
{
  "schema_version": 2,
  "source": "video.mp4",
  "duration": 10.0,
  "language": "zh",
  "engine": "local",
  "model": "small",
  "segments": [{ "start": 0.5, "end": 2.1, "text": "你好，世界。" }],
  "metadata": { "audio_track": 1, "audio_offset": 0.0, "audio_duration": 10.0 }
}
```

SRT / VTT 精度为毫秒，ASS 精度为百分之一秒。JSON v2 保留 `words`（start / end / text / estimated）、`speaker` 和 `translation`，兼容读取 v1。默认拒绝重叠字幕，勾选“允许重叠语音字幕”后可保存按开始时间排序的重叠字幕；识别边界仍默认规整。说话人识别区分发言者，不会分离同时发声的声源或恢复被覆盖的对话。SRT 保留 `AT&T` 等原始文本，不做 HTML 转义；VTT 转义标记字符。ASS 样式字符会转换为全角字符，避免识别文本被误当作样式指令。

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
node --check src/shengmu/web/studio.js
python -m build
```

测试使用合成的多音轨媒体和模拟的 API 响应，不会上传用户文件或调用付费 API。媒体测试需要 FFmpeg / ffprobe；OpenAI SDK 合约测试在未安装 SDK 时跳过。JavaScript 单元测试使用 Node.js 22 内置测试运行器，无需 npm 依赖或前端构建。开发依赖同时安装 httpx2 供新版 Starlette TestClient 使用，并保留 httpx 供 OpenAI SDK 合约测试使用。

GitHub Actions 在 Ubuntu、macOS、Windows × Python 3.10 / 3.12 上安装 FFmpeg、运行 pytest / ruff / JavaScript 测试，并构建发布包。配置 CI 不等于已经在全部平台实测。

## 实用边界

- 取消任务是协作式的：FFmpeg 可直接停止；本地模型加载 / 推理和已发出的 API 请求需等当前操作返回。已经开始的云请求可能计费。
- 单用户本地工具，不适合作为公网多用户服务。GUI 使用原子写入的本地项目记录，CLI 输出保存在指定目录。
- 对音乐、口音、多人重叠语音和噪声较大的音频，字幕准确度及时间对齐仍需人工校对。
- 音轨起始偏移会恢复到视频时间轴；特殊时间戳断续或损坏的媒体建议先转换为常规格式。

## 参考文档

- [OpenAI 官方语音转写与时间戳文档](https://developers.openai.com/api/docs/guides/speech-to-text)
- [faster-whisper 官方项目](https://github.com/SYSTRAN/faster-whisper)
- [FFmpeg 官方文档](https://ffmpeg.org/ffmpeg.html)

MIT License。
