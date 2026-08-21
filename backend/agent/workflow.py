from ..agent.errors import AgentProtocolError
from ..generators import QueryGeneratorRegistry
from ..models import ResultStatus, ToolResult, WorkflowOutcome
from ..tools import ToolRegistry
from .orchestrator import Orchestrator
from .synthesizer import Synthesizer
from ..observability import (
    bind_request_id,
    get_logger,
    get_request_id,
    new_request_id,
    reset_request_id,
    trace_async,
)


logger = get_logger("workflow")


class SREWorkflow:
    def __init__(
        self,
        orchestrator: Orchestrator,
        generators: QueryGeneratorRegistry,
        tools: ToolRegistry,
        synthesizer: Synthesizer,
        max_steps: int,
    ):
        self.orchestrator = orchestrator
        self.generators = generators
        self.tools = tools
        self.synthesizer = synthesizer
        self.max_steps = max_steps

    @trace_async("workflow")
    async def run(self, question: str, user_context: str = "") -> WorkflowOutcome:
        request_token = None
        if get_request_id() == "-":
            request_token = bind_request_id(new_request_id())
        try:
            return await self._run(question, user_context)
        finally:
            if request_token is not None:
                reset_request_id(request_token)

    async def _run(self, question: str, user_context: str) -> WorkflowOutcome:
        plan = await self.orchestrator.plan(question, user_context)
        logger.info(
            "workflow.plan_ready",
            extra={
                "event": "workflow.plan_ready",
                "function_name": "SREWorkflow._run",
                "step_count": len(plan.steps),
                "datasources": [step.datasource.value for step in plan.steps],
                "reason": plan.reason,
            },
        )
        results: list[ToolResult] = []
        iterations = 1

        for step_index, step in enumerate(plan.steps[:self.max_steps], start=1):
            logger.info(
                "workflow.step.started",
                extra={
                    "event": "workflow.step.started",
                    "function_name": "SREWorkflow._run",
                    "step_index": step_index,
                    "datasource": step.datasource.value,
                    "intent": step.intent,
                },
            )
            try:
                generated = await self.generators.generate(step)
                iterations += 1
                result = await self.tools.execute(generated)
            except (AgentProtocolError, ValueError) as exc:
                logger.exception(
                    "workflow.step.generation_failed",
                    extra={
                        "event": "workflow.step.generation_failed",
                        "function_name": "SREWorkflow._run",
                        "step_index": step_index,
                        "datasource": step.datasource.value,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    },
                )
                result = ToolResult(
                    datasource=step.datasource,
                    query="",
                    status=ResultStatus.ERROR,
                    error=f"쿼리 생성 실패: {exc}",
                )
            results.append(result)
            logger.info(
                "workflow.step.completed",
                extra={
                    "event": "workflow.step.completed",
                    "function_name": "SREWorkflow._run",
                    "step_index": step_index,
                    "datasource": step.datasource.value,
                    "query": result.query,
                    "status": result.status.value,
                    "error": result.error,
                    "truncated": result.truncated,
                },
            )

        reply = await self.synthesizer.synthesize(question, plan, results)
        iterations += 1
        return WorkflowOutcome(
            reply=reply,
            iterations=iterations,
            plan=plan,
            results=results,
        )
