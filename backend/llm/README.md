# LLM 연결 설정 및 vLLM 호환성 검증

SRELens는 OpenAI-compatible Chat Completions API를 사용합니다. LLM 서버만 교체할 때 Grafana 패널, 관측성 조회 도구, InvestigationContext를 변경할 필요는 없습니다.

## 설정 적용

`backend/.env`를 실행 위치와 무관하게 읽습니다. `.env.example`은 자동으로 읽지 않습니다. `python-dotenv==1.2.3` 의존성이 필요하며 `backend/requirements.txt`에 포함되어 있습니다.

- 같은 키는 프로세스 환경변수가 `.env`보다 우선합니다.
- URL은 프로세스의 신규 URL → 프로세스의 기존 Ollama URL → 파일의 신규 URL → 파일의 기존 URL → 로컬 기본값 순서입니다.
- `.env`는 프로세스 환경을 변경하지 않으며 `${...}` 변수 확장을 하지 않습니다.
- 역할별 모델명이 공통 모델명보다 우선합니다. 역할별 값이 없거나 비어 있으면 공통 모델명을 사용합니다.
- 앱은 시작 시 설정을 읽으므로 변경 후 백엔드를 재시작해야 합니다.
- API 키는 `.env.example`, 보고서, Git에 넣지 마세요. `backend/.env`와 변형 파일은 Git에서 제외합니다.

| 설정 | 기본값 / 의미 |
|---|---|
| `SRELENS_LLM_BASE_URL` | `http://localhost:11434/v1`; 전체 `/chat/completions`가 아닌 API base URL |
| `SRELENS_OLLAMA_BASE_URL` | 호환용 기존 URL 설정 |
| `SRELENS_LLM_API_KEY` | 인증 서버의 API 키; 설정 객체의 repr에서 제외 |
| `SRELENS_LLM_AUTH_MODE` | 키가 있거나 외부 호스트면 `api_key`, 키 없는 loopback은 `none` |
| `SRELENS_MODEL_NAME` | 공통 모델명, 기본 `qwen3.5:9b`; vLLM `/v1/models`의 ID로 변경 필요 |
| `SRELENS_ORCHESTRATOR_MODEL` / `QUERY_MODEL` / `SYNTHESIZER_MODEL` | 각 이름 앞에 `SRELENS_` 사용; 역할별 선택적 override |
| `SRELENS_LLM_TIMEOUT` | 120초, HTTP 요청 timeout; 전체 워크플로우 deadline은 아님 |
| `SRELENS_LLM_CONNECT_TIMEOUT` | 연결 timeout 5초 |
| `SRELENS_LLM_MAX_RETRIES` | SDK 재시도 2회, 0으로 비활성화 가능 |
| `SRELENS_ORCHESTRATOR_MAX_TOKENS` | 1024 |
| `SRELENS_QUERY_MAX_TOKENS` | 512 |
| `SRELENS_SYNTHESIZER_MAX_TOKENS` | 512 |

외부 무인증 서버는 `SRELENS_LLM_AUTH_MODE=none`을 명시해야 합니다. 이 경우 SDK에는 인증용이 아닌 placeholder 키를 전달합니다. 외부 서버를 공개하지 말고 방화벽·프록시 인증·TLS로 접근을 통제하세요. 인증서 검증은 끄지 않습니다. 기본 HTTP 클라이언트는 리다이렉트를 자동으로 따라가지 않습니다.

## Reasoning 제어

서버와 모델의 지원 여부에 맞춰 `SRELENS_LLM_REASONING_MODE`를 선택합니다. 공급자 이름만으로 방식을 추측하거나, 400 오류 후 옵션을 자동 변경해 재요청하지 않습니다.

| 모드 | 실제 요청 |
|---|---|
| `omit` | reasoning 관련 필드 미전송; 서버 기본 동작 사용. 추론 비활성화를 의미하지 않음 |
| `effort` | `reasoning_effort` 전달. `SRELENS_LLM_REASONING_EFFORT=none/low/medium/high` |
| `chat_template` | `extra_body={"chat_template_kwargs":{"enable_thinking":false}}`; `SRELENS_LLM_ENABLE_THINKING=true/false` 사용 |

글로벌 모드를 지정하지 않으면 기존 동작을 유지합니다: 오케스트레이터/쿼리는 `omit`, 통합 분석기는 `effort`와 `none`입니다. 역할별 `SRELENS_ORCHESTRATOR_REASONING_MODE`, `SRELENS_QUERY_REASONING_MODE`, `SRELENS_SYNTHESIZER_REASONING_MODE`로 덮어쓸 수 있습니다. `_REASONING_EFFORT`, `_ENABLE_THINKING`도 동일하게 역할별 override를 지원합니다. 기존 `SRELENS_SYNTHESIZER_REASONING_EFFORT`는 계속 유효합니다.

