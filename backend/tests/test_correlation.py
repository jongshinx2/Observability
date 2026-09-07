import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from pydantic import ValidationError

from backend.agent.workflow import SREWorkflow
from backend.correlation.context import InvestigationContextMerger
from backend.correlation.extractors.tempo import TempoCorrelationExtractor
from backend.models import (
    ContextDelta,
    CorrelationPivot,
    Datasource,
    EvidenceScope,
    GeneratedQuery,
    InvestigationContext,
    InvestigationPlan,
    InvestigationStep,
    PivotKind,
    ResultStatus,
    TimeWindow,
    ToolResult,
)
from backend.tools.tempo import TempoTool


TRACE_ID = "3fb3148d11506e89b3f6631f3ae8155e"
SPAN_ID = "0123456789abcdef"


def tempo_payload() -> dict:
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "payment-api"}},
                        {
                            "key": "service.instance.id",
                            "value": {"stringValue": "payment-api-1"},
                        },
                        {
                            "key": "deployment.environment.name",
                            "value": {"stringValue": "local-dev"},
                        },
                    ]
                },
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": TRACE_ID,
                                "spanId": SPAN_ID,
                                "name": "process_payment",
                                "startTimeUnixNano": "1000000000",
                                "endTimeUnixNano": "1250000000",
                                "attributes": [
                                    {
                                        "key": "http.response.status_code",
                                        "value": {"intValue": "200"},
                                    }
                                ],
                            }
                        ]
                    }
                ],
            }
        ]
    }


def pivot(
    kind: PivotKind,
    value: str,
    *,
    source: Datasource = Datasource.TEMPO,
    scope: EvidenceScope = EvidenceScope.DIRECT,
) -> CorrelationPivot:
    return CorrelationPivot(
        kind=kind,
        value=value,
        source=source,
        scope=scope,
        source_query="test-query",
    )


