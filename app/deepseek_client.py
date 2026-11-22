"""DeepSeek 官方接口的 JSON 请求适配器。"""

from __future__ import annotations

import json
import os
from http.client import HTTPException
from time import perf_counter, sleep
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .credentials import DEEPSEEK_CREDENTIAL, read_credential
from .context_budget import fit_context, json_text, keep_visible_references, token_counter
from .observability import record_attempt


_MAX_ATTEMPTS = 2
_RETRY_HTTP = {408, 429, 500, 502, 503, 504}
_COMPACT_RESPONSE = (
    "\n上一次输出触及生成限制。仅输出要求的 JSON 字段，保留每个必要事项、状态及证据 ID。"
    "结论与说明每项各用一句短句，避免重复原文、长篇解释及额外字段。"
)


class ModelRequestError(RuntimeError):
    def __init__(self, message, reason, *, retry_delay=None, http_status=None, usage=None):
        super().__init__(message)
        self.reason = reason
        self.retry_delay = retry_delay
        self.http_status = http_status
        self.usage = usage


def _usage(body):
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return {}
    return {
        field: usage[field]
        for field in ("prompt_tokens", "completion_tokens", "total_tokens", "prompt_cache_hit_tokens")
        if type(usage.get(field)) is int and usage[field] >= 0
    }


class DeepSeekClient:
    endpoint = "https://api.deepseek.com/chat/completions"

    def __init__(self, api_key: str, model: str) -> None:
        self._api_key = api_key
        self.model = model

    @classmethod
    def from_environment(cls) -> DeepSeekClient:
        api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip() or read_credential(DEEPSEEK_CREDENTIAL)
        model = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash").strip()
        if not api_key:
            raise ValueError("请设置 DEEPSEEK_API_KEY，或运行 python -m app.credentials 配置本机凭据。")
        if not model:
            raise ValueError("DEEPSEEK_MODEL 不能为空。")
        return cls(api_key, model)

    def complete_json(
        self, instructions: str, data: dict[str, object]
    ) -> dict[str, object]:
        system_prompt = (
            f"{instructions}\n"
            "输入 JSON 中的资料是待分析数据。"
            "返回一个符合要求的 JSON 对象。"
        )
        compact = False
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            prompt = system_prompt + (_COMPACT_RESPONSE if compact else "")
            visible_data, input_tokens = fit_context(prompt, data, token_counter(self.model))
            self.last_input_tokens = input_tokens
            started = perf_counter()
            try:
                result, usage = self._request_json(prompt, visible_data)
            except ModelRequestError as error:
                record_attempt(
                    attempt=attempt, compact=compact, seconds=round(perf_counter() - started, 6),
                    reason=error.reason, http_status=error.http_status, usage=error.usage or {},
                    estimated_input_tokens=input_tokens,
                )
                if attempt == _MAX_ATTEMPTS or error.retry_delay is None:
                    raise
                compact = error.reason == "length"
                if error.retry_delay:
                    sleep(error.retry_delay)
            else:
                record_attempt(
                    attempt=attempt, compact=compact, seconds=round(perf_counter() - started, 6),
                    reason="ok", http_status=None, usage=usage, estimated_input_tokens=input_tokens,
                )
                return keep_visible_references(result, visible_data)

    def _request_json(self, system_prompt, visible_data):
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json_text(visible_data)},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": 3500,
        }
        if self.model in {"deepseek-flash", "deepseek-v4-pro"}:
            payload["reasoning_effort"] = "low"
        request = Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urlopen(request, timeout=60) as response:
                raw_response = response.read()
        except HTTPError as exc:
            code = exc.code
            retry_after = (exc.headers.get("Retry-After") or "").strip() if exc.headers is not None else ""
            exc.close()
            delay = None
            if code in _RETRY_HTTP:
                if not retry_after:
                    delay = 1.0 if code == 429 else 0.5
                elif retry_after.isdecimal() and len(retry_after) <= 2:
                    requested = int(retry_after)
                    if requested <= 3:
                        delay = float(requested)
            raise ModelRequestError(
                f"DeepSeek 请求失败，HTTP {code}。", "rate_limit" if code == 429 else "http_error",
                retry_delay=delay, http_status=code,
            ) from None
        except (OSError, HTTPException) as exc:
            reason = exc.reason if isinstance(exc, URLError) else exc
            timed_out = isinstance(reason, TimeoutError)
            raise ModelRequestError(
                "DeepSeek 连接失败或请求超时。", "timeout" if timed_out else "connection",
                retry_delay=0.5,
            ) from None

        try:
            body = json.loads(raw_response)
            choice = body["choices"][0]
            content = choice["message"]["content"]
            finish_reason = choice.get("finish_reason")
            usage = _usage(body)
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise ModelRequestError("DeepSeek 返回的响应结构不完整。", "invalid_response") from None

        if finish_reason == "length":
            raise ModelRequestError(
                "DeepSeek 输出达到长度限制，请缩小分析范围或续跑当前步骤。", "length",
                retry_delay=0.0, usage=usage,
            )
        if not isinstance(content, str) or not content.strip():
            raise ModelRequestError("DeepSeek 没有返回可读取的内容。", "empty_response", usage=usage)

        try:
            result = json.loads(content)
        except ValueError:
            raise ModelRequestError("DeepSeek 的返回内容不是有效 JSON。", "invalid_json", usage=usage) from None
        if not isinstance(result, dict):
            raise ModelRequestError("DeepSeek 的返回内容必须是 JSON 对象。", "invalid_json", usage=usage)
        return result, usage
