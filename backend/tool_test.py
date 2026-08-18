import json
from openai import OpenAI

# 1. Ollama 호환 클라이언트 설정
client = OpenAI(
    base_url="http://localhost:11434/v1",
    api_key="ollama"  # Ollama에서는 임의의 문자열 입력 가능
)

MODEL_NAME = "qwen3.5:2b"  # 또는 "llama3.1:8b"

# 2. 로컬에서 실행될 가짜(Mock) 함수 정의
def mock_get_logs(service_name: str, level: str = "ERROR"):
    """LLM이 호출할 가짜 로그 조회 함수"""
    print(f"\n[백엔드 코어] >> 'mock_get_logs' 함수 실제 실행됨!")
    print(f"[백엔드 코어] >> 전달받은 인자: service_name='{service_name}', level='{level}'")
    
    # 가짜 반환 데이터
    return {
        "status": "success",
        "logs": [
            f"[2026-08-06 10:15:00] [{level}] {service_name}: DB connection timeout",
            f"[2026-08-06 10:15:05] [{level}] {service_name}: Internal server error (500)"
        ]
    }

# 3. LLM에게 알려줄 도구 스키마(Tool Definition) 정의
tools = [
    {
        "type": "function",
        "function": {
            "name": "mock_get_logs",
            "description": "특정 서비스의 로그를 조회합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "service_name": {
                        "type": "string",
                        "description": "조회할 서비스의 이름 (예: payment, user, auth 등)"
                    },
                    "level": {
                        "type": "string",
                        "description": "로그 레벨 (예: INFO, WARN, ERROR)",
                        "enum": ["INFO", "WARN", "ERROR"]
                    }
                },
                "required": ["service_name"]
            }
        }
    }
]

def run_test():
    user_query = "payment 서비스에서 발생한 에러 로그 좀 찾아서 원인 요약해줘."
    print(f"사용자 질문: {user_query}\n")

    messages = [{"role": "user", "content": user_query}]

    # 4. LLM 1차 호출 (도구 목록 전달)
    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=messages,
        tools=tools,
        tool_choice="auto"
    )

    response_message = response.choices[0].message
    tool_calls = response_message.tool_calls

    # 5. LLM이 도구 호출을 판단했는지 확인
    if tool_calls:
        print("[LLM 판단] 도구 호출 필요성을 감지했습니다.")
        messages.append(response_message)  # LLM의 답변 맥락 유지

        for tool_call in tool_calls:
            function_name = tool_call.function.name
            function_args = json.loads(tool_call.function.arguments)
            
            print(f"[LLM이 요청한 함수]: {function_name}")
            print(f"[LLM이 추출한 파라미터]: {function_args}")

            # 6. 인수에 맞춰 실제 Python 함수 실행
            if function_name == "mock_get_logs":
                function_response = mock_get_logs(
                    service_name=function_args.get("service_name"),
                    level=function_args.get("level", "ERROR")
                )

                # 7. 함수 실행 결과를 LLM에게 다시 전달 (Tool Role)
                messages.append({
                    "tool_call_id": tool_call.id,
                    "role": "tool",
                    "name": function_name,
                    "content": json.dumps(function_response, ensure_ascii=False)
                })

        # 8. LLM 2차 호출 (함수 결과 데이터를 바탕으로 최종 자연어 응답 생성)
        final_response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages
        )
        print("\n[최종 LLM 답변]:")
        print(final_response.choices[0].message.content)
    else:
        print("[LLM 판단] 도구를 호출하지 않고 직접 답변했습니다:")
        print(response_message.content)

if __name__ == "__main__":
    run_test()