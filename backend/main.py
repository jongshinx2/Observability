import json
import os
import re
import yaml
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from openai import AsyncOpenAI, OpenAIError
from pydantic import BaseModel, Field, ValidationError, field_validator

# =====================================================================
# 1. FastAPI 및 안전 임계값 설정
# =====================================================================
BASE_DIR = Path(__file__).resolve().parent
DATASOURCE_CONFIG_PATH = BASE_DIR / "datasource_config.yaml"
TOOLS_CONFIG_PATH = BASE_DIR / "tools_config.yaml"

def get_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as e:
        raise RuntimeError(f"{name}은 정수여야 합니다: {raw_value}") from e
    if value < 1:
        raise RuntimeError(f"{name}은 1 이상이어야 합니다: {value}")
    return value

def get_float_env(name: str, default: float) -> float:
    raw_value = os.getenv(name, str(default))
    try:
        value = float(raw_value)
    except ValueError as e:
        raise RuntimeError(f"{name}은 숫자여야 합니다: {raw_value}") from e
    if value <= 0:
        raise RuntimeError(f"{name}은 0보다 커야 합니다: {value}")
    return value

OLLAMA_BASE_URL = os.getenv("SRELENS_OLLAMA_BASE_URL", "http://localhost:11434/v1")
MODEL_NAME = os.getenv("SRELENS_MODEL_NAME", "qwen3.5:2b")
PROMETHEUS_URL = os.getenv("SRELENS_PROMETHEUS_URL", "http://localhost:9090/api/v1/query")
LOKI_URL = os.getenv("SRELENS_LOKI_URL", "http://localhost:3100/loki/api/v1/query_range")
TEMPO_URL = os.getenv("SRELENS_TEMPO_URL", "http://localhost:3200/api/traces")
MAX_ITERATIONS = get_int_env("SRELENS_MAX_ITERATIONS", 5)
MAX_OUTPUT_CHARS = get_int_env("SRELENS_MAX_OUTPUT_CHARS", 2000)
REQUEST_TIMEOUT = get_float_env("SRELENS_REQUEST_TIMEOUT", 5.0)
CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "SRELENS_CORS_ORIGINS",
        "http://localhost:3000,http://127.0.0.1:3000",
    ).split(",")
    if origin.strip()
]

if not CORS_ORIGINS:
    raise RuntimeError("SRELENS_CORS_ORIGINS에 하나 이상의 origin이 필요합니다.")

app = FastAPI(title="SRELens Agent Local API", version="1.0.0")

# Grafana(기본 3000 포트)에서 통신할 수 있도록 CORS 허용
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

client = AsyncOpenAI(base_url=OLLAMA_BASE_URL, api_key="ollama")

# =====================================================================
# 2. Request / Response 데이터 모델 정의
# =====================================================================
class ChatRequest(BaseModel):
    message: str
    user_context: Optional[str] = ""

class ChatResponse(BaseModel):
    query: str
    reply: str
    iterations: int

