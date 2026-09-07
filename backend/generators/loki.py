from typing import Any

import yaml

from ..agent.llm import request_function_arguments
from ..config.settings import load_prompt
from ..models import Datasource, GeneratedQuery, InvestigationStep
from ..llm.options import LLMRequestOptions
from ..tools.loki import LokiQuery
from ..observability import get_logger, trace_async


logger = get_logger("loki_generator")


class LokiQueryGenerator:
    def __init__(
        self, client: Any, model: str, datasource_config: dict,
        request_options: LLMRequestOptions | None = None,
    ):
        self.client = client
        self.model = model
        self.request_options = request_options or LLMRequestOptions(max_tokens=512)
        self.system_prompt = (
            f"{load_prompt('loki')}\n\n"
            f"[Loki 환경 정보]\n{yaml.safe_dump(datasource_config, allow_unicode=True)}"
        )

    @trace_async("loki_generator")
    async def generate(self, step: InvestigationStep) -> GeneratedQuery:
        template_query = self._template_query(step)
        if template_query:
            logger.info(
                "query.generated_from_template",
                extra={
                    "event": "query.generated_from_template",
                    "function_name": "LokiQueryGenerator.generate",
                    "datasource": Datasource.LOKI.value,
                    "query": template_query,
                },
            )
            return GeneratedQuery(
                datasource=Datasource.LOKI,
                query=template_query,
                purpose=step.intent,
                time_range_minutes=step.time_range_minutes,
                limit=20,
            )

        arguments = await request_function_arguments(
            client=self.client,
            model=self.model,
            request_options=self.request_options,
            system_prompt=self.system_prompt,
            user_prompt=step.model_dump_json(exclude_none=True),
            function_name="submit_loki_query",
            function_description="조사 의도에 맞는 LogQL 하나를 제출합니다.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 2000},
                    "purpose": {"type": "string", "minLength": 1, "maxLength": 500},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                "required": ["query", "purpose"],
            },
        )
        query = LokiQuery(logql=arguments.get("query", "")).logql
        logger.info(
            "query.generated_by_llm",
            extra={
                "event": "query.generated_by_llm",
                "function_name": "LokiQueryGenerator.generate",
                "datasource": Datasource.LOKI.value,
                "query": query,
                "model": self.model,
            },
        )
        return GeneratedQuery(
            datasource=Datasource.LOKI,
            query=query,
            purpose=arguments.get("purpose", step.intent),
            time_range_minutes=step.time_range_minutes,
            limit=arguments.get("limit", 20),
        )

    @staticmethod
    def _template_query(step: InvestigationStep) -> str:
        if step.service:
            service = step.service.replace("\\", "\\\\").replace('"', '\\"')
            query = f'{{service_name="{service}"}}'
        else:
            query = '{service_name=~".+"}'
        intent = step.intent.lower()
        if any(keyword in intent for keyword in ("오류", "에러", "error")):
            query += ' | detected_level="error"'
        return query
