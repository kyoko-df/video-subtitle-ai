# 验证记录

验证日期：2026-10-02，macOS / Apple Silicon，Python 3.12.12。

## 自动化检查

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
