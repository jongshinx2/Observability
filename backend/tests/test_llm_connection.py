import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import httpx

from backend.agent.errors import AgentProtocolError
from backend.agent.fallback_analyzer import build_deterministic_analysis
from backend.agent.llm import request_function_arguments
from backend.agent.synthesizer import Synthesizer
from backend.config.settings import load_settings
from backend.llm.check import main as check_main, run_checks
from backend.llm.client import LLMCallError, create_llm_client
from backend.llm.options import LLMRequestOptions
from backend.models import Datasource, InvestigationPlan, InvestigationStep


TEST_ENV = {
    "SRELENS_LLM_BASE_URL": "https://llm.example.test/gateway/v1",
    "SRELENS_LLM_API_KEY": "test-secret-key",
    "SRELENS_MODEL_NAME": "served-model",
    "SRELENS_LLM_MAX_RETRIES": "0",
}


def settings(**overrides):
    with patch.dict(os.environ, {**TEST_ENV, **overrides}, clear=True):
        return load_settings(env_file=None)


def completion(function=None, arguments=None, content="synthetic reply", finish=None, **message_fields):
    message = {"role": "assistant", "content": content, **message_fields}
    if function:
        message["content"] = None
        message["tool_calls"] = [{
            "id": "call-test", "type": "function",
            "function": {"name": function, "arguments": json.dumps(arguments)},
        }]
    return {
        "id": "chatcmpl-test", "object": "chat.completion", "created": 0,
        "model": "served-model",
        "choices": [{"index": 0, "message": message, "finish_reason": finish or ("tool_calls" if function else "stop")}],
        "usage": {"prompt_tokens": 30, "completion_tokens": 10, "total_tokens": 40},
    }


async def named_call(client, options=None):
    return await request_function_arguments(
        client, "served-model", "Return a function call.", "synthetic",
        "submit_check", "Submit the check.",
        {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]},
        request_options=options,
    )


def synthetic_plan():
    return InvestigationPlan(
        reason="synthetic", steps=[InvestigationStep(datasource=Datasource.PROMETHEUS, intent="check")],
    )


