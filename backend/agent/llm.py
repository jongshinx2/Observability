import json
from typing import Any

from .errors import AgentProtocolError
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
) -> dict:
    logger.info(
        "llm.function_call.requested",
        extra={
            "event": "llm.function_call.requested",
            "function_name": function_name,
            "model": model,
            "user_prompt_chars": len(user_prompt),
        },
    )
    response = await client.chat.completions.create(
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
    )
    message = response.choices[0].message
    if not message.tool_calls:
        raise AgentProtocolError(f"{function_name} 구조화 출력이 없습니다.")
    tool_call = next(
        (call for call in message.tool_calls if call.function.name == function_name),
        None,
    )
    if tool_call is None:
        raise AgentProtocolError(f"예상하지 않은 함수가 호출됐습니다: {function_name}")
    try:
        arguments = json.loads(tool_call.function.arguments)
    except json.JSONDecodeError as exc:
        raise AgentProtocolError(f"{function_name} 인자가 유효한 JSON이 아닙니다.") from exc
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
