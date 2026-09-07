from typing import Any

from ..config.settings import Settings, load_datasource_config
from ..models import Datasource, GeneratedQuery, InvestigationStep
from .loki import LokiQueryGenerator
from .prometheus import PrometheusQueryGenerator
from .tempo import TempoQueryGenerator
from ..observability import trace_async


class QueryGeneratorRegistry:
    def __init__(self, client: Any, settings: Settings):
        self.generators = {
            Datasource.PROMETHEUS: PrometheusQueryGenerator(
                client,
                settings.query_model,
                load_datasource_config("prometheus"),
                request_options=settings.query_options,
            ),
            Datasource.LOKI: LokiQueryGenerator(
                client,
                settings.query_model,
                load_datasource_config("loki"),
                request_options=settings.query_options,
            ),
            Datasource.TEMPO: TempoQueryGenerator(),
        }

    @trace_async("query_generator_registry")
    async def generate(self, step: InvestigationStep) -> GeneratedQuery:
        return await self.generators[step.datasource].generate(step)
