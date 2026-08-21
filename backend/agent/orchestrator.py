import re
from typing import Any

from pydantic import ValidationError

from ..config.settings import load_prompt
from ..models import Datasource, InvestigationPlan, InvestigationStep
from .errors import AgentProtocolError
from .llm import request_function_arguments
from ..observability import get_logger, trace_async


logger = get_logger("orchestrator")


class Orchestrator:
    def __init__(self, client: Any, model: str):
        self.client = client
        self.model = model
        self.system_prompt = load_prompt("orchestrator")

    @trace_async("orchestrator")
    async def plan(self, question: str, user_context: str = "") -> InvestigationPlan:
        user_prompt = f"질문: {question}\n사용자 맥락: {user_context or '없음'}"
        try:
            arguments = await request_function_arguments(
                client=self.client,
                model=self.model,
                system_prompt=self.system_prompt,
                user_prompt=user_prompt,
                function_name="submit_investigation_plan",
                function_description="쿼리 문자열이 없는 고수준 SRE 조사 계획을 제출합니다.",
                parameters={
                    "type": "object",
                    "properties": {
                        "steps": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 3,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "datasource": {
                                        "type": "string",
                                        "enum": ["prometheus", "loki", "tempo"],
                                    },
                                    "intent": {"type": "string"},
                                    "service": {"type": "string"},
                                    "time_range_minutes": {
                                        "type": "integer",
                                        "minimum": 1,
                                        "maximum": 1440,
                                    },
                                    "filters": {
                                        "type": "object",
                                        "additionalProperties": {"type": "string"},
                                    },
                                    "trace_id": {"type": "string"},
                                },
                                "required": ["datasource", "intent"],
                            },
                        },
                        "reason": {"type": "string"},
                    },
                    "required": ["steps", "reason"],
                },
            )
            plan = InvestigationPlan.model_validate(arguments)
            return self._normalize_plan(question, plan)
        except (AgentProtocolError, ValidationError, ValueError) as exc:
            logger.warning(
                "orchestrator.fallback_applied",
                extra={
                    "event": "orchestrator.fallback_applied",
                    "function_name": "Orchestrator.plan",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
                exc_info=True,
            )
            return self._fallback_plan(question)

    @staticmethod
    def _normalize_plan(question: str, plan: InvestigationPlan) -> InvestigationPlan:
        lowered = question.lower()
        steps = plan.steps

        if "로그" in lowered and not any(
            keyword in lowered for keyword in ("메트릭", "요청량", "상태와 로그")
        ):
            loki_steps = [step for step in steps if step.datasource == Datasource.LOKI]
            if loki_steps:
                steps = loki_steps[:1]

        normalized_steps: list[InvestigationStep] = []
        seen: set[tuple[Datasource, str, str | None]] = set()
        for step in steps:
            if step.datasource == Datasource.TEMPO and not step.trace_id:
                continue
            if not any(keyword in lowered for keyword in ("24시간", "하루", "어제")):
                step.time_range_minutes = min(step.time_range_minutes, 60)
            signature = (step.datasource, step.intent, step.service)
            if signature not in seen:
                normalized_steps.append(step)
                seen.add(signature)

        if not normalized_steps:
            return Orchestrator._fallback_plan(question)
        return InvestigationPlan(steps=normalized_steps[:3], reason=plan.reason)

    @staticmethod
    def _fallback_plan(question: str) -> InvestigationPlan:
        lowered = question.lower()
        trace_match = re.search(r"\b[0-9a-fA-F]{32}\b", question)
        service_match = re.search(
            r"([a-zA-Z0-9][a-zA-Z0-9_-]{1,100})(?:\s*서비스|의)",
            question,
        )
        service = service_match.group(1) if service_match else None

        if trace_match:
            steps = [InvestigationStep(
                datasource=Datasource.TEMPO,
                intent="제공된 Trace ID의 호출 흐름과 오류 확인",
                service=service,
                trace_id=trace_match.group(0),
            )]
        elif "로그" in lowered:
            steps = [InvestigationStep(
                datasource=Datasource.LOKI,
                intent=question,
                service=service,
            )]
        elif any(keyword in lowered for keyword in ("장애", "원인", "에러", "오류")):
            steps = [
                InvestigationStep(
                    datasource=Datasource.PROMETHEUS,
                    intent=f"메트릭으로 상태와 오류 징후 확인: {question}",
                    service=service,
                ),
                InvestigationStep(
                    datasource=Datasource.LOKI,
                    intent=f"관련 오류 로그 확인: {question}",
                    service=service,
                ),
            ]
        else:
            steps = [InvestigationStep(
                datasource=Datasource.PROMETHEUS,
                intent=question,
                service=service,
            )]
        return InvestigationPlan(
            steps=steps,
            reason="구조화 계획 생성 실패로 안전한 기본 라우팅을 적용했습니다.",
        )
