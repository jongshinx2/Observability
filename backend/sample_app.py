import time
import random
from opentelemetry import trace, metrics
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

# 1. 표준 라벨(Resource) 지정: SRELens 에이전트가 검색할 때 쓸 통일된 이름!
resource = Resource.create({"service.name": "payment-api", "environment": "local-dev"})

# 2. Trace 엔진 설정 -> OTel Collector(4318)로 전송
trace_provider = TracerProvider(resource=resource)
trace_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint="http://localhost:4318/v1/traces")))
trace.set_tracer_provider(trace_provider)
tracer = trace.get_tracer("payment.tracer")

# 3. Metric 엔진 설정 -> OTel Collector(4318)로 전송
metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter(endpoint="http://localhost:4318/v1/metrics"), export_interval_millis=3000)
meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
metrics.set_meter_provider(meter_provider)
meter = metrics.get_meter("payment.meter")

req_counter = meter.create_counter("http_requests_total", description="Total requests")

print("▶ 샘플 트래픽 발생 시작... (Ctrl+C 종료)")

while True:
    # 'process_payment' 라는 작업 단위(Span) 생성
    with tracer.start_as_current_span("process_payment") as span:
        # 20% 확률로 500 에러 발생 시뮬레이션
        is_error = random.random() < 0.2
        status_code = "500" if is_error else "200"
        
        # 메타데이터(라벨) 주입
        span.set_attribute("http.status_code", status_code)
        
        if is_error:
            span.set_status(trace.Status(trace.StatusCode.ERROR, "DB Connection Timeout"))
        
        # 메트릭 증가
        req_counter.add(1, {"http.status_code": status_code})
        
        trace_id = format(span.get_span_context().trace_id, '032x')
        print(f"[Data Pushed] TraceID: {trace_id} | Status: {status_code}")

    time.sleep(2)