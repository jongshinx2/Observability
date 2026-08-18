import json
import requests
import yaml
from datetime import datetime, timedelta, timezone
from openai import OpenAI

# =====================================================================
# 1. 환경 및 API 설정
# =====================================================================
client = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")
MODEL_NAME = "qwen3.5:2b"  # 또는 "llama3.1:8b"

PROMETHEUS_URL = "http://localhost:9090/api/v1/query"
LOKI_URL = "http://localhost:3100/loki/api/v1/query_range"

# =====================================================================
# 2. Step 2 관측성 통신 함수 (LLM Tools용)
# =====================================================================
def query_prometheus(promql: str) -> str:
    """Prometheus에 PromQL을 실행하여 메트릭을 조회합니다."""
    print(f"\n  [Tool Execution] Prometheus API 호출 -> PromQL: '{promql}'")
    try:
        response = requests.get(PROMETHEUS_URL, params={"query": promql}, timeout=5)
        response.raise_for_status()
        data = response.json().get("data", {}).get("result", [])
        return json.dumps(data, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"Prometheus 조회 실패: {str(e)}"})

def query_loki(logql: str, limit: int = 5, minutes_ago: int = 60) -> str:
    """Loki에 LogQL을 실행하여 최근 로그를 조회합니다."""
    print(f"\n  [Tool Execution] Loki API 호출 -> LogQL: '{logql}'")
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(minutes=minutes_ago)
    
    params = {
        "query": logql,
        "limit": limit,
        "start": str(int(start_time.timestamp() * 1e9)),
        "end": str(int(end_time.timestamp() * 1e9))
    }
    
    try:
        response = requests.get(LOKI_URL, params=params, timeout=5)
        response.raise_for_status()
        results = response.json().get("data", {}).get("result", [])
        
        extracted_logs = []
        for stream in results:
            for val in stream.get("values", []):
                extracted_logs.append(val[1])
                
        return json.dumps(extracted_logs if extracted_logs else "조회된 로그가 없습니다.", ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"Loki 조회 실패: {str(e)}"})

# =====================================================================
# 3. 3계층 프롬프트 합성 (Prompt Synthesizer)
# =====================================================================
def build_system_prompt(user_context: str = "") -> str:
    # Layer 1: Base System Prompt (전역 행동 정책 및 가이드라인)
    base_prompt = """
[전역 분석 정책]
너는 SRE 엔지니어를 돕는 관측성 데이터 분석 에이전트다.
답변할 때 다음 절차를 준수해라:
1. 장애/상태 질문을 받으면 필요한 도구(Prometheus, Loki)를 선택해 호출해라.
2. 메트릭 이상 유무를 먼저 판단하고, 필요 시 로그 조회를 이어 나가라.
3. 데이터에 기반하여 근거를 제시하고 사실만 답변해라.
"""

    # Layer 2: DataSource Fragment (YAML 메타데이터 주입)
    with open("datasource_config.yaml", "r", encoding="utf-8") as f:
        datasource_metadata = yaml.safe_dump(yaml.safe_load(f), allow_unicode=True)
    
    layer2_prompt = f"\n[데이터소스 환경 지식]\n{datasource_metadata}"

    # Layer 3: User Context (사용자/팀 맥락)
    layer3_prompt = f"\n[사용자 맥락]\n{user_context}" if user_context else ""

    # 3개 레이어 합성
    return f"{base_prompt}\n{layer2_prompt}\n{layer3_prompt}"

# =====================================================================
# 4. LLM 도구 정의 (Tools Spec)
# =====================================================================
tools = [
    {
        "type": "function",
        "function": {
            "name": "query_prometheus",
            "description": "Prometheus 시계열 메트릭 조회 (PromQL 실행)",
            "parameters": {
                "type": "object",
                "properties": {
                    "promql": {
                        "type": "string",
                        "description": "실행할 PromQL 쿼리문 (예: 'up', 'rate(http_requests_total[5m])')"
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
                    "logql": {
                        "type": "string",
                        "description": "실행할 LogQL 쿼리문 (예: '{job=\"prometheus\"}')"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "가져올 최대 로그 수",
                        "default": 5
                    }
                },
                "required": ["logql"]
            }
        }
    }
]

# =====================================================================
# 5. 에이전트 실행 루프
# =====================================================================
def run_agent(user_query: str, user_context: str = ""):
    print(f"=== 사용자 질문: '{user_query}' ===")
    
    # 합성된 시스템 프롬프트 준비
    system_prompt = build_system_prompt(user_context)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_query}
    ]

    # LLM 호출 및 도구 연쇄 처리
    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=messages,
        tools=tools,
        tool_choice="auto"
    )

    response_msg = response.choices[0].message
    
    # LLM이 도구 호출을 판단한 경우
    if response_msg.tool_calls:
        messages.append(response_msg)
        
        for tool_call in response_msg.tool_calls:
            func_name = tool_call.function.name
            args = json.loads(tool_call.function.arguments)
            
            # 실제 도구 함수 호출
            if func_name == "query_prometheus":
                result = query_prometheus(args.get("promql"))
            elif func_name == "query_loki":
                result = query_loki(args.get("logql"), args.get("limit", 5))
            else:
                result = "Unknown tool"

            # 도구 결과를 대화 맥락에 보강
            messages.append({
                "tool_call_id": tool_call.id,
                "role": "tool",
                "name": func_name,
                "content": result
            })

        # 도구 실행 결과를 바탕으로 최종 분석 답변 유도
        final_response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages
        )
        print("\n=== [SRELens Agent 최종 분석 결과] ===")
        print(final_response.choices[0].message.content)
    else:
        print("\n=== [SRELens Agent 직접 답변] ===")
        print(response_msg.content)

if __name__ == "__main__":
    # Layer 3 사용자 맥락 예시 주입
    user_ctx = "나는 인프라 팀 담당자이며, prometheus 서비스 상태에 관심이 많다."
    
    # 에이전트 호출 테스트
    run_agent("현재 prometheus 서비스 정상 동작 중이야? 상태 확인해줘.", user_context=user_ctx)