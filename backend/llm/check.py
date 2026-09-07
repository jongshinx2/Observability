"""Explicit, synthetic-only compatibility checks. Run with --live to contact a server."""

import argparse
import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

from openai import OpenAIError

from ..agent.orchestrator import Orchestrator
from ..agent.synthesizer import Synthesizer
from ..config.settings import BACKEND_DIR, Settings, load_settings
from ..generators.prometheus import PrometheusQueryGenerator
from ..models import Datasource, InvestigationPlan, InvestigationStep, ResultStatus, ToolResult
from .client import LLMCallError, create_llm_client, response_stats
from .options import LLMRequestOptions


class _RecordingClient:
    def __init__(self, client: Any):
        self.client = client
        self.responses: list[Any] = []
        self.attempts = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kwargs: Any) -> Any:
        self.attempts += 1
        response = await self.client.chat.completions.create(**kwargs)
        self.responses.append(response)
        return response


def _failure(exc: Exception) -> dict:
    if isinstance(exc, LLMCallError):
        safe = exc
    elif isinstance(exc, (OpenAIError, TimeoutError)):
        safe = LLMCallError(type(exc).__name__, getattr(exc, "status_code", None))
    else:
        # Validation exceptions can include input bodies: expose the type only.
        return {"error_type": type(exc).__name__, "error_category": "invalid_response"}
    return {
        "error_type": safe.error_type,
        "error_category": safe.category,
        "http_status": safe.status_code,
    }


async def run_checks(client: Any, settings: Settings) -> dict:
    checks: list[dict] = []
    report = {
        "base_url": settings.llm_base_url,
        "auth_mode": settings.llm_auth_mode,
        "status": "failed",
        "checks": checks,
        "scope": "synthetic prompts only; no observability queries executed",
        "limitation": "Observed output is not proof that hidden reasoning is disabled on the server.",
    }
    try:
        models = await client.models.list()
        available = {model.id for model in models.data}
        requested = {settings.orchestrator_model, settings.query_model, settings.synthesizer_model}
        missing = sorted(requested - available)
        checks.append({"name": "models", "status": "failed" if missing else "passed", "missing": missing})
        if missing:
            return report
    except Exception as exc:
        checks.append({"name": "models", "status": "failed", **_failure(exc)})
        return report

    async def check(name: str, model: str, options: LLMRequestOptions, operation: Any) -> None:
        recorder = _RecordingClient(client)
        row: dict = {"name": name, "model": model, "reasoning_mode": options.reasoning_mode}
        try:
            await operation(recorder)
            if not recorder.responses:
                raise ValueError("No LLM response; deterministic routes are not compatibility proof")
            row.update(response_stats(recorder.responses[-1]))
            reasoning_visible = (
                row["reasoning_chars"] > 0 or row["has_think_tags"]
                or (isinstance(row["reasoning_tokens"], int) and row["reasoning_tokens"] > 0)
            )
            if options.requests_no_thinking and reasoning_visible:
                row.update(status="failed", error_category="reasoning_disabled_not_observed")
            else:
                row["status"] = "passed"
            row["reasoning_control"] = (
                "not_requested" if options.reasoning_mode == "omit"
                else "disable_requested" if options.requests_no_thinking else "enable_requested"
            )
        except Exception as exc:
            row.update(status="failed", **_failure(exc))
            if recorder.responses:
                row.update(response_stats(recorder.responses[-1]))
        row["llm_calls"] = recorder.attempts
        checks.append(row)

    await check(
        "orchestrator_named_function", settings.orchestrator_model, settings.orchestrator_options,
        lambda recorder: Orchestrator(
            recorder, settings.orchestrator_model, settings.orchestrator_options,
        ).plan("demo-service의 최근 10분 메트릭과 로그를 조사해줘", allow_fallback=False),
    )
    await check(
        "query_named_function", settings.query_model, settings.query_options,
        lambda recorder: PrometheusQueryGenerator(
            recorder, settings.query_model,
            {"metrics": ["demo_latency_seconds"], "labels": ["service_name"]},
            settings.query_options,
        ).generate(InvestigationStep(datasource=Datasource.PROMETHEUS, intent="p95 지연 분포 확인")),
    )
    synthetic_plan = InvestigationPlan(
        reason="synthetic compatibility check",
        steps=[InvestigationStep(datasource=Datasource.PROMETHEUS, intent="합성 메트릭 요약")],
    )
    await check(
        "synthesizer_text", settings.synthesizer_model, settings.synthesizer_options,
        lambda recorder: Synthesizer(
            recorder, settings.synthesizer_model, request_options=settings.synthesizer_options,
        ).synthesize(
            "합성 데이터의 값만 한 문장으로 요약해줘", synthetic_plan,
            [ToolResult(
                datasource=Datasource.PROMETHEUS, query="demo_latency_seconds",
                status=ResultStatus.SUCCESS,
                data=[{"metric": {"service_name": "demo-service"}, "value": [0, "0.1"]}],
            )],
            allow_fallback=False,
        ),
    )
    report["status"] = "passed" if all(row["status"] == "passed" for row in checks) else "failed"
    report["not_exercised"] = ["Loki template-only path", "Tempo deterministic path", "live telemetry", "load/performance"]
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Send synthetic requests to the configured LLM")
    parser.add_argument("--env-file", default=str(BACKEND_DIR / ".env"))
    args = parser.parse_args()
    if not args.live:
        print(json.dumps({"status": "not_run", "reason": "Use --live after configuring the target URL and model."}))
        return 2
    try:
        # No SDK retry or application fallback may hide a failed compatibility request.
        settings = replace(load_settings(args.env_file), llm_max_retries=0)
    except Exception as exc:
        print(json.dumps({"status": "configuration_error", "error_type": type(exc).__name__}))
        return 2

    async def run() -> dict:
        async with create_llm_client(settings) as client:
            return await run_checks(client, settings)

    report = asyncio.run(run())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