class ConnectionSettingsTests(unittest.TestCase):
    def test_local_defaults_preserve_legacy_reasoning(self):
        with patch.dict(os.environ, {}, clear=True):
            result = load_settings(env_file=None)
        self.assertEqual(result.llm_base_url, "http://localhost:11434/v1")
        self.assertEqual(result.ollama_base_url, result.llm_base_url)
        self.assertEqual(result.llm_auth_mode, "none")
        self.assertEqual(result.orchestrator_options.reasoning_mode, "omit")
        self.assertEqual(result.query_options.reasoning_mode, "omit")
        self.assertEqual(result.synthesizer_options.as_kwargs(), {"max_tokens": 512, "reasoning_effort": "none"})

    def test_new_url_wins_legacy_url_and_key_is_not_in_repr(self):
        result = settings(SRELENS_OLLAMA_BASE_URL="http://localhost:11434/v1")
        self.assertEqual(result.llm_base_url, TEST_ENV["SRELENS_LLM_BASE_URL"])
        self.assertNotIn(TEST_ENV["SRELENS_LLM_API_KEY"], repr(result))

    def test_dotenv_precedence_literals_and_no_global_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                'SRELENS_LLM_BASE_URL="https://file.example.test/v1"\n'
                'SRELENS_LLM_AUTH_MODE=none\nSRELENS_MODEL_NAME=file-model\n'
                'SRELENS_LLM_API_KEY="literal-${KEEP_ME}"\n', encoding="utf-8",
            )
            with patch.dict(os.environ, {"SRELENS_MODEL_NAME": "process-model"}, clear=True):
                result = load_settings(path)
                self.assertEqual(result.llm_base_url, "https://file.example.test/v1")
                self.assertEqual(result.query_model, "process-model")
                self.assertNotIn("SRELENS_LLM_BASE_URL", os.environ)
            with patch.dict(os.environ, {"SRELENS_LLM_AUTH_MODE": "api_key"}, clear=True):
                self.assertEqual(load_settings(path).llm_api_key, "literal-${KEEP_ME}")
            with patch.dict(os.environ, {"SRELENS_OLLAMA_BASE_URL": "http://localhost:1234/v1"}, clear=True):
                self.assertEqual(load_settings(path).llm_base_url, "http://localhost:1234/v1")
            with patch.dict(os.environ, {}, clear=True):
                path.write_text("SRELENS_MODEL_NAME=changed-model", encoding="utf-8")
                self.assertEqual(load_settings(path).query_model, "changed-model")

    def test_absent_dotenv_does_not_read_example(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory)
            (path / ".env.example").write_text("SRELENS_MODEL_NAME=must-not-load", encoding="utf-8")
            self.assertEqual(load_settings(path / ".env").query_model, "qwen3.5:9b")

    def test_role_override_and_empty_model_inheritance(self):
        result = settings(SRELENS_ORCHESTRATOR_MODEL="planner", SRELENS_QUERY_MODEL="")
        self.assertEqual(result.orchestrator_model, "planner")
        self.assertEqual(result.query_model, "served-model")
        self.assertEqual(result.synthesizer_model, "served-model")

    def test_remote_auth_requires_key_or_explicit_none(self):
        with self.assertRaises(RuntimeError):
            settings(SRELENS_LLM_API_KEY="")
        result = settings(SRELENS_LLM_API_KEY="", SRELENS_LLM_AUTH_MODE="none")
        self.assertEqual(result.llm_auth_mode, "none")

    def test_invalid_connection_settings_fail_early(self):
        for name, value in [
            ("SRELENS_LLM_BASE_URL", "ftp://host/v1"),
            ("SRELENS_LLM_BASE_URL", "https://user:secret@host/v1"),
            ("SRELENS_LLM_BASE_URL", "https://host/v1?key=secret"),
            ("SRELENS_LLM_BASE_URL", "https://host/v1/chat/completions"),
            ("SRELENS_LLM_BASE_URL", "https://host/\nv1"),
            ("SRELENS_LLM_BASE_URL", "https://@host/v1"),
            ("SRELENS_LLM_MAX_RETRIES", "-1"),
            ("SRELENS_LLM_TIMEOUT", "nan"),
            ("SRELENS_LLM_CONNECT_TIMEOUT", "0"),
            ("SRELENS_LLM_AUTH_MODE", "typo"),
            ("SRELENS_LLM_API_KEY", "key\nheader"),
            ("SRELENS_LLM_REASONING_MODE", "auto"),
            ("SRELENS_LLM_ENABLE_THINKING", "no"),
        ]:
            with self.subTest(name=name, value=value), self.assertRaises(RuntimeError):
                settings(**{name: value})

    def test_global_reasoning_policy_and_role_override(self):
        result = settings(
            SRELENS_LLM_REASONING_MODE="chat_template", SRELENS_LLM_ENABLE_THINKING="false",
            SRELENS_QUERY_REASONING_MODE="omit", SRELENS_ORCHESTRATOR_MAX_TOKENS="700",
        )
        self.assertEqual(result.orchestrator_options.max_tokens, 700)
        self.assertEqual(result.synthesizer_options.as_kwargs()["extra_body"], {"chat_template_kwargs": {"enable_thinking": False}})
        self.assertEqual(result.query_options.as_kwargs(), {"max_tokens": 512})


class WireCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_sdk_request_url_auth_named_function_and_reasoning_modes(self):
        for mode in ("omit", "effort", "chat_template"):
            with self.subTest(mode=mode):
                requests = []

                def handler(request):
                    requests.append(request)
                    return httpx.Response(200, json=completion("submit_check", {"value": "ok"}))

                transport = httpx.AsyncClient(transport=httpx.MockTransport(handler))
                async with create_llm_client(settings(), http_client=transport) as client:
                    self.assertEqual(client.max_retries, 0)
                    self.assertEqual(client.timeout.connect, 5)
                    self.assertEqual(client.timeout.read, 120)
                    result = await named_call(client, LLMRequestOptions(max_tokens=128, reasoning_mode=mode))
                self.assertEqual(result, {"value": "ok"})
                self.assertEqual(len(requests), 1)
                self.assertEqual(str(requests[0].url), "https://llm.example.test/gateway/v1/chat/completions")
                self.assertEqual(requests[0].headers["authorization"], "Bearer test-secret-key")
                body = json.loads(requests[0].content)
                self.assertEqual(body["model"], "served-model")
                self.assertEqual(body["tool_choice"], {"type": "function", "function": {"name": "submit_check"}})
                self.assertEqual(body["max_tokens"], 128)
                self.assertNotIn("extra_body", body)
                if mode == "effort":
                    self.assertEqual(body["reasoning_effort"], "none")
                    self.assertNotIn("chat_template_kwargs", body)
                elif mode == "chat_template":
                    self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})
                    self.assertNotIn("reasoning_effort", body)
                else:
                    self.assertNotIn("reasoning_effort", body)
                    self.assertNotIn("chat_template_kwargs", body)

    async def test_http_failures_do_not_retry_with_changed_options_or_leak_body(self):
        for status, category in [(400, "request_incompatible"), (401, "authentication"), (404, "endpoint_or_model"), (429, "rate_limit"), (503, "upstream_unavailable")]:
            calls = []

            def handler(request):
                calls.append(request)
                return httpx.Response(status, json={"error": {"message": "test-secret-key private response"}})

            with self.subTest(status=status):
                transport = httpx.AsyncClient(transport=httpx.MockTransport(handler))
                async with create_llm_client(settings(), http_client=transport) as client:
                    with self.assertRaises(LLMCallError) as raised:
                        await named_call(client)
                self.assertEqual(len(calls), 1)
                self.assertEqual(raised.exception.category, category)
                self.assertEqual(raised.exception.status_code, status)
                self.assertNotIn("test-secret-key", str(raised.exception))
                self.assertNotIn("private response", str(raised.exception))

    async def test_timeout_is_classified(self):
        def handler(request):
            raise httpx.ReadTimeout("private body", request=request)

        transport = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        async with create_llm_client(settings(), http_client=transport) as client:
            with self.assertRaises(LLMCallError) as raised:
                await named_call(client)
        self.assertEqual(raised.exception.category, "timeout")

    async def test_malformed_function_responses_are_rejected(self):
        good = completion("submit_check", {"value": "ok"})
        empty = {**good, "choices": []}
        invalid_json = copy.deepcopy(good)
        invalid_json["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "not json"
        duplicate = copy.deepcopy(good)
        duplicate["choices"][0]["message"]["tool_calls"] *= 2
        cases = [empty, completion(), completion("wrong_name", {}), invalid_json, duplicate,
                 completion("submit_check", []), completion("submit_check", {}, finish="length")]
        for index, payload in enumerate(cases):
            with self.subTest(index=index):
                transport = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))
                async with create_llm_client(settings(), http_client=transport) as client:
                    with self.assertRaises(AgentProtocolError):
                        await named_call(client)

    async def test_synthesizer_strict_validation_does_not_count_fallback_as_success(self):
        for payload in (completion(content=""), completion(finish="length"), {"id": "missing-choices"}):
            transport = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))
            async with create_llm_client(settings(), http_client=transport) as client:
                synth = Synthesizer(client, "served-model")
                with self.assertRaises(AgentProtocolError):
                    await synth.synthesize("synthetic", synthetic_plan(), [], allow_fallback=False)
                self.assertEqual(await synth.synthesize("synthetic", synthetic_plan(), []), build_deterministic_analysis([]))

    async def test_synthesizer_auth_failure_keeps_fallback_but_strict_probe_fails(self):
        transport = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(401, json={"error": {"message": "test-secret-key"}})
        ))
        async with create_llm_client(settings(), http_client=transport) as client:
            synth = Synthesizer(client, "served-model")
            with self.assertRaises(LLMCallError):
                await synth.synthesize("synthetic", synthetic_plan(), [], allow_fallback=False)
            with self.assertLogs("srelens.synthesizer", level="WARNING") as captured:
                self.assertEqual(await synth.synthesize("synthetic", synthetic_plan(), []), build_deterministic_analysis([]))
            self.assertNotIn("test-secret-key", "\n".join(captured.output))


