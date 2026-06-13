from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Dict, Sequence

import httpx

try:
    from overstats.config import config as app_config
    from overstats.src.modules.errors import ModuleError
except ModuleNotFoundError:
    from config import config as app_config
    from src.modules.errors import ModuleError

from ..analysis_common import build_async_client as build_analysis_async_client
from ..analysis_common import get_analysis_proxy


AUTO_ROUTE_TIMEOUT_SECONDS = 60.0


def _sanitize_api_key(value: Any) -> str:
    text = str(value or "").strip()
    if not text or "replace-with-your" in text.lower():
        return ""
    return text


def _analysis_model_for_base_url(base_url: str) -> str:
    normalized = str(base_url or "").strip().lower()
    if "generativelanguage.googleapis.com" in normalized or "googleapis.com" in normalized:
        return str(
            getattr(app_config, "ANALYSIS_GOOGLE_MODEL", "gemini-3.1-flash-lite-preview")
            or "gemini-3.1-flash-lite-preview"
        )
    if "deepseek" in normalized:
        return str(getattr(app_config, "ANALYSIS_DEEPSEEK_MODEL", "deepseek-chat") or "deepseek-chat")
    return str(getattr(app_config, "ANALYSIS_OPENAI_MODEL", "gpt-4o-mini") or "gpt-4o-mini")


def _chat_completion_url(base_url: str) -> str:
    base = str(base_url or "").rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1") or base.endswith("/openai"):
        return f"{base}/chat/completions"
    return f"{base}/chat/completions"


def _parse_tool_arguments(arguments: Any) -> Dict[str, Any]:
    if isinstance(arguments, dict):
        return dict(arguments)
    raw_text = str(arguments or "").strip()
    if not raw_text:
        return {}
    try:
        payload = json.loads(raw_text)
    except Exception as exc:
        raise ModuleError(
            error="auto_route_invalid_arguments",
            message="LLM tool arguments are not valid JSON.",
            status_code=502,
            details={"arguments": raw_text},
        ) from exc
    if not isinstance(payload, dict):
        raise ModuleError(
            error="auto_route_invalid_arguments",
            message="LLM tool arguments must be a JSON object.",
            status_code=502,
            details={"arguments": payload},
        )
    return payload


@dataclass(frozen=True)
class AutoRouteToolCall:
    name: str
    arguments: Dict[str, Any]


def extract_tool_call(payload: Any) -> AutoRouteToolCall:
    if not isinstance(payload, dict):
        raise ModuleError(
            error="auto_route_no_tool_call",
            message="LLM response did not contain a tool call.",
            status_code=502,
        )

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ModuleError(
            error="auto_route_no_tool_call",
            message="LLM response did not contain a tool call.",
            status_code=502,
            details={"payload_keys": list(payload.keys())[:10]},
        )

    first_choice = choices[0] or {}
    message = first_choice.get("message") if isinstance(first_choice, dict) else None
    if not isinstance(message, dict):
        raise ModuleError(
            error="auto_route_no_tool_call",
            message="LLM response did not contain a tool call.",
            status_code=502,
        )

    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        function = tool_calls[0].get("function") if isinstance(tool_calls[0], dict) else None
        if isinstance(function, dict):
            name = str(function.get("name") or "").strip()
            if name:
                return AutoRouteToolCall(name=name, arguments=_parse_tool_arguments(function.get("arguments")))

    function_call = message.get("function_call")
    if isinstance(function_call, dict):
        name = str(function_call.get("name") or "").strip()
        if name:
            return AutoRouteToolCall(name=name, arguments=_parse_tool_arguments(function_call.get("arguments")))

    raise ModuleError(
        error="auto_route_no_tool_call",
        message="LLM response did not contain a tool call.",
        status_code=502,
    )


class AutoRouteRequests:
    def __init__(self, *, timeout_seconds: float = AUTO_ROUTE_TIMEOUT_SECONDS) -> None:
        self.timeout_seconds = float(timeout_seconds)

    async def select_tool_call(
        self,
        *,
        user_text: str,
        system_prompt: str,
        tools: Sequence[Dict[str, Any]],
        supported_commands: Sequence[str],
    ) -> AutoRouteToolCall:
        base_url = str(getattr(app_config, "ANALYSIS_BASE_URL", "") or "").strip()
        api_key = _sanitize_api_key(getattr(app_config, "ANALYSIS_API_KEY", ""))
        if not base_url or not api_key:
            raise ModuleError(
                error="auto_route_not_configured",
                message="Auto route requires ANALYSIS_BASE_URL and ANALYSIS_API_KEY.",
                status_code=503,
            )

        payload = {
            "model": _analysis_model_for_base_url(base_url),
            "temperature": 0.0,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "user_text": str(user_text or ""),
                            "supported_commands": list(supported_commands),
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "tools": list(tools),
            "tool_choice": "auto",
        }
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        proxy_url = get_analysis_proxy(base_url)
        async with build_analysis_async_client(timeout=self.timeout_seconds, proxy_url=proxy_url) as client:
            response = await client.post(_chat_completion_url(base_url), json=payload, headers=headers)
            response.raise_for_status()
            response_payload = response.json()

        return extract_tool_call(response_payload)
