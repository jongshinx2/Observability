import json
import requests
import yaml
from datetime import datetime, timedelta, timezone
from openai import OpenAI

# =====================================================================
# 1. 설정 및 제어 임계값 (Safety Thresholds)
# =====================================================================
client = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")
MODEL_NAME = "qwen3.5:2b"

PROMETHEUS_URL = "http://localhost:9090/api/v1/query"
LOKI_URL = "http://localhost:3100/loki/api/v1/query_range"

# [안전 가드레일 설정]
MAX_ITERATIONS = 5        # LLM 도구 연쇄 호출 최대 횟수 제한
MAX_OUTPUT_CHARS = 2000   # LLM에게 전달할 도구 응답 최대 문자 수

# =====================================================================
# 2. Safety Guard 적용된 Tools
# =====================================================================
def truncate_output(content: str, max_length: int = MAX_OUTPUT_CHARS) -> str:
    """도구 실행 결과가 너무 크면 잘라내고 안내 문구를 첨부합니다."""
    if len(content) > max_length:
        truncated = content[:max_length]
        return f"{truncated}\n\n[System Warning: 데이터가 너무 커서 상위 {max_length}자만 잘라내 표시했습니다. 필요시 조건을 좁혀 재쿼리하세요.]"
    return content

def query_prometheus(promql: str) -> str:
    """Prometheus 시계열 메트릭 조회"""
    print(f"\n  [Tool Execution] Prometheus API -> PromQL: '{promql}'")
    try:
        response = requests.get(PROMETHEUS_URL, params={"query": promql}, timeout=5)
        response.raise_for_status()
        data = response.json().get("data", {}).get("result", [])
        
        # [Fallback] 결과가 비어있을 때 LLM 가이드 주입
        if not data:
            return json.dumps({
                "status": "empty",
                "message": "조회된 메트릭 데이터가 없습니다. PromQL 라벨명이나 메트릭 이름이 올바른지 확인하거나 범위를 넓혀보세요."
            }, ensure_ascii=False)
            
        raw_result = json.dumps(data, ensure_ascii=False)
        return truncate_output(raw_result)
        
    except Exception as e:
        return json.dumps({"error": f"Prometheus 실행 실패: {str(e)}"}, ensure_ascii=False)

def query_loki(logql: str, limit: int = 10, minutes_ago: int = 60) -> str:
    """Loki 로그 데이터 조회"""
    print(f"\n  [Tool Execution] Loki API -> LogQL: '{logql}'")
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
                
        # [Fallback] 로그 결과가 없을 때
        if not extracted_logs:
            return json.dumps({
                "status": "empty",
                "message": f"최근 {minutes_ago}분간 매칭되는 로그가 없습니다. 다른 라벨(job/app)을 시도하거나 LogQL 조건식을 완화하세요."
            }, ensure_ascii=False)
            
        raw_result = json.dumps(extracted_logs, ensure_ascii=False)
        return truncate_output(raw_result)
        
    except Exception as e:
        return json.dumps({"error": f"Loki 실행 실패: {str(e)}"}, ensure_ascii=False)

# =====================================================================
# 3. 프롬프트 및 도구 정의
# =====================================================================
def build_system_prompt(user_context: str = "") -> str:
    base_prompt = """
[전역 분석 및 안전 정책]
너는 SRE 엔지니어를 돕는 관측성 데이터 분석 에이전트다.
1. 질의를 받으면 필요한 도구를 호출해 근거 데이터를 수집해라.
2. 데이터가 'empty'로 돌아오면 다른 라벨/조건으로 최대 1~2회까지만 재시도해라.
3. 2회 이상 재시도해도 데이터가 없으면, 더 이상 도구를 호출하지 말고 즉시 "해당 서비스의 데이터/로그를 찾을 수 없습니다"라고 사용자에게 답변해라.
4. 데이터가 축약되어 Warning 표시가 있는 경우, 제시된 데이터 범위 내에서만 정직하게 분석해라.
"""
    with open("datasource_config.yaml", "r", encoding="utf-8") as f:
        datasource_metadata = yaml.safe_dump(yaml.safe_load(f), allow_unicode=True)
    
    return f"{base_prompt}\n[데이터소스 환경 지식]\n{datasource_metadata}\n[사용자 맥락]\n{user_context}"

tools = [
    {
        "type": "function",
        "function": {
            "name": "query_prometheus",
            "description": "Prometheus 시계열 메트릭 조회 (PromQL 실행)",
            "parameters": {
                "type": "object",
                "properties": {"promql": {"type": "string"}},
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
                    "logql": {"type": "string"},
                    "limit": {"type": "integer", "default": 10}
                },
                "required": ["logql"]
            }
        }
    }
]

# =====================================================================
# 4. Agent Loop with Max Iterations Guard
# =====================================================================
def run_safe_agent(user_query: str, user_context: str = ""):
    print(f"\n==================================================")
    print(f"사용자 질의: '{user_query}'")
    print(f"==================================================")
    
    messages = [
        {"role": "system", "content": build_system_prompt(user_context)},
        {"role": "user", "content": user_query}
    ]

    # [Max Iterations Guard] 루프 횟수 제어 변수
    iteration_count = 0

    while iteration_count < MAX_ITERATIONS:
        iteration_count += 1
        print(f"\n[Agent Loop Step {iteration_count}/{MAX_ITERATIONS}] LLM 추론 중...")

        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            tools=tools,
            tool_choice="auto"
        )

        response_msg = response.choices[0].message
        
        # 도구 호출이 없는 경우 (최종 답변에 도달)
        if not response_msg.tool_calls:
            print("\n=== [SRELens Agent 최종 분석 답변] ===")
            print(response_msg.content)
            return

        # 도구 호출 처리
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

    # 루프 제한 초과 시 강제 종료 가드레일
    print(f"\n[Warning] 최대 도구 호출 횟수({MAX_ITERATIONS}회)를 초과하여 안전을 위해 분석을 중단했습니다.")

if __name__ == "__main__":
    # 테스트 1: 존재하지 않는 라벨 조회 시 폴백 처리 확인
    run_safe_agent("non_existent_service 로그 좀 검색해줘.")