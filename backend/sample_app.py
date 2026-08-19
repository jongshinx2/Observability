import logging
import os
import time
import random
from opentelemetry import trace, metrics
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

OTEL_COLLECTOR_ENDPOINT = os.getenv(
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "http://localhost:4318",
).rstrip("/")

RESOURCE = Resource.create({
    "service.name": "payment-api",
    "deployment.environment.name": "local-dev",
})

def configure_telemetry():
    """트레이스, 메트릭, 로그를 동일한 OTel 리소스로 구성합니다."""
    trace_provider = TracerProvider(resource=RESOURCE)
    trace_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
        endpoint=f"{OTEL_COLLECTOR_ENDPOINT}/v1/traces"
    )))
    trace.set_tracer_provider(trace_provider)
    tracer = trace.get_tracer("payment.tracer")

    metric_reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=f"{OTEL_COLLECTOR_ENDPOINT}/v1/metrics"),
        export_interval_millis=3000,
    )
    meter_provider = MeterProvider(resource=RESOURCE, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)
    meter = metrics.get_meter("payment.meter")
    request_counter = meter.create_counter("http_requests_total", description="Total requests")

    logger_provider = LoggerProvider(resource=RESOURCE)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter(
        endpoint=f"{OTEL_COLLECTOR_ENDPOINT}/v1/logs"
    )))
    set_logger_provider(logger_provider)
    logger = logging.getLogger("payment-api")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(LoggingHandler(level=logging.INFO, logger_provider=logger_provider))

    return trace_provider, meter_provider, logger_provider, tracer, request_counter, logger

def main():
    trace_provider, meter_provider, logger_provider, tracer, request_counter, logger = configure_telemetry()
    print("▶ 샘플 트래픽 발생 시작... (Ctrl+C 종료)")

    try:
        while True:
            with tracer.start_as_current_span("process_payment") as span:
                is_error = random.random() < 0.2
                status_code = "500" if is_error else "200"
                attributes = {"http.status_code": status_code}

                span.set_attribute("http.status_code", status_code)
                request_counter.add(1, attributes)

                if is_error:
                    span.set_status(trace.Status(trace.StatusCode.ERROR, "DB Connection Timeout"))
                    logger.error("DB Connection Timeout", extra=attributes)
                else:
                    logger.info("Payment processed", extra=attributes)

                trace_id = format(span.get_span_context().trace_id, "032x")
                print(f"[Data Pushed] TraceID: {trace_id} | Status: {status_code}")

            time.sleep(2)
    except KeyboardInterrupt:
        print("\n▶ 샘플 트래픽 발생을 종료합니다.")
    finally:
        logger_provider.shutdown()
        meter_provider.shutdown()
        trace_provider.shutdown()

if __name__ == "__main__":
    main()