class PrometheusToolArgs(BaseModel):
    promql: str = Field(max_length=2000)

    @field_validator("promql")
    @classmethod
    def validate_promql(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("promql은 비어 있을 수 없습니다.")
        if "|" in value:
            raise ValueError(
                "PromQL에는 LogQL 파이프('|')를 사용할 수 없습니다. "
                "메트릭 이름을 먼저 쓰고 라벨을 중괄호 안에 넣으세요. "
                "예: http_requests_total{service_name=\"payment-api\"}"
            )
        if re.match(r"^[a-zA-Z_:][a-zA-Z0-9_:.]*\s*=(?!=)", value):
            raise ValueError(
                "PromQL은 label=값으로 시작할 수 없습니다. "
                "예: service_name=\"payment-api\"가 아니라 "
                "http_requests_total{service_name=\"payment-api\"}를 사용하세요."
            )
        return value

class LokiToolArgs(BaseModel):
    logql: str = Field(max_length=2000)
    limit: int = Field(default=10, ge=1, le=1000)

    @field_validator("logql")
    @classmethod
    def validate_logql(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("logql은 비어 있을 수 없습니다.")
        if not value.startswith("{"):
            raise ValueError(
                "로그 조회용 LogQL은 스트림 selector인 '{...}'로 시작해야 합니다. "
                "예: {service_name=\"payment-api\"} | detected_level=\"error\""
            )
        return value

class TempoToolArgs(BaseModel):
    trace_id: str = Field(pattern=r"^[0-9a-fA-F]{32}$")

    @field_validator("trace_id")
    @classmethod
    def normalize_trace_id(cls, value: str) -> str:
        return value.lower()

# =====================================================================
# 3. Step 4 헬퍼 함수 및 도구 정의
# =====================================================================
def truncate_output(content: str) -> str:
    if len(content) > MAX_OUTPUT_CHARS:
        return f"{content[:MAX_OUTPUT_CHARS]}\n\n[Warning: 데이터가 커서 축약되었습니다.]"
    return content

async def query_prometheus(promql: str) -> str:
    print(f"\n[Debug] LLM이 생성한 PromQL: '{promql}'")  # 생성된 쿼리 출력
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as http_client:
            res = await http_client.get(PROMETHEUS_URL, params={"query": promql})
        res.raise_for_status()
        data = res.json().get("data", {}).get("result", [])
        if not data:
            return json.dumps({"status": "empty", "message": "조회 결과가 없습니다."})
        return truncate_output(json.dumps(data, ensure_ascii=False))
    except httpx.HTTPStatusError as e:
        # 400 에러 발생 시 Prometheus가 반환한 상세 이유 출력
        error_msg = e.response.text
        print(f"[Debug Error] Prometheus 400 상세 내용: {error_msg}")
        return json.dumps({"error": f"Prometheus HTTP 오류 ({e.response.status_code}): {error_msg}"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"Prometheus 조회 실패: {str(e)}"}, ensure_ascii=False)

async def query_loki(logql: str, limit: int = 10) -> str:
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(minutes=60)
    params = {
        "query": logql,
        "limit": limit,
        "start": str(int(start_time.timestamp() * 1e9)),
        "end": str(int(end_time.timestamp() * 1e9))
    }
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as http_client:
            res = await http_client.get(LOKI_URL, params=params)
        res.raise_for_status()
        results = res.json().get("data", {}).get("result", [])
        logs = [val[1] for stream in results for val in stream.get("values", [])]
        if not logs:
            return json.dumps({"status": "empty", "message": "매칭되는 로그가 없습니다."})
        return truncate_output(json.dumps(logs, ensure_ascii=False))
    except Exception as e:
        return json.dumps({"error": f"Loki 조회 실패: {str(e)}"})

async def query_tempo(trace_id: str) -> str:
    """Tempo REST API를 사용해 Trace ID 기반의 스팬 정보를 조회합니다."""
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as http_client:
            res = await http_client.get(f"{TEMPO_URL}/{trace_id}")
        res.raise_for_status()
        data = res.json()
        if not data:
            return json.dumps({"status": "empty", "message": "해당 Trace ID의 트레이스 정보가 없습니다."})
        return truncate_output(json.dumps(data, ensure_ascii=False))
    except Exception as e:
        return json.dumps({"error": f"Tempo 조회 실패: {str(e)}"}, ensure_ascii=False)

def build_system_prompt(user_context: str = "") -> str:
    base_prompt = """
[전역 분석 및 PromQL 작성 규칙]
너는 SRE 엔지니어를 돕는 관측성 데이터 분석 에이전트다.

[도구별 쿼리 문법 분리 규칙]
PromQL과 LogQL 문법을 절대로 섞지 마라.

[query_prometheus / PromQL 전용]
1. 메트릭 이름을 먼저 쓰고 라벨 selector는 바로 뒤의 중괄호 안에 작성한다.
2. 올바름: http_requests_total{service_name="payment-api"}
3. 잘못됨: service_name="payment-api" | http_requests_total
4. PromQL에는 LogQL 파이프 연산자 '|'를 절대 사용하지 않는다.
5. 상태 비교 시 단일 '=' 대신 반드시 '=='를 사용한다.
6. 수집 경로 상태는 up 또는 up{job="otel-collector"}로 조회한다.
7. since(), duration(), % 같은 가짜 문법을 만들지 않는다.

[query_loki / LogQL 전용]
1. 로그 조회는 반드시 {service_name="..."} 형태의 스트림 selector로 시작한다.
2. '|' 파이프는 query_loki의 LogQL에서만 사용할 수 있다.
3. 예: {service_name="payment-api"} | detected_level="error"

[공통 데이터 처리]
데이터가 'empty'로 돌아오면 가짜 데이터를 만들지 말고 정직하게 데이터가 없다고 답변해라.
"""
    return f"{base_prompt}\n[데이터소스 가이드 및 예시]\n{datasource_metadata}\n[사용자 맥락]\n{user_context}"

def load_datasource_metadata() -> str:
    """데이터소스 메타데이터를 애플리케이션 시작 시 검증하고 로드합니다."""
    try:
        with DATASOURCE_CONFIG_PATH.open("r", encoding="utf-8") as file:
            config = yaml.safe_load(file)
    except (OSError, yaml.YAMLError) as e:
        raise RuntimeError(f"datasource 설정 로드 실패: {DATASOURCE_CONFIG_PATH}") from e
    if not isinstance(config, dict):
        raise RuntimeError("datasource_config.yaml의 최상위 값은 객체여야 합니다.")
    return yaml.safe_dump(config, allow_unicode=True)

def load_tools_config() -> list:
    """tools_config.yaml 파일에서 도구 정의 스키마를 불러옵니다."""
    try:
        with TOOLS_CONFIG_PATH.open("r", encoding="utf-8") as file:
            config = yaml.safe_load(file)
    except (OSError, yaml.YAMLError) as e:
        raise RuntimeError(f"tools 설정 로드 실패: {TOOLS_CONFIG_PATH}") from e
    if not isinstance(config, dict) or not isinstance(config.get("tools"), list):
        raise RuntimeError("tools_config.yaml에는 tools 목록이 필요합니다.")
    return config["tools"]

datasource_metadata = load_datasource_metadata()
tools = load_tools_config()

ToolHandler = Callable[..., Awaitable[str]]
TOOL_HANDLERS: dict[str, tuple[type[BaseModel], ToolHandler]] = {
    "query_prometheus": (PrometheusToolArgs, query_prometheus),
    "query_loki": (LokiToolArgs, query_loki),
    "query_tempo": (TempoToolArgs, query_tempo),
}

async def execute_tool(function_name: str, raw_arguments: str) -> str:
    tool_definition = TOOL_HANDLERS.get(function_name)
    if tool_definition is None:
        return json.dumps({"error": f"등록되지 않은 도구입니다: {function_name}"}, ensure_ascii=False)

    argument_model, handler = tool_definition
    try:
        arguments = json.loads(raw_arguments)
        if not isinstance(arguments, dict):
            raise ValueError("도구 인자는 JSON 객체여야 합니다.")
        validated_arguments = argument_model.model_validate(arguments)
    except (json.JSONDecodeError, ValidationError, ValueError) as e:
        return json.dumps({"error": f"도구 인자 검증 실패: {str(e)}"}, ensure_ascii=False)

    return await handler(**validated_arguments.model_dump())

def extract_tool_error(result: str) -> Optional[str]:
    """도구 결과가 구조화된 오류이면 오류 메시지를 반환합니다."""
    try:
        payload = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(payload, dict) and isinstance(payload.get("error"), str):
        return payload["error"]
    return None

# =====================================================================
# 4. API 엔드포인트 정의
# =====================================================================
@app.post("/api/chat", response_model=ChatResponse)
async def chat_endpoint(req: ChatRequest):
    messages = [
        {"role": "system", "content": build_system_prompt(req.user_context)},
        {"role": "user", "content": req.message}
    ]
    
    iteration_count = 0
    failed_tool_calls: dict[str, int] = {}
    last_tool_error: Optional[str] = None
    while iteration_count < MAX_ITERATIONS:
        iteration_count += 1
        try:
            response = await client.chat.completions.create(
                model=MODEL_NAME,
                messages=messages,
                tools=tools,
                tool_choice="auto"
            )
        except OpenAIError as e:
            raise HTTPException(status_code=502, detail=f"로컬 LLM 호출 실패: {str(e)}") from e
        response_msg = response.choices[0].message
        
        # 최종 답변
        if not response_msg.tool_calls:
            return ChatResponse(
                query=req.message,
                reply=response_msg.content or "분석 결과를 생성하지 못했습니다.",
                iterations=iteration_count
            )
            
        messages.append(response_msg)
        for tool_call in response_msg.tool_calls:
            func_name = tool_call.function.name
            result = await execute_tool(func_name, tool_call.function.arguments)

            tool_error = extract_tool_error(result)
            if tool_error:
                last_tool_error = tool_error
                call_signature = f"{func_name}:{tool_call.function.arguments}"
                failed_tool_calls[call_signature] = failed_tool_calls.get(call_signature, 0) + 1
                if failed_tool_calls[call_signature] >= 2:
                    return ChatResponse(
                        query=req.message,
                        reply=(
                            "동일한 데이터소스 쿼리 오류가 반복되어 분석을 중단했습니다. "
                            f"오류: {tool_error}"
                        ),
                        iterations=iteration_count,
                    )

            messages.append({
                "tool_call_id": tool_call.id,
                "role": "tool",
                "name": func_name,
                "content": result
            })

    if last_tool_error:
        reply = f"쿼리 오류를 수정하지 못해 분석을 중단했습니다. 오류: {last_tool_error}"
    else:
        reply = "최대 분석 반복 횟수에 도달해 분석을 중단했습니다. 질문 범위를 좁혀 다시 시도해 주세요."
    return ChatResponse(query=req.message, reply=reply, iterations=iteration_count)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