class LiveCheckHarnessTests(unittest.IsolatedAsyncioTestCase):
    def handler(self, request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"object": "list", "data": [{"id": "served-model", "object": "model", "created": 0, "owned_by": "test"}]})
        body = json.loads(request.content)
        self.bodies.append(body)
        function = body.get("tool_choice", {}).get("function", {}).get("name")
        if function == "submit_investigation_plan":
            payload = completion(function, {
                "steps": [{"datasource": "prometheus", "intent": "synthetic metrics"}],
                "reason": "synthetic",
            })
        elif function == "submit_prometheus_query":
            payload = completion(function, {"query": "demo_latency_seconds", "purpose": "synthetic latency"})
        else:
            payload = completion()
        if self.mutation:
            payload = self.mutation(payload)
        return httpx.Response(200, json=payload)

    async def run_harness(self, mutation=None, mode="effort"):
        self.bodies = []
        self.mutation = mutation
        configured = settings(SRELENS_LLM_REASONING_MODE=mode)
        transport = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        async with create_llm_client(configured, http_client=transport) as client:
            return await run_checks(client, configured)

    async def test_probe_exercises_all_llm_roles_and_their_options(self):
        report = await self.run_harness()
        self.assertEqual(report["status"], "passed")
        self.assertEqual(len(report["checks"]), 4)
        self.assertEqual(len(self.bodies), 3)
        for body in self.bodies:
            self.assertEqual(body["reasoning_effort"], "none")
            self.assertIn("max_tokens", body)
        self.assertNotIn("test-secret-key", json.dumps(report))
        self.assertIn("Loki template-only path", report["not_exercised"])

    async def test_probe_detects_ignored_reasoning_disable(self):
        def mutate(payload):
            payload["choices"][0]["message"]["reasoning"] = "still thinking"
            return payload
        report = await self.run_harness(mutate)
        self.assertEqual(report["status"], "failed")
        self.assertTrue(all(row["error_category"] == "reasoning_disabled_not_observed" for row in report["checks"][1:]))

    async def test_probe_serializes_omit_and_template_modes_for_all_roles(self):
        for mode in ("omit", "chat_template"):
            report = await self.run_harness(mode=mode)
            self.assertEqual(report["status"], "passed")
            for body in self.bodies:
                self.assertNotIn("reasoning_effort", body)
                if mode == "omit":
                    self.assertNotIn("chat_template_kwargs", body)
                else:
                    self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})

    async def test_probe_detects_reasoning_mixed_into_content(self):
        def mutate(payload):
            if not payload["choices"][0]["message"].get("tool_calls"):
                payload["choices"][0]["message"]["content"] = "<think>reasoning</think>answer"
            return payload
        report = await self.run_harness(mutate)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["checks"][-1]["error_category"], "reasoning_disabled_not_observed")

    async def test_probe_detects_hidden_reasoning_token_usage(self):
        def mutate(payload):
            payload["usage"]["completion_tokens_details"] = {"reasoning_tokens": 5}
            return payload
        report = await self.run_harness(mutate)
        self.assertEqual(report["status"], "failed")

    async def test_probe_rejects_broken_responses_instead_of_fallback_success(self):
        report = await self.run_harness(lambda payload: {**payload, "choices": []})
        self.assertEqual(report["status"], "failed")
        self.assertTrue(all(row["status"] == "failed" for row in report["checks"][1:]))

    async def test_missing_model_stops_before_any_chat(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"object": "list", "data": []})
        transport = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        async with create_llm_client(settings(), http_client=transport) as client:
            report = await run_checks(client, settings())
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["checks"][0]["missing"], ["served-model"])
        self.assertEqual(len(calls), 1)

    async def test_unauthorized_model_listing_stops_without_exposing_response(self):
        transport = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(401, json={"error": {"message": "test-secret-key"}})
        ))
        async with create_llm_client(settings(), http_client=transport) as client:
            report = await run_checks(client, settings())
        self.assertEqual(report["checks"][0]["error_category"], "authentication")
        self.assertNotIn("test-secret-key", json.dumps(report))

    def test_cli_does_not_contact_a_server_without_live_flag(self):
        output = io.StringIO()
        with patch("sys.argv", ["check"]), patch("backend.llm.check.create_llm_client") as factory, redirect_stdout(output):
            self.assertEqual(check_main(), 2)
        factory.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["status"], "not_run")
