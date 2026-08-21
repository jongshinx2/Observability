from ..agent.errors import AgentProtocolError
from ..models import Datasource, GeneratedQuery, InvestigationStep
from ..tools.tempo import TempoQuery
from ..observability import get_logger, trace_async


logger = get_logger("tempo_generator")


class TempoQueryGenerator:
    @trace_async("tempo_generator")
    async def generate(self, step: InvestigationStep) -> GeneratedQuery:
        if not step.trace_id:
            raise AgentProtocolError("Tempo 조회에 필요한 Trace ID가 없습니다.")
        trace_id = TempoQuery(trace_id=step.trace_id).trace_id
        logger.info(
            "query.generated_deterministically",
            extra={
                "event": "query.generated_deterministically",
                "function_name": "TempoQueryGenerator.generate",
                "datasource": Datasource.TEMPO.value,
                "query": trace_id,
            },
        )
        return GeneratedQuery(
            datasource=Datasource.TEMPO,
            query=trace_id,
            purpose=step.intent,
            time_range_minutes=step.time_range_minutes,
        )
