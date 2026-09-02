from ..agent.errors import AgentProtocolError
from ..correlation import InvestigationContextMerger
from ..generators import QueryGeneratorRegistry
from ..models import InvestigationContext, ResultStatus, ToolResult, WorkflowOutcome
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
        correlation_max_hops: int = 4,
        correlation_buffer_seconds: int = 120,
        correlation_max_trace_ids: int = 10,
        correlation_max_services: int = 10,
        correlation_max_span_ids: int = 100,
    ):
        self.orchestrator = orchestrator
        self.generators = generators
        self.tools = tools
        self.synthesizer = synthesizer
        self.max_steps = max_steps
        self.context_merger = InvestigationContextMerger(
            max_hops=correlation_max_hops,
            buffer_seconds=correlation_buffer_seconds,
            max_trace_ids=correlation_max_trace_ids,
            max_services=correlation_max_services,
            max_span_ids=correlation_max_span_ids,
        )

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
        context = InvestigationContext()
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
            if result.query:
                context = self.context_merger.register_query(
                    context,
                    result.datasource,
                    result.query,
                )
            if result.context_delta is not None:
                merge_result = self.context_merger.absorb(context, result.context_delta)
                context = merge_result.context
                context = self.context_merger.advance_hop(context)
                logger.info(
                    "correlation.context.updated",
                    extra={
                        "event": "correlation.context.updated",
                        "function_name": "SREWorkflow._run",
                        "step_index": step_index,
                        "datasource": result.datasource.value,
                        "added_pivots": merge_result.added_pivots,
                        "promoted_pivots": merge_result.promoted_pivots,
                        "added_time_windows": merge_result.added_time_windows,
                        "total_pivots": len(context.pivots),
                        "total_time_windows": len(context.time_windows),
                        "visited_query_count": len(context.visited_queries),
                        "hop_count": context.hop_count,
                    },
                )
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
            context=context,
        )
