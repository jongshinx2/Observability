from typing import Any

from openai import OpenAIError

from ..config.settings import load_prompt
from ..models import InvestigationPlan, ToolResult
from ..llm.client import create_completion, response_stats
from ..llm.options import LLMRequestOptions
from .errors import AgentProtocolError
from ..observability import get_logger, trace_async
from .fallback_analyzer import build_deterministic_analysis


logger = get_logger("synthesizer")


class Synthesizer:
    def __init__(
        self,
        client: Any,
        model: str,
        reasoning_effort: str = "none",
        max_tokens: int = 512,
        request_options: LLMRequestOptions | None = None,
    ):
        self.client = client
        self.model = model
        self.request_options = request_options or LLMRequestOptions(
            reasoning_mode="effort", reasoning_effort=reasoning_effort, max_tokens=max_tokens,
        )
        self.reasoning_effort = self.request_options.reasoning_effort
        self.max_tokens = self.request_options.max_tokens
        self.system_prompt = load_prompt("synthesizer")

    @trace_async("synthesizer")
    async def synthesize(
        self,
        question: str,
        plan: InvestigationPlan,
        results: list[ToolResult],
        *,
        allow_fallback: bool = True,
    ) -> str:
        evidence = "\n".join(
            result.model_dump_json(exclude={"context_delta"}) for result in results
        )
        logger.info(
            "synthesizer.input_prepared",
            extra={
                "event": "synthesizer.input_prepared",
                "function_name": "Synthesizer.synthesize",
                "step_count": len(plan.steps),
                "result_count": len(results),
                "evidence_chars": len(evidence),
            },
        )
        try:
            response = await create_completion(
                self.client,
                model=self.model,
                messages=[
                    {"role": "system", "content": self.system_prompt},
                    {
                        "role": "user",
                        "content": (
                            f"[사용자 질문]\n{question}\n\n"
                            f"[조사 계획]\n{plan.model_dump_json()}\n\n"
                            f"[관측성 근거]\n{evidence}"
                        ),
                    },
                ],
                temperature=0.2,
                **self.request_options.as_kwargs(),
            )
        except (OpenAIError, TimeoutError) as exc:
            if not allow_fallback:
                raise
            logger.warning(
                "synthesizer.fallback_applied",
                extra={
                    "event": "synthesizer.fallback_applied",
                    "function_name": "Synthesizer.synthesize",
                    "fallback_reason": "llm_request_failed",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "http_status": getattr(exc, "status_code", None),
                    "error_category": getattr(exc, "category", None),
                },
                exc_info=True,
            )
            return build_deterministic_analysis(results)

        choices = getattr(response, "choices", None) or []
        choice = choices[0] if choices else None
        message = getattr(choice, "message", None)
        content = getattr(message, "content", "") if message is not None else ""
        content = content.strip() if isinstance(content, str) else ""
        reasoning = ""
        if message is not None:
            reasoning = (
                getattr(message, "reasoning", None)
                or getattr(message, "reasoning_content", None)
                or ""
            )
        finish_reason = getattr(choice, "finish_reason", None)
        usage = getattr(response, "usage", None)
        logger.info(
            "synthesizer.response_received",
            extra={
                "event": "synthesizer.response_received",
                "function_name": "Synthesizer.synthesize",
                "model": self.model,
                "finish_reason": finish_reason,
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
                "content_chars": len(content),
                "reasoning_chars": len(str(reasoning)),
                "reasoning_mode": self.request_options.reasoning_mode,
                "reasoning_tokens": response_stats(response)["reasoning_tokens"],
            },
        )
        if not content or finish_reason == "length":
            if not allow_fallback:
                raise AgentProtocolError("통합 분석 응답이 비어 있거나 토큰 제한으로 중단됐습니다.")
            fallback_reason = "token_limit" if finish_reason == "length" else "empty_content"
            logger.warning(
                "synthesizer.fallback_applied",
                extra={
                    "event": "synthesizer.fallback_applied",
                    "function_name": "Synthesizer.synthesize",
                    "fallback_reason": fallback_reason,
                    "finish_reason": finish_reason,
                    "content_chars": len(content),
                    "reasoning_chars": len(str(reasoning)),
                },
            )
            return build_deterministic_analysis(results)
        return content
