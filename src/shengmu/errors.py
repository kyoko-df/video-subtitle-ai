from __future__ import annotations

from .models import SubtitleError


class TranslationError(SubtitleError):
    def __init__(self, message, *, code="unknown", retryable=False, splittable=False, status=None):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.splittable = splittable
        self.status = status


def local_model_error(exc, *, downloading=False) -> SubtitleError:
    chain, current = [], exc
    while current is not None and len(chain) < 8:
        chain.append(current)
        current = current.__cause__ or current.__context__
    names = {type(item).__name__ for item in chain}
    text = " ".join(str(item) for item in chain).lower()
    if "out of memory" in text:
        message = (
            "模型所需内存 / 显存不足，请改用更小模型或降低精度；CUDA 可尝试 int8，或改为 CPU。"
        )
    elif "cudnn" in text or "cublas" in text:
        message = (
            "CUDA / cuDNN 库缺失或版本不兼容，请检查对应平台的 GPU 依赖；也可改用 CPU / int8。"
        )
    elif any(isinstance(item, PermissionError) for item in chain):
        message = "无法访问模型目录，请检查目录权限及 SHENGMU_MODEL_DIR。"
    elif any(isinstance(item, FileNotFoundError) for item in chain):
        message = "模型目录或文件不存在，请检查模型路径及 SHENGMU_MODEL_DIR。"
    elif "OfflineModeIsEnabled" in names or "offline mode" in text:
        message = "当前处于离线模式且缺少模型缓存，请先下载模型，或选择完整的本地模型目录。"
    elif (
        names
        & {
            "LocalEntryNotFoundError",
            "ConnectTimeout",
            "ReadTimeout",
            "ConnectionError",
            "ConnectError",
        }
        or "connecttimeout" in text
    ):
        message = "模型未在本机缓存，且无法连接模型下载服务；请检查网络 / 下载源，或选择已下载的模型目录。"
    elif "no space left" in text:
        message = "模型下载 / 加载失败：磁盘空间不足，请清理空间或更换模型缓存目录。"
    else:
        message = (
            "请检查模型下载源、缓存路径和服务日志。"
            if downloading
            else "本地模型转写失败。请检查模型文件、设备和计算精度；Mac 建议 cpu / int8。"
        )
    return SubtitleError(message)
