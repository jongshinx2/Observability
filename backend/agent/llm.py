import json
from typing import Any

from .errors import AgentProtocolError
from ..llm.client import create_completion, response_stats
from ..llm.options import LLMRequestOptions
from ..observability import get_logger, trace_async


logger = get_logger("llm")


@trace_async("llm")
async def request_function_arguments(
    client: Any,
    model: str,
    system_prompt: str,
    user_prompt: str,
    function_name: str,
    function_description: str,
    parameters: dict,
    request_options: LLMRequestOptions | None = None,
) -> dict:
    options = request_options or LLMRequestOptions()
    logger.info(
        "llm.function_call.requested",
        extra={
            "event": "llm.function_call.requested",
            "function_name": function_name,
            "model": model,
            "user_prompt_chars": len(user_prompt),
            "reasoning_mode": options.reasoning_mode,
            "max_tokens": options.max_tokens,
        },
    )
    response = await create_completion(
        client,
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        tools=[{
            "type": "function",
            "function": {
                "name": function_name,
                "description": function_description,
                "parameters": parameters,
            },
        }],
        tool_choice={"type": "function", "function": {"name": function_name}},
        temperature=0,
        **options.as_kwargs(),
    )
    stats = response_stats(response)
    logger.info("llm.response_received", extra={
        "event": "llm.response_received", "function_name": function_name,
        "model": model, **stats,
    })
    if not getattr(response, "choices", None):
        raise AgentProtocolError(f"{function_name} 응답 choices가 비어 있습니다.")
    if stats["finish_reason"] == "length":
        raise AgentProtocolError(f"{function_name} 응답이 토큰 제한으로 중단됐습니다.")
    message = getattr(response.choices[0], "message", None)
    tool_calls = getattr(message, "tool_calls", None)
    if not tool_calls:
        raise AgentProtocolError(f"{function_name} 구조화 출력이 없습니다.")
    if len(tool_calls) != 1:
        raise AgentProtocolError(f"{function_name} 응답에는 함수 호출 하나만 허용됩니다.")
    function = getattr(tool_calls[0], "function", None)
    if getattr(function, "name", None) != function_name:
        raise AgentProtocolError(f"예상하지 않은 함수가 호출됐습니다: {function_name}")
    try:
        arguments = json.loads(function.arguments)
    except (json.JSONDecodeError, TypeError, AttributeError):
        raise AgentProtocolError(f"{function_name} 인자가 유효한 JSON이 아닙니다.") from None
    if not isinstance(arguments, dict):
        raise AgentProtocolError(f"{function_name} 인자는 JSON 객체여야 합니다.")
    logger.info(
        "llm.function_call.received",
        extra={
            "event": "llm.function_call.received",
            "function_name": function_name,
            "model": model,
            "argument_keys": sorted(arguments),
        },
    )
    return arguments
