from ..config.settings import Settings
from ..models import Datasource, GeneratedQuery, ToolResult
from .loki import LokiTool
from .prometheus import PrometheusTool
from .tempo import TempoTool
from ..observability import trace_async


class ToolRegistry:
    def __init__(self, settings: Settings):
        self.prometheus = PrometheusTool(
            settings.prometheus_url,
            settings.request_timeout,
            settings.max_output_chars,
        )
        self.loki = LokiTool(
            settings.loki_url,
            settings.request_timeout,
            settings.max_output_chars,
        )
        self.tempo = TempoTool(
            settings.tempo_url,
            settings.request_timeout,
            settings.max_output_chars,
        )

    @trace_async("tool_registry")
    async def execute(self, generated: GeneratedQuery) -> ToolResult:
        if generated.datasource == Datasource.PROMETHEUS:
            return await self.prometheus.execute(generated.query)
        if generated.datasource == Datasource.LOKI:
            return await self.loki.execute(
                generated.query,
                generated.time_range_minutes,
                generated.limit,
            )
        return await self.tempo.execute(generated.query)