설정 예시(실제 모델·버전에 맞게 선택):

```dotenv
SRELENS_LLM_BASE_URL=https://<vllm-host>/v1
SRELENS_MODEL_NAME=<served-model-id>
SRELENS_LLM_MAX_RETRIES=0
SRELENS_LLM_REASONING_MODE=effort
SRELENS_LLM_REASONING_EFFORT=none
# SRELENS_LLM_API_KEY는 프로세스 환경변수/시크릿으로 주입
```

`effort` 대신 template 기반 제어가 필요한 모델은 모드를 `chat_template`로 변경하고 `SRELENS_LLM_ENABLE_THINKING=false`를 사용합니다. 지원하지 않는 모델에 임의 적용하지 마세요.

## 실제 서버 검증

프로젝트 루트에서 실행합니다.

```powershell
.\backend\venv\Scripts\python.exe -m backend.llm.check --live
```

다른 설정 파일을 사용할 경우:

```powershell
.\backend\venv\Scripts\python.exe -m backend.llm.check --live --env-file C:\path\to\vllm.env
```

`--live` 없이는 서버에 접속하지 않고 `not_run`, 종료 코드 2를 반환합니다. 검증 시 SDK 재시도는 0회이며 응용 계층 fallback도 금지합니다. API 키는 명령행 인자로 받지 않습니다.

검증 항목:

1. 모델 목록과 설정된 모든 역할의 모델 ID 일치
2. 실제 오케스트레이터의 named function call 및 계획 계약 검증
3. 실제 Prometheus 쿼리 생성기의 LLM 경로 및 쿼리 문법 검증(쿼리를 실행하지는 않음)
4. 실제 통합 분석기의 비어 있지 않은 최종 답변과 정상 토큰 종료 검증
5. 추론 비활성화 요청 시 반환된 reasoning 문자열, `<think>` 태그, 제공되는 reasoning 토큰 통계 검사

합성 질문과 합성 메트릭만 보냅니다. 실제 Tempo/Loki/Prometheus 서버에는 접속하지 않습니다. Loki의 현재 템플릿 전용 경로와 Tempo 결정론적 경로는 LLM 호환성 검증 대상이 아닙니다.

출력은 JSON이며 인증 키, 응답 원문, reasoning 원문은 출력하지 않습니다. 역할별 `finish_reason`, 토큰 수, `content_chars`, `reasoning_chars`, `reasoning_control`, `llm_calls`를 확인할 수 있습니다.

- 종료 코드 0: 합성 요청 검증 통과
- 종료 코드 1: 서버/모델/응답 호환성 검증 실패
- 종료 코드 2: 미실행 또는 설정 오류

`authentication`은 인증, `endpoint_or_model`은 경로/모델, `request_incompatible`은 요청 옵션, `timeout`은 시간초과, `reasoning_disabled_not_observed`는 비활성화 요청과 관측된 추론 출력의 불일치를 의미합니다. `omit`이면 추론 제어 미검증(`not_requested`)으로 표시합니다.

**한계:** 응답에 reasoning이 없다는 사실만으로 내부 추론이 완전히 꺼졌다고 보장할 수 없습니다. 미제공 토큰 통계는 0이 아니라 null로 기록합니다. 설치된 vLLM 버전, 모델 chat template, 서버 설정도 확인해야 합니다. 합성 테스트 통과는 실제 관측성 분석 품질이나 부하 성능의 보증이 아닙니다.

## 오프라인 회귀 검증

```powershell
.\backend\venv\Scripts\python.exe -m unittest discover -s backend/tests -v
```

새 테스트는 실제 OpenAI SDK와 `httpx.MockTransport`를 사용해 HTTP 요청 직렬화와 오류 처리를 확인합니다. 실제 외부 vLLM 테스트를 대신하지 않습니다.

## 참고한 공식 문서

- [OpenAI Python SDK](https://developers.openai.com/api/reference/python)
- [vLLM Tool Calling](https://docs.vllm.ai/en/stable/features/tool_calling/)
- [vLLM Reasoning Outputs](https://docs.vllm.ai/en/stable/features/reasoning_outputs/)

문서의 지원 범위와 실제 설치 버전이 다를 수 있으므로 서버별 검증 결과를 우선합니다.
