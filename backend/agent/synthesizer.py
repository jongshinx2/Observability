from typing import Any

from ..config.settings import load_prompt
from ..models import InvestigationPlan, ToolResult
from ..observability import get_logger, trace_async


logger = get_logger("synthesizer")


class Synthesizer:
    def __init__(self, client: Any, model: str):
        self.client = client
        self.model = model
        self.system_prompt = load_prompt("synthesizer")

    @trace_async("synthesizer")
    async def synthesize(
        self,
        question: str,
        plan: InvestigationPlan,
        results: list[ToolResult],
    ) -> str:
        evidence = "\n".join(result.model_dump_json() for result in results)
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
        response = await self.client.chat.completions.create(
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
        )
        return response.choices[0].message.content or "분석 결과를 생성하지 못했습니다."
