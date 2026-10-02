# 验证记录

验证日期：2026-10-02–03，macOS / Apple Silicon，Python 3.12.12。以下按版本保留验证历史，最新工作台功能见文末 0.2.0 记录。

## 首轮自动化检查

- `pytest`：**29 项通过**，没有跳过。
- `ruff check`：通过；Python 源码已格式化。
- JavaScript 语法检查：通过。
- 构建 wheel：成功；从 wheel 安装后可读取 GUI 首页及 JavaScript，确认静态文件已打包。

测试覆盖：五种字幕格式、时间跨分钟 / 小时、极短字幕、非法与重叠时间、JSON 往返、覆盖保护、真实多音轨 FFmpeg 提取、延迟音轨对齐、失败后的临时音频清理、取消、CLI 查看音轨与转格式、OpenAI SDK 多段 multipart 请求及时间还原、缺少时间戳的兼容服务、GUI 上传 / 任务 / 编辑 / 下载，以及本地解码器兼容性。

测试输出有一条第三方 Starlette / httpx 的弃用提示，不影响测试通过。

## 实际本地模型

使用系统语音合成生成一段英语测试音频，将其合成为视频；使用 **faster-whisper Tiny / CPU / INT8** 跑完整 CLI，成功导出 SRT、VTT、ASS、TXT、JSON。

识别结果为 3 条：

| 时间（秒） | 字幕 |
| --- | --- |
| 0.0–4.0 | Hello everyone! This is a test of automatic video subtitles. |
| 4.0–7.0 | The subtitles should include accurate timestamps. |
| 7.0–8.0 | Thank you for listening. |

测试视频和实际生成的 SRT 在 `examples/local-demo.mp4`、`examples/local-demo.srt`。

