from __future__ import annotations

import json
import math
import os
from http.client import HTTPConnection, HTTPException
from urllib.parse import urlsplit

from .errors import TranslationError
from .media import check_cancel
from .models import SubtitleError


def server_address():
    value = os.environ.get("LM_STUDIO_BASE_URL", "http://127.0.0.1:1234/v1").strip()
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "http"
            and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
            and parsed.path.rstrip("/") in {"", "/v1"}
            and (parsed.port is None or 1 <= parsed.port <= 65535)
        )
    except ValueError:
        valid = False
    if not valid:
        raise SubtitleError(
            "LM_STUDIO_BASE_URL 必须是本机 HTTP 地址，例如 http://127.0.0.1:1234/v1。"
        )
    return parsed.hostname, parsed.port or 80, "/v1"


def request_timeout(value=None):
    try:
        number = float(value if value is not None else os.environ.get("LM_STUDIO_TIMEOUT", "180"))
        if not math.isfinite(number) or not 5 <= number <= 3600:
            raise ValueError("invalid timeout")
        return number
    except (TypeError, ValueError) as exc:
        raise SubtitleError("LM_STUDIO_TIMEOUT / 翻译超时必须为 5–3600 秒。") from exc


def request_json(endpoint: str, payload=None, cancel=None, timeout=None):
    check_cancel(cancel)
    host, port, prefix = server_address()
    connection = HTTPConnection(
        host, port, timeout=request_timeout(timeout) if payload is not None else 8
    )
    headers = {"Content-Type": "application/json"}
    key = os.environ.get("LM_STUDIO_API_KEY", "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        body = (
            json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        )
        path = endpoint if endpoint.startswith("/api/") else prefix + endpoint
        connection.request("POST" if payload is not None else "GET", path, body, headers)
        response = connection.getresponse()
        check_cancel(cancel)
        raw = response.read(2 * 1024**2 + 1)
        check_cancel(cancel)
        if response.status in {401, 403}:
            raise TranslationError(
                "LM Studio 要求认证，请在启动工作台前设置 LM_STUDIO_API_KEY。",
                code="auth",
                status=response.status,
            )
        if response.status != 200:
            text = raw[:8192].decode("utf-8", errors="replace").lower()
            code = "service"
            split = False
            if any(
                term in text
                for term in (
                    "context length",
                    "context window",
                    "context overflow",
                    "too many tokens",
                    "n_ctx",
                )
            ):
                message, code, split = (
                    "输入超过模型已加载的上下文，正在尝试缩小批次；单条失败时请在 LM Studio 增加上下文。",
                    "context",
                    True,
                )
            elif "out of memory" in text or "insufficient memory" in text:
                message, code = "LM Studio 模型内存不足，请降低并发或选择更小模型。", "memory"
            else:
                message = (
                    f"LM Studio 返回 HTTP {response.status}，请检查模型标识、加载状态及接口支持。"
                )
            raise TranslationError(
                message,
                code=code,
                splittable=split,
                retryable=not split
                and response.status in {429, 500, 502, 503, 504}
                and code != "memory",
                status=response.status,
            )
        if len(raw) > 2 * 1024**2:
            raise TranslationError(
                "LM Studio 响应过大，原字幕未修改。", code="format", splittable=True
            )
        return json.loads(raw.decode("utf-8"))
    except SubtitleError:
        raise
    except TimeoutError as exc:
        check_cancel(cancel)
        raise TranslationError(
            "LM Studio 模型响应超时，可能正在加载、排队或思考；可缩小批次、关闭思考，或提高翻译超时 / LM_STUDIO_TIMEOUT。"
            if payload is not None
            else "LM Studio 模型列表响应超时，请检查本机服务是否繁忙。",
            code="timeout",
        ) from exc
    except ConnectionRefusedError as exc:
        check_cancel(cancel)
        raise TranslationError(
            "无法连接 LM Studio：本机服务未启动或端口不正确，请在 Developer 页面启动服务。",
            code="connection",
        ) from exc
    except PermissionError as exc:
        check_cancel(cancel)
        raise TranslationError(
            "系统阻止了本机模型连接，请检查网络权限或安全软件。", code="permission"
        ) from exc
    except (OSError, HTTPException) as exc:
        check_cancel(cancel)
        raise TranslationError(
            "LM Studio 连接中断，请检查本机服务状态后重试。", code="connection", retryable=True
        ) from exc
    except (UnicodeError, ValueError) as exc:
        raise TranslationError(
            "LM Studio 返回了无效 JSON，原字幕未修改。", code="format", splittable=True
        ) from exc
    finally:
        connection.close()