class CorrelationContractTests(unittest.TestCase):
    def test_malformed_trace_and_span_ids_are_rejected(self):
        with self.assertRaises(ValidationError):
            pivot(PivotKind.TRACE_ID, "not-a-trace-id")
        with self.assertRaises(ValidationError):
            pivot(PivotKind.SPAN_ID, "short")

    def test_merger_deduplicates_and_promotes_direct_evidence(self):
        merger = InvestigationContextMerger()
        temporal = pivot(
            PivotKind.SERVICE_NAME,
            "payment-api",
            source=Datasource.LOKI,
            scope=EvidenceScope.TEMPORAL,
        )
        direct = pivot(PivotKind.SERVICE_NAME, "payment-api")

        first = merger.absorb(InvestigationContext(), ContextDelta(pivots=[temporal]))
        second = merger.absorb(first.context, ContextDelta(pivots=[direct, direct]))

        self.assertEqual(len(second.context.pivots), 1)
        self.assertEqual(second.context.pivots[0].scope, EvidenceScope.DIRECT)
        self.assertEqual(second.context.pivots[0].source, Datasource.TEMPO)
        self.assertEqual(second.promoted_pivots, 1)

    def test_merger_applies_limits_and_prefers_direct_time_window(self):
        merger = InvestigationContextMerger(
            max_hops=2,
            buffer_seconds=2,
            max_trace_ids=1,
            max_services=1,
            max_span_ids=1,
        )
        temporal_window = TimeWindow(
            start_unix_ns=1_000_000_000,
            end_unix_ns=10_000_000_000,
            source=Datasource.LOKI,
            scope=EvidenceScope.TEMPORAL,
        )
        direct_window = TimeWindow(
            start_unix_ns=4_000_000_000,
            end_unix_ns=5_000_000_000,
            source=Datasource.TEMPO,
            scope=EvidenceScope.DIRECT,
        )
        delta = ContextDelta(
            pivots=[
                pivot(PivotKind.TRACE_ID, TRACE_ID),
                pivot(PivotKind.TRACE_ID, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
                pivot(PivotKind.SERVICE_NAME, "payment-api"),
                pivot(PivotKind.SERVICE_NAME, "checkout-api"),
                pivot(PivotKind.SPAN_ID, SPAN_ID),
                pivot(PivotKind.SPAN_ID, "fedcba9876543210"),
            ],
            time_windows=[temporal_window, direct_window],
        )

        context = merger.absorb(InvestigationContext(), delta).context
        self.assertEqual(context.values(PivotKind.TRACE_ID), [TRACE_ID])
        self.assertEqual(context.values(PivotKind.SERVICE_NAME), ["payment-api"])
        self.assertEqual(context.values(PivotKind.SPAN_ID), [SPAN_ID])
        self.assertEqual(merger.primary_time_window(context), direct_window)
        buffered = merger.buffered_time_window(context)
        self.assertIsNotNone(buffered)
        self.assertEqual(buffered.start_unix_ns, 2_000_000_000)
        self.assertEqual(buffered.end_unix_ns, 7_000_000_000)

        context = merger.advance_hop(context)
        context = merger.advance_hop(context)
        context = merger.advance_hop(context)
        self.assertEqual(context.hop_count, 2)
        self.assertFalse(merger.can_advance(context))


class TempoCorrelationExtractorTests(unittest.TestCase):
    def test_extracts_pivots_and_direct_time_window(self):
        delta = TempoCorrelationExtractor().extract(
            tempo_payload(),
            source_query=TRACE_ID.upper(),
        )

        values = {(item.kind, item.value) for item in delta.pivots}
        self.assertIn((PivotKind.TRACE_ID, TRACE_ID), values)
        self.assertIn((PivotKind.SPAN_ID, SPAN_ID), values)
        self.assertIn((PivotKind.SERVICE_NAME, "payment-api"), values)
        self.assertIn((PivotKind.INSTANCE_ID, "payment-api-1"), values)
        self.assertIn((PivotKind.ENVIRONMENT, "local-dev"), values)
        self.assertIn((PivotKind.HTTP_STATUS_CODE, "200"), values)
        self.assertEqual(len(delta.time_windows), 1)
        self.assertEqual(delta.time_windows[0].start_unix_ns, 1_000_000_000)
        self.assertEqual(delta.time_windows[0].end_unix_ns, 1_250_000_000)


class TempoToolCorrelationTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_is_extracted_before_large_result_is_compacted(self):
        response = httpx.Response(
            200,
            json=tempo_payload(),
            request=httpx.Request("GET", f"http://tempo/api/traces/{TRACE_ID}"),
        )
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        client.get = AsyncMock(return_value=response)

        with patch("backend.tools.tempo.httpx.AsyncClient", return_value=client):
            result = await TempoTool(
                "http://tempo/api/traces",
                timeout=1,
                max_output_chars=20,
            ).execute(TRACE_ID)

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertTrue(result.truncated)
        self.assertIsNotNone(result.context_delta)
        self.assertIn(TRACE_ID, [item.value for item in result.context_delta.pivots])
        self.assertEqual(len(result.context_delta.time_windows), 1)


class WorkflowContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_is_request_local(self):
        plan = InvestigationPlan(
            reason="trace lookup",
            steps=[
                InvestigationStep(
                    datasource=Datasource.TEMPO,
                    intent="trace lookup",
                    trace_id=TRACE_ID,
                )
            ],
        )
        generated = GeneratedQuery(
            datasource=Datasource.TEMPO,
            query=TRACE_ID,
            purpose="trace lookup",
        )
        first_result = ToolResult(
            datasource=Datasource.TEMPO,
            query=TRACE_ID,
            status=ResultStatus.SUCCESS,
            data={},
            context_delta=ContextDelta(pivots=[pivot(PivotKind.TRACE_ID, TRACE_ID)]),
        )
        second_result = ToolResult(
            datasource=Datasource.TEMPO,
            query=TRACE_ID,
            status=ResultStatus.EMPTY,
        )
        orchestrator = MagicMock(plan=AsyncMock(return_value=plan))
        generators = MagicMock(generate=AsyncMock(return_value=generated))
        tools = MagicMock(execute=AsyncMock(side_effect=[first_result, second_result]))
        synthesizer = MagicMock(synthesize=AsyncMock(return_value="analysis"))
        workflow = SREWorkflow(
            orchestrator=orchestrator,
            generators=generators,
            tools=tools,
            synthesizer=synthesizer,
            max_steps=1,
        )

        first = await workflow.run("first")
        second = await workflow.run("second")

        self.assertEqual(first.context.values(PivotKind.TRACE_ID), [TRACE_ID])
        self.assertEqual(first.context.hop_count, 1)
        self.assertEqual(second.context.pivots, [])
        self.assertEqual(second.context.hop_count, 0)
        self.assertEqual(second.context.visited_queries, {f"tempo:{TRACE_ID}"})
