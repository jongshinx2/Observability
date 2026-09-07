from typing import TYPE_CHECKING, Any

import httpx
from openai import AsyncOpenAI, OpenAIError

if TYPE_CHECKING:
    from ..config.settings import Settings


class LLMCallError(OpenAIError):
    """Safe error metadata: never include the upstream body or credentials."""

    def __init__(self, error_type: str, status_code: int | None = None):
        self.error_type = error_type
        self.status_code = status_code
        if status_code in {401, 403}:
            self.category = "authentication"
        elif status_code == 404:
            self.category = "endpoint_or_model"
        elif status_code == 429:
            self.category = "rate_limit"
        elif status_code == 400:
            self.category = "request_incompatible"
        elif status_code is not None and status_code >= 500:
            self.category = "upstream_unavailable"
        elif "Timeout" in error_type:
            self.category = "timeout"
        else:
            self.category = "connection_or_protocol"
        super().__init__(f"LLM request failed ({self.category}, {error_type}, HTTP {status_code})")


def create_llm_client(
    settings: "Settings", *, http_client: httpx.AsyncClient | None = None,
) -> AsyncOpenAI:
    timeout = httpx.Timeout(settings.llm_timeout, connect=settings.llm_connect_timeout)
    return AsyncOpenAI(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        timeout=timeout,
        max_retries=settings.llm_max_retries,
        http_client=http_client or httpx.AsyncClient(timeout=timeout, follow_redirects=False),
    )


async def create_completion(client: Any, **kwargs: Any) -> Any:
    try:
        return await client.chat.completions.create(**kwargs)
    except (OpenAIError, TimeoutError) as exc:
        raise LLMCallError(type(exc).__name__, getattr(exc, "status_code", None)) from None


def response_stats(response: Any) -> dict[str, Any]:
    choices = getattr(response, "choices", None) or []
    choice = choices[0] if choices else None
    message = getattr(choice, "message", None)
    content = getattr(message, "content", None) or ""
    reasoning = (
        getattr(message, "reasoning", None)
        or getattr(message, "reasoning_content", None) or ""
    )
    usage = getattr(response, "usage", None)
    details = getattr(usage, "completion_tokens_details", None)
    return {
        "finish_reason": getattr(choice, "finish_reason", None),
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "completion_tokens": getattr(usage, "completion_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
        "reasoning_tokens": getattr(details, "reasoning_tokens", None),
        "content_chars": len(str(content)),
        "reasoning_chars": len(str(reasoning)),
        "has_think_tags": "<think>" in str(content).lower() or "</think>" in str(content).lower(),
    }