def model_inventory():
    for endpoint, generation in [
        ("/api/v1/models", "native-v1"),
        ("/api/v0/models", "native-v0"),
        ("/models", "compatible"),
    ]:
        try:
            data = request_json(endpoint)
        except TranslationError as exc:
            if exc.status in {404, 405, 501} and generation != "compatible":
                continue
            raise
        try:
            items = data["models" if generation == "native-v1" else "data"]
            if not isinstance(items, list):
                raise ValueError("invalid list")
            results = {}
            for item in items:
                identifier = item["key" if generation == "native-v1" else "id"]
                if not isinstance(identifier, str) or not identifier.strip():
                    continue
                kind = item.get("type", "unknown")
                if kind in {"embedding", "embeddings"}:
                    continue
                instances = item.get("loaded_instances", [])
                config = instances[0].get("config", {}) if instances else {}
                reasoning = item.get("capabilities", {}).get("reasoning", {}) or {}
                results[identifier] = {
                    "id": identifier,
                    "name": item.get("display_name") or identifier,
                    "type": kind,
                    "loaded": bool(instances)
                    if generation == "native-v1"
                    else item.get("state") == "loaded"
                    if generation == "native-v0"
                    else None,
                    "context_length": config.get("context_length"),
                    "max_context_length": item.get("max_context_length"),
                    "parallel": config.get("parallel"),
                    "format": item.get("format", item.get("compatibility_type")),
                    "reasoning_options": reasoning.get("allowed_options", []),
                    "api": generation,
                }
            return list(results.values())
        except (KeyError, TypeError, ValueError, IndexError, AttributeError) as exc:
            raise SubtitleError("LM Studio 模型列表无效，请确认模型接口可用。") from exc
    return []


def models() -> list[str]:
    return [item["id"] for item in model_inventory()]


def validate_capabilities(model, options):
    entry = next((item for item in model_inventory() if item["id"] == model), None)
    if entry is None:
        raise SubtitleError("所选文字模型不在可用列表，请刷新模型并重新选择。")
    if options.concurrency > 1:
        if entry["loaded"] is False:
            raise SubtitleError(
                "并发翻译前请先在 LM Studio 加载所选模型，并设置 Max Concurrent Predictions。"
            )
        if isinstance(entry["parallel"], int) and options.concurrency > entry["parallel"]:
            raise SubtitleError(
                "翻译并发超过模型可用容量，请降低并发或调整 LM Studio 的 Max Concurrent Predictions。"
            )
    if options.reasoning != "auto" and (
        entry["api"] != "native-v1" or options.reasoning not in entry["reasoning_options"]
    ):
        raise SubtitleError(
            "此模型 / 服务未报告所选思考选项，请沿用模型设置，或在 LM Studio 中配置支持的文字模型。"
        )
    return entry


def completion(model, batch, target, schema, cancel=None, options=None) -> str:
    from .translation import TranslationOptions, instructions

    options = options or TranslationOptions()
    prompt = instructions(target) + f" Match this JSON schema: {json.dumps(schema)}"
    if options.reasoning != "auto":
        # The native endpoint documents reasoning controls; its output is validated by the caller.
        result = request_json(
            "/api/v1/chat",
            {
                "model": model,
                "system_prompt": prompt,
                "input": json.dumps(batch, ensure_ascii=False),
                "reasoning": options.reasoning,
                "max_output_tokens": options.max_tokens,
                "temperature": 0,
                "stream": False,
                "store": False,
            },
            cancel,
            options.timeout,
        )
        try:
            texts = [item["content"] for item in result["output"] if item["type"] == "message"]
            if not texts or any(not isinstance(text, str) for text in texts):
                raise ValueError("missing output")
            return "\n".join(texts)
        except (KeyError, TypeError, ValueError) as exc:
            raise TranslationError(
                "本地翻译未完整生成，请缩小批次、提高输出上限或调整思考设置。",
                code="length",
                splittable=True,
            ) from exc
    result = request_json(
        "/chat/completions",
        {
            "model": model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(batch, ensure_ascii=False)},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "subtitle_translation", "strict": True, "schema": schema},
            },
            "temperature": 0,
            "max_tokens": options.max_tokens,
            "stream": False,
        },
        cancel,
        options.timeout,
    )
    try:
        choice = result["choices"][0]
        if choice["finish_reason"] != "stop":
            raise TranslationError(
                "本地翻译未完整生成：输出被截断，请缩小批次、提高输出上限或关闭思考。原字幕未修改。",
                code="length",
                splittable=True,
            )
        content = choice["message"]["content"]
        if not isinstance(content, str):
            raise ValueError("missing content")
        return content
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise TranslationError(
            "LM Studio 翻译响应不完整，原字幕未修改。", code="format", splittable=True
        ) from exc
