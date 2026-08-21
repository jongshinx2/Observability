import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from pydantic import ValidationError

from backend import main
from backend.agent.orchestrator import Orchestrator
from backend.agent.synthesizer import Synthesizer
from backend.agent.workflow import SREWorkflow
from backend.config.settings import DATASOURCE_DIR, PROMPTS_DIR, load_datasource_config
from backend.generators.loki import LokiQueryGenerator
from backend.generators.prometheus import PrometheusQueryGenerator
from backend.models import (
    Datasource,
    GeneratedQuery,
    InvestigationPlan,
    InvestigationStep,
    ResultStatus,
    ToolResult,
    WorkflowOutcome,
)
from backend.tools.loki import LokiQuery
from backend.tools.prometheus import PrometheusQuery, PrometheusTool
from backend.tools.tempo import TempoQuery
from backend.tools.base import describe_exception


def function_call_response(name: str, arguments: dict):
    call = SimpleNamespace(
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )
    message = SimpleNamespace(tool_calls=[call], content=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class ContractTests(unittest.TestCase):
    def test_orchestrator_step_cannot_contain_raw_query_fields(self):
        fields = InvestigationStep.model_fields

        self.assertNotIn("promql", fields)
        self.assertNotIn("logql", fields)
        self.assertNotIn("query", fields)

    def test_plan_limits_number_of_steps(self):
        steps = [
            InvestigationStep(datasource=Datasource.PROMETHEUS, intent=f"step-{index}")
            for index in range(4)
        ]

        with self.assertRaises(ValidationError):
            InvestigationPlan(steps=steps, reason="too many")


class QueryValidationTests(unittest.TestCase):
    def test_mixed_logql_syntax_is_rejected_as_promql(self):
        with self.assertRaises(ValidationError):
            PrometheusQuery(
                promql='service_name="payment-api" | http_requests_total'
            )

    def test_valid_prometheus_selector_is_accepted(self):
        query = PrometheusQuery(
            promql='http_requests_total{service_name="payment-api"}'
        )

        self.assertEqual(
            query.promql,
            'http_requests_total{service_name="payment-api"}',
        )

    def test_logql_must_start_with_stream_selector(self):
        with self.assertRaises(ValidationError):
            LokiQuery(logql="http_requests_total")

    def test_tempo_trace_id_is_normalized(self):
        query = TempoQuery(trace_id="ABCDEF0123456789ABCDEF0123456789")

        self.assertEqual(query.trace_id, "abcdef0123456789abcdef0123456789")


class ErrorDetailTests(unittest.TestCase):
    def test_http_error_keeps_status_and_limited_response_body(self):
        request = httpx.Request(
            "GET",
            "http://localhost:9090/api/v1/query",
        )
        response = httpx.Response(
            400,
            request=request,
            text='{"status":"error","error":"parse error: unexpected pipe"}',
        )
        error = httpx.HTTPStatusError(
            "bad request",
            request=request,
            response=response,
        )

        detail, fields = describe_exception(error)

        self.assertIn("HTTP 400", detail)
        self.assertIn("unexpected pipe", detail)
        self.assertEqual(fields["http_status"], 400)
        self.assertIn("unexpected pipe", fields["response_body"])


class DatasourceErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_prometheus_tool_result_keeps_400_response_detail(self):
        request = httpx.Request(
            "GET",
            "http://localhost:9090/api/v1/query",
        )
        response = httpx.Response(
            400,
            request=request,
            text='{"status":"error","error":"parse error: unexpected pipe"}',
        )

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

            async def get(self, url, params):
                return response

        tool = PrometheusTool(
            "http://localhost:9090/api/v1/query",
            timeout=5,
            max_output_chars=2000,
        )
        with patch(
            "backend.tools.prometheus.httpx.AsyncClient",
            return_value=FakeClient(),
        ):
            result = await tool.execute("http_requests_total")

        self.assertEqual(result.status, ResultStatus.ERROR)
        self.assertIn("HTTP 400", result.error)
        self.assertIn("unexpected pipe", result.error)


class PromptIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_orchestrator_outputs_high_level_plan_only(self):
        create = AsyncMock(return_value=function_call_response(
            "submit_investigation_plan",
            {
                "steps": [{
                    "datasource": "prometheus",
                    "intent": "payment-api 요청 상태 확인",
                    "service": "payment-api",
                    "time_range_minutes": 15,
                }],
                "reason": "메트릭 상태 질문",
            },
        ))
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )
        orchestrator = Orchestrator(client, "test-model")

        plan = await orchestrator.plan("payment-api 요청 상태를 확인해줘")

        self.assertEqual(plan.steps[0].datasource, Datasource.PROMETHEUS)
        self.assertEqual(plan.steps[0].service, "payment-api")
        serialized = plan.model_dump_json()
        self.assertNotIn("promql", serialized)
        self.assertNotIn("logql", serialized)
        self.assertNotIn("http_requests_total", orchestrator.system_prompt)

    async def test_prometheus_generator_sees_only_prometheus_metadata(self):
        create = AsyncMock(return_value=function_call_response(
            "submit_prometheus_query",
            {
                "query": 'http_requests_total{service_name="payment-api"}',
                "purpose": "요청 수 확인",
            },
        ))
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )
        generator = PrometheusQueryGenerator(
            client,
            "test-model",
            load_datasource_config("prometheus"),
        )

        generated = await generator.generate(InvestigationStep(
            datasource=Datasource.PROMETHEUS,
            intent="요청 수 확인",
            service="payment-api",
        ))

        self.assertEqual(generated.datasource, Datasource.PROMETHEUS)
        self.assertNotIn("detected_level", generator.system_prompt)
        self.assertNotIn("LogQL", generator.system_prompt)
        create.assert_not_awaited()

    async def test_loki_generator_sees_only_loki_metadata(self):
        create = AsyncMock(return_value=function_call_response(
            "submit_loki_query",
            {
                "query": '{service_name="payment-api"} | detected_level="error"',
                "purpose": "오류 로그 확인",
                "limit": 10,
            },
        ))
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )
        generator = LokiQueryGenerator(
            client,
            "test-model",
            load_datasource_config("loki"),
        )

        generated = await generator.generate(InvestigationStep(
            datasource=Datasource.LOKI,
            intent="오류 로그 확인",
            service="payment-api",
        ))

        self.assertEqual(generated.datasource, Datasource.LOKI)
        self.assertNotIn("http_requests_total", generator.system_prompt)
        self.assertNotIn("PromQL", generator.system_prompt)
        create.assert_not_awaited()

    async def test_synthesizer_has_no_tool_access(self):
        response_message = SimpleNamespace(tool_calls=None, content="근거 기반 답변")
        create = AsyncMock(return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=response_message)]
        ))
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )
        synthesizer = Synthesizer(client, "test-model")
        plan = InvestigationPlan(
            steps=[InvestigationStep(
                datasource=Datasource.PROMETHEUS,
                intent="상태 확인",
            )],
            reason="메트릭 우선",
        )

        reply = await synthesizer.synthesize("상태 확인", plan, [])

        self.assertEqual(reply, "근거 기반 답변")
        self.assertNotIn("tools", create.await_args.kwargs)
        self.assertNotIn("tool_choice", create.await_args.kwargs)


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_workflow_runs_plan_generate_execute_synthesize(self):
        plan = InvestigationPlan(
            steps=[InvestigationStep(
                datasource=Datasource.PROMETHEUS,
                intent="요청 상태 확인",
                service="payment-api",
            )],
            reason="메트릭 우선",
        )
        generated = GeneratedQuery(
            datasource=Datasource.PROMETHEUS,
            query='http_requests_total{service_name="payment-api"}',
            purpose="요청 상태 확인",
        )
        result = ToolResult(
            datasource=Datasource.PROMETHEUS,
            query=generated.query,
            status=ResultStatus.SUCCESS,
            data=[{"value": 1}],
        )
        orchestrator = SimpleNamespace(plan=AsyncMock(return_value=plan))
        generators = SimpleNamespace(generate=AsyncMock(return_value=generated))
        tools = SimpleNamespace(execute=AsyncMock(return_value=result))
        synthesizer = SimpleNamespace(synthesize=AsyncMock(return_value="정상입니다."))
        workflow = SREWorkflow(
            orchestrator=orchestrator,
            generators=generators,
            tools=tools,
            synthesizer=synthesizer,
            max_steps=3,
        )

        outcome = await workflow.run("상태 확인")

        self.assertEqual(outcome.reply, "정상입니다.")
        self.assertEqual(outcome.results, [result])
        self.assertEqual(outcome.iterations, 3)
        orchestrator.plan.assert_awaited_once()
        generators.generate.assert_awaited_once()
        tools.execute.assert_awaited_once_with(generated)
        synthesizer.synthesize.assert_awaited_once()


class ChatEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_endpoint_preserves_api_contract(self):
        plan = InvestigationPlan(
            steps=[InvestigationStep(
                datasource=Datasource.PROMETHEUS,
                intent="상태 확인",
            )],
            reason="메트릭 조회",
        )
        outcome = WorkflowOutcome(
            reply="정상입니다.",
            iterations=3,
            plan=plan,
            results=[],
        )
        fake_workflow = SimpleNamespace(run=AsyncMock(return_value=outcome))

        with patch.object(main, "workflow", fake_workflow):
            response = await main.chat_endpoint(main.ChatRequest(message="상태 확인"))

        self.assertEqual(
            response.model_dump(),
            {"query": "상태 확인", "reply": "정상입니다.", "iterations": 3},
        )

    async def test_http_response_echoes_request_id_for_log_correlation(self):
        plan = InvestigationPlan(
            steps=[InvestigationStep(
                datasource=Datasource.PROMETHEUS,
                intent="상태 확인",
            )],
            reason="메트릭 조회",
        )
        outcome = WorkflowOutcome(
            reply="정상입니다.",
            iterations=3,
            plan=plan,
            results=[],
        )
        fake_workflow = SimpleNamespace(run=AsyncMock(return_value=outcome))
        transport = httpx.ASGITransport(app=main.app)

        with patch.object(main, "workflow", fake_workflow):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                response = await client.post(
                    "/api/chat",
                    headers={"X-Request-ID": "test-request-123"},
                    json={"message": "상태 확인"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Request-ID"], "test-request-123")


class ConfigurationTests(unittest.TestCase):
    def test_role_files_exist(self):
        for prompt in ("orchestrator", "prometheus", "loki", "tempo", "synthesizer"):
            self.assertTrue((PROMPTS_DIR / f"{prompt}.txt").is_file())
        for datasource in ("prometheus", "loki", "tempo"):
            self.assertTrue((DATASOURCE_DIR / f"{datasource}.yaml").is_file())


if __name__ == "__main__":
    unittest.main()