初次安装时发现 PyAV 19 移除了 faster-whisper 1.2.1 使用的 `metadata_errors` 参数，真实转写会失败。已为本地引擎增加 PyAV 兼容范围，实际验证使用 **PyAV 15.1.0**；补充解码测试防止回归。该变更依据 [PyAV 19 发布说明](https://github.com/PyAV-Org/PyAV/releases/tag/v19.0.0) 及实际失败记录。

## 浏览器实际操作

用 Chromium 运行真实本地 GUI 流程，完成：

1. 上传合成语音视频，读取音轨。
2. 选择 Tiny / 英语，生成 3 条字幕。
3. 修改第一条字幕，保存并重新导出。
4. 下载 SRT，确认下载内容包含修改后的文字。
5. 在 1440px 桌面与 390px 手机宽度检查布局，手机没有水平溢出，浏览器没有脚本错误。

界面截图：[初始工作台](docs/gui.png)、[生成与编辑结果](docs/gui-result.png)、[手机布局](docs/gui-mobile.png)。截图中的“Edited subtitle…”是浏览器保存验证使用的修改内容，不是原始识别结果。

## OpenAI 模式

使用真实 OpenAI SDK 与模拟 HTTP 传输验证请求参数、文件分块、时间轴偏移、取消、错误反馈和缺失时间戳。**未调用真实付费 OpenAI API**：测试环境未配置用户 API Key。实际使用时需配置 `OPENAI_API_KEY`；账号权限、额度和网络连通性由使用环境决定。

## 主要依赖版本

| 依赖 | 本次验证版本 |
| --- | --- |
| FastAPI | 0.142.2 |
| Uvicorn | 0.54.0 |
| faster-whisper | 1.2.1 |
| CTranslate2 | 4.8.2 |
| PyAV | 15.1.0 |
| OpenAI Python SDK | 2.54.0 |
| onnxruntime | 1.30.0 |

已在 macOS 验证。Windows / Linux 入口与依赖配置已提供，但本次未在这些系统实测。

## 问题修复后验证

同日在 macOS / Apple Silicon、Python 3.12.12、Node.js 22.20.0 上重新验证：

- `pytest -q --cov=shengmu --cov-report=term-missing --cov-fail-under=80 -W error`：**53 项通过**，没有跳过或警告，Python 行覆盖率 **88.19%**。
- `ruff check .`、`ruff format --check .`：通过。
- `node --test tests/test_editor.cjs`：**7 项通过**；单独使用 Node 内置覆盖率功能验证 `editor.js`，行 / 函数覆盖率 100%，分支覆盖率 93.33%。这不代表整个 DOM 应用脚本有 100% 覆盖率。
- 两个 JavaScript 文件的语法检查、Prettier 3.6.2 格式检查及 `git diff --check`：通过。
- `python -m build`：成功生成 sdist 与 wheel，新增的字幕切分模块、诊断模块和 `web/editor.js` 均已打包；版本来自 `shengmu.__version__`。
- `pip check`：没有依赖冲突。
- 开发依赖增加 httpx2 2.13.1，消除了新版 Starlette TestClient 使用 httpx 时的弃用警告；OpenAI SDK 模拟传输仍使用 httpx。

新增回归覆盖：中文 / 英文的词级分段、两行与行长 / 时长限制、标点保留、缺失或部分词时间戳的兜底、孤立幻觉过滤与正常对话保留、本地模型缓存及质量开关、API 静音切点不丢采样、上下文 prompt、API 词时间对齐、超限分块清理、SRT 原始文本与 VTT 转义、上传释放及活动任务保护、会话存储上限、线程池写入、GUI 模型白名单、IPv6 Host、后台堆栈和 CLI 脱敏异常链。

### 修复后的真实浏览器操作

使用已有 Tiny 模型，设置离线模式，不下载新模型或调用付费服务。Chromium 中处理 `examples/local-demo.mp4`，得到 3 条按词时间重新排版的字幕，成功导出五种格式。实际边界为 0.0–3.6、4.1–6.7、7.06–7.98 秒；第一、二条文本自动换行。

完成新增、拆分、合并、删除、撤销、文本编辑、保存和下载内容校验；确认 `AT&T <literal> edited subtitle` 在 SRT 中保持原样。检查单行编辑 / 拆分时其他字幕行的 DOM 节点未被重建。替换文件后确认旧上传已删除，而旧任务字幕仍可下载；手动释放新源文件后仍可读取原任务字幕。

使用 2,000 条合成字幕检查大列表：首次列表与时间轴渲染约 243 ms，100 次同步播放定位更新约 1 ms，高亮类属性变更 396 次（只更新旧 / 新的行和时间轴节点）。这是本机单次合成检查，不是跨硬件性能保证。

390px 手机宽度检查无横向页面溢出。实际操作没有脚本异常；测试删除已释放媒体时产生的预期 HTTP 404 不作为脚本错误。

### CI 与仍未验证的内容

已添加 GitHub Actions：Ubuntu、macOS、Windows × Python 3.10 / 3.12，安装 FFmpeg，运行 Python / JavaScript 测试、ruff 和构建。**尚未触发远端 CI，Windows / Linux 仍未在本次会话中实测**。

**未调用真实 OpenAI 付费 API**，也未测试 CUDA、BatchedInferencePipeline 或大型模型。中文简体 / 标点提示的配置与过滤逻辑已有回归测试，但不宣称所有中文录音的实际识别质量已验证。数据模型仍不支持重叠字幕，已在三种语言的 README 中明确说明。

## 0.2.0 工作台功能验证（2026-10-03）

本次实现项目持久保存及草稿恢复、批量任务及 ZIP、编辑增强、波形及拖动、可配置排版与质检、字幕样式及视频导出、翻译及双语、模型管理、说话人标注及可选自动识别、词时间及逐词高亮。未增加已有字幕导入界面。JSON v2 支持词时间、说话人和译文，并可读取 v1；手动重叠字幕需显式开启，自动识别不执行重叠声源分离。

### 自动检查

- Python：**69 项通过**，无跳过或警告，行覆盖率 **90.68%**；命令为 `pytest -q --cov=shengmu --cov-report=term-missing --cov-fail-under=80 -W error`。
- 编辑器：`node --test tests/test_editor.cjs` **11 项通过**。
- `ruff check .`、`ruff format --check .`、三个 JavaScript 文件的语法检查、Prettier 格式检查和 `git diff --check`：通过。
- `python -m build --no-isolation`：成功构建 0.2.0 的 wheel 和源码包，新增模块与 `web/studio.js` 均包含在包内。使用当前虚拟环境的构建工具，避免隔离构建再次联网安装依赖。
- `pip check`：无依赖冲突。

新增回归覆盖：持久项目 / 草稿 / 偏好 / 媒体重启恢复、进程目录锁、损坏清单容错、中断任务重试、版本冲突拒绝、波形缓存、重新排版、ZIP 文件目录隔离及清理、后台操作锁与取消、翻译按编号严格校验和失败保留原字幕、模型安装 / 取消 / 删除及状态一致性、说话人按词切分、词时间及 ASS 高亮、整体偏移 / 替换 / 撤销 / 重做 / 重叠质检。

视频测试调用真实 FFmpeg / FFprobe：分别验证烧录和 MP4 字幕轨，检查视频流、保留两条音轨、字幕轨的 mov_text 编码及临时文件清理。翻译使用真实 OpenAI SDK 搭配模拟 HTTP 响应，验证分批 Structured Outputs 请求、错误返回及取消；模型下载和 pyannote 调用使用模拟后端。

### 实际浏览器与本地转写

使用 `examples/local-demo.mp4`、已有 Tiny / CPU / INT8 模型和离线模式，真实浏览器完成：

1. 生成 3 条含词时间的字幕，加载音频波形。
2. 修改原文、说话人和译文，查找替换、撤销 / 重做；自动草稿在刷新后恢复。
3. 保存双语字幕，调用真实 FFmpeg 生成烧录视频；重启服务后恢复源视频、项目、字幕和视频下载链接，并继续生成 MP4 字幕轨。
4. 通过批量入口添加测试文件并完成转写；确认多文件选择入口可用。
5. 原文预览逐词高亮、循环当前句、整体时间偏移及撤销；拖动第二条字幕后，其时间由 4.100–6.700 变为 4.195–6.795 秒，撤销恢复原值。
6. 390px 手机宽度下无页面水平溢出；最后检查未发现浏览器脚本错误。

### 本次未实测的范围

未调用真实付费 OpenAI 转写 / 翻译 API，未下载新 ASR / pyannote 模型，未安装和运行真实 pyannote / PyTorch。自动说话人识别需要额外 `[diarization]` 依赖和已获得模型访问权的 `HF_TOKEN`，或已有本地模型目录。模拟测试不代表已验证这些服务的网络、额度、授权或实际识别质量。

Windows / Linux、CUDA、大型模型和远端 CI 仍未在本次会话中实际运行。以上浏览器测试为本机合成样例，不代表所有视频编码、语种、长视频或重叠语音均已验证。
