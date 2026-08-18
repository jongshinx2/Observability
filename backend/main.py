import json
import requests
import yaml
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from openai import OpenAI

# =====================================================================
# 1. FastAPI 및 안전 임계값 설정
# =====================================================================
app = FastAPI(title="SRELens Agent Local API", version="1.0.0")

# Grafana(기본 3000 포트)에서 통신할 수 있도록 CORS 허용
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 실운영 시 Grafana 주소로 제한 (예: ["http://localhost:3000"])
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

client = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")
MODEL_NAME = "qwen3.5:2b"

PROMETHEUS_URL = "http://localhost:9090/api/v1/query"
LOKI_URL = "http://localhost:3100/loki/api/v1/query_range"

MAX_ITERATIONS = 5
MAX_OUTPUT_CHARS = 2000

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

# =====================================================================
# 3. Step 4 헬퍼 함수 및 도구 정의
# =====================================================================
def truncate_output(content: str) -> str:
    if len(content) > MAX_OUTPUT_CHARS:
        return f"{content[:MAX_OUTPUT_CHARS]}\n\n[Warning: 데이터가 커서 축약되었습니다.]"
    return content

def query_prometheus(promql: str) -> str:
    print(f"\n[Debug] LLM이 생성한 PromQL: '{promql}'")  # 생성된 쿼리 출력
    try:
        res = requests.get(PROMETHEUS_URL, params={"query": promql}, timeout=5)
        res.raise_for_status()
        data = res.json().get("data", {}).get("result", [])
        if not data:
            return json.dumps({"status": "empty", "message": "조회 결과가 없습니다."})
        return truncate_output(json.dumps(data, ensure_ascii=False))
    except requests.exceptions.HTTPError as e:
        # 400 에러 발생 시 Prometheus가 반환한 상세 이유 출력
        error_msg = res.text if 'res' in locals() else str(e)
        print(f"[Debug Error] Prometheus 400 상세 내용: {error_msg}")
        return json.dumps({"error": f"PromQL 문법 오류 (400 Bad Request): {error_msg}"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"Prometheus 조회 실패: {str(e)}"}, ensure_ascii=False)

def query_loki(logql: str, limit: int = 10) -> str:
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(minutes=60)
    params = {
        "query": logql,
        "limit": limit,
        "start": str(int(start_time.timestamp() * 1e9)),
        "end": str(int(end_time.timestamp() * 1e9))
    }
    try:
        res = requests.get(LOKI_URL, params=params, timeout=5)
        res.raise_for_status()
        results = res.json().get("data", {}).get("result", [])
        logs = [val[1] for stream in results for val in stream.get("values", [])]
        if not logs:
            return json.dumps({"status": "empty", "message": "매칭되는 로그가 없습니다."})
        return truncate_output(json.dumps(logs, ensure_ascii=False))
    except Exception as e:
        return json.dumps({"error": f"Loki 조회 실패: {str(e)}"})

def build_system_prompt(user_context: str = "") -> str:
    base_prompt = """
[전역 분석 및 PromQL 작성 규칙]
너는 SRE 엔지니어를 돕는 관측성 데이터 분석 에이전트다.

[PromQL 작성 엄격 규칙]
1. PromQL 작성 시 반드시 표준 문법만 사용해라.
2. 절대 'since()', 'duration()', '%' 같은 가짜 함수나 문법을 지어내지 마라.
3. 상태 비교 시 '=' 대신 반드시 '=='를 사용해라. (잘못됨: up=0 / 올바름: up == 0 또는 up)
4. 단순 상태 확인 질문에는 가장 단순한 쿼리인 'up' 또는 'up{job="prometheus"}'만 사용해라.
5. 데이터가 'empty'로 돌아오면 가짜 데이터를 만들지 말고 정직하게 데이터가 없다고 답변해라.
"""
    try:
        with open("datasource_config.yaml", "r", encoding="utf-8") as f:
            datasource_metadata = yaml.safe_dump(yaml.safe_load(f), allow_unicode=True)
    except Exception:
        datasource_metadata = ""
    
    return f"{base_prompt}\n[데이터소스 가이드 및 예시]\n{datasource_metadata}\n[사용자 맥락]\n{user_context}"

tools = [
    {
        "type": "function",
        "function": {
            "name": "query_prometheus",
            "description": "Prometheus 시계열 메트릭 조회. 올바른 PromQL 예시: 'up', 'up{job=\"prometheus\"}', 'up == 0'. (주의: 'since', 'duration', '=' 사용 금지)",
            "parameters": {
                "type": "object",
                "properties": {
                    "promql": {
                        "type": "string",
                        "description": "실행할 표준 PromQL 쿼리문 (예: 'up')"
                    }
                },
                "required": ["promql"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "query_loki",
            "description": "Loki 로그 데이터 조회 (LogQL 실행)",
            "parameters": {
                "type": "object",
                "properties": {
                    "logql": {"type": "string", "description": "LogQL 쿼리문 (예: '{job=\"prometheus\"}')"},
                    "limit": {"type": "integer", "default": 10}
                },
                "required": ["logql"]
            }
        }
    }
]

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
    while iteration_count < MAX_ITERATIONS:
        iteration_count += 1
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            tools=tools,
            tool_choice="auto"
        )
        response_msg = response.choices[0].message
        
        # 최종 답변
        if not response_msg.tool_calls:
            return ChatResponse(
                query=req.message,
                reply=response_msg.content,
                iterations=iteration_count
            )
            
        messages.append(response_msg)
        for tool_call in response_msg.tool_calls:
            func_name = tool_call.function.name
            args = json.loads(tool_call.function.arguments)
            
            if func_name == "query_prometheus":
                result = query_prometheus(args.get("promql"))
            elif func_name == "query_loki":
                result = query_loki(args.get("logql"), args.get("limit", 10))
            else:
                result = "Unknown tool"

            messages.append({
                "tool_call_id": tool_call.id,
                "role": "tool",
                "name": func_name,
                "content": result
            })

    raise HTTPException(status_code=500, detail="최대 분석 루프 횟수를 초과했습니다.")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)