import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend import main


class ToolExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_mixed_logql_syntax_is_rejected_before_prometheus_call(self):
        handler = AsyncMock(return_value="should not be called")

        with patch.dict(
            main.TOOL_HANDLERS,
            {"query_prometheus": (main.PrometheusToolArgs, handler)},
        ):
            result = await main.execute_tool(
                "query_prometheus",
                '{"promql":"service_name=\\"payment-api\\" | http_requests_total"}',
            )

        self.assertIn("PromQL에는 LogQL 파이프", json.loads(result)["error"])
        handler.assert_not_awaited()

    async def test_valid_prometheus_selector_is_accepted(self):
        async def fake_prometheus(promql: str) -> str:
            return promql

        with patch.dict(
            main.TOOL_HANDLERS,
            {"query_prometheus": (main.PrometheusToolArgs, fake_prometheus)},
        ):
            result = await main.execute_tool(
                "query_prometheus",
                '{"promql":"http_requests_total{service_name=\\"payment-api\\"}"}',
            )

        self.assertEqual(
            result,
            'http_requests_total{service_name="payment-api"}',
        )

    async def test_tempo_trace_id_is_validated_and_normalized(self):
        async def fake_tempo(trace_id: str) -> str:
            return trace_id

        with patch.dict(
            main.TOOL_HANDLERS,
            {"query_tempo": (main.TempoToolArgs, fake_tempo)},
        ):
            result = await main.execute_tool(
                "query_tempo",
                '{"trace_id":"ABCDEF0123456789ABCDEF0123456789"}',
            )

        self.assertEqual(result, "abcdef0123456789abcdef0123456789")

    async def test_invalid_tool_arguments_return_structured_error(self):
        result = await main.execute_tool(
            "query_loki",
            '{"logql":"{service_name=\\"payment-api\\"}","limit":1001}',
        )

        self.assertIn("도구 인자 검증 실패", json.loads(result)["error"])

    async def test_unknown_tool_returns_structured_error(self):
        result = await main.execute_tool("unknown", "{}")

        self.assertEqual(
            json.loads(result),
            {"error": "등록되지 않은 도구입니다: unknown"},
        )


class ChatEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_invalid_query_returns_chat_response_instead_of_500(self):
        tool_call = SimpleNamespace(
            id="call-invalid",
            function=SimpleNamespace(
                name="query_prometheus",
                arguments=(
                    '{"promql":"service_name=\\"payment-api\\" '
                    '| http_requests_total"}'
                ),
            ),
        )
        invalid_message = SimpleNamespace(tool_calls=[tool_call], content=None)
        create = AsyncMock(side_effect=[
            SimpleNamespace(choices=[SimpleNamespace(message=invalid_message)]),
            SimpleNamespace(choices=[SimpleNamespace(message=invalid_message)]),
        ])
        fake_client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )

        with patch.object(main, "client", fake_client):
            response = await main.chat_endpoint(main.ChatRequest(message="서비스 상태"))

        self.assertEqual(response.iterations, 2)
        self.assertIn("동일한 데이터소스 쿼리 오류", response.reply)
        self.assertIn("PromQL에는 LogQL 파이프", response.reply)

    async def test_direct_answer_preserves_api_contract(self):
        response_message = SimpleNamespace(tool_calls=None, content="정상입니다.")
        create = AsyncMock(return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=response_message)]
        ))
        fake_client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )

        with patch.object(main, "client", fake_client):
            response = await main.chat_endpoint(main.ChatRequest(message="상태 확인"))

        self.assertEqual(response.query, "상태 확인")
        self.assertEqual(response.reply, "정상입니다.")
        self.assertEqual(response.iterations, 1)

    async def test_tool_result_is_returned_to_llm(self):
        tool_call = SimpleNamespace(
            id="call-1",
            function=SimpleNamespace(
                name="query_tempo",
                arguments='{"trace_id":"ABCDEF0123456789ABCDEF0123456789"}',
            ),
        )
        tool_message = SimpleNamespace(tool_calls=[tool_call], content=None)
        final_message = SimpleNamespace(tool_calls=None, content="트레이스 분석 완료")
        create = AsyncMock(side_effect=[
            SimpleNamespace(choices=[SimpleNamespace(message=tool_message)]),
            SimpleNamespace(choices=[SimpleNamespace(message=final_message)]),
        ])
        fake_client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )

        async def fake_tempo(trace_id: str) -> str:
            return json.dumps({"trace_id": trace_id})

        with (
            patch.object(main, "client", fake_client),
            patch.dict(
                main.TOOL_HANDLERS,
                {"query_tempo": (main.TempoToolArgs, fake_tempo)},
            ),
        ):
            response = await main.chat_endpoint(main.ChatRequest(message="트레이스 확인"))

        second_call_messages = create.await_args_list[1].kwargs["messages"]
        self.assertEqual(response.reply, "트레이스 분석 완료")
        self.assertEqual(response.iterations, 2)
        self.assertEqual(second_call_messages[-1]["role"], "tool")
        self.assertIn(
            "abcdef0123456789abcdef0123456789",
            second_call_messages[-1]["content"],
        )


class ConfigurationTests(unittest.TestCase):
    def test_config_paths_are_anchored_to_backend_directory(self):
        self.assertTrue(main.DATASOURCE_CONFIG_PATH.is_absolute())
        self.assertTrue(main.TOOLS_CONFIG_PATH.is_absolute())
        self.assertTrue(main.DATASOURCE_CONFIG_PATH.is_file())
        self.assertTrue(main.TOOLS_CONFIG_PATH.is_file())

    def test_prompt_uses_actual_collector_job(self):
        prompt = main.build_system_prompt()

        self.assertIn('up{job="otel-collector"}', prompt)
        self.assertNotIn('up{job="prometheus"}', prompt)

    def test_prompt_keeps_promql_and_logql_syntax_separate(self):
        prompt = main.build_system_prompt()

        self.assertIn("PromQL에는 LogQL 파이프 연산자 '|'를 절대 사용하지 않는다", prompt)
        self.assertIn('http_requests_total{service_name="payment-api"}', prompt)
        self.assertIn("파이프는 query_loki의 LogQL에서만 사용할 수 있다", prompt)


if __name__ == "__main__":
    unittest.main()
