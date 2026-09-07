# 프로젝트 진행현황

## 최초 프로토타입
### 흐름
```
사용자
  │ POST /api/chat
  ▼
FastAPI 에이전트
  │
  ├─ Ollama / qwen3.5:2b ── 도구 선택 및 결과 해석
  │
  ├─ Prometheus ─────────── 메트릭 조회
  ├─ Loki ──────────────── 로그 조회
  └─ Tempo ─────────────── 트레이스 조회
            ▲
            │
샘플 앱 → OTel Collector
            ├─ metrics → Prometheus
            └─ traces  → Tempo

Grafana → Prometheus / Loki / Tempo 시각화
```
### 디렉터리 구조
```
srelens/
├─ backend/
│  ├─ main.py                    # 실제 FastAPI 애플리케이션
│  ├─ datasource_config.yaml     # LLM에 제공할 데이터소스 지식
│  ├─ tools_config.yaml          # LLM Function Calling 스키마
│  ├─ sample_app.py              # 메트릭·트레이스 생성 샘플
│  ├─ agent_core_test.py         # 초기 에이전트 루프 실험
│  ├─ safety_test.py             # 반복 제한·출력 축약 실험
│  ├─ tool_test.py               # Mock 도구 호출 실험
│  ├─ observability_test.py      # Prometheus/Loki 연결 확인
│  ├─ requirements.txt
│  ├─ docker/
│  │  ├─ docker-compose.yml
│  │  ├─ otel-collector.yaml
│  │  ├─ prometheus.yaml
│  │  ├─ tempo.yaml
│  │  └─ grafana-datasources.yaml
│  └─ venv/                      # 로컬 Python 3.14 가상환경
└─ frontend-plugin/              # 현재 완전히 비어 있음
```
### 인프라 구성
| 구성요소 | 포트 | 역할 |
|---|---:|---|
| OTel Collector | 4318 | 샘플 앱의 OTLP 데이터 수신 |
| Prometheus | 9090 | 메트릭 저장·조회 |
| Loki | 3100 | 로그 저장·조회 |
| Tempo | 3200 | 트레이스 저장·조회 |
| Grafana | 3000 | 세 데이터소스 시각화 |

## 오케스트레이션 적용
### 구성
```
사용자 질문
    ↓
SRE 오케스트레이터
    ├─ 메트릭 의도 → Prometheus 전문가 → PromQL
    ├─ 로그 의도   → Loki 전문가       → LogQL
    └─ 추적 의도   → Tempo 전문가      → Trace 조회
    ↓
통합 분석 에이전트
    ↓
최종 답변
```
<b>장점</b>
- Prometheus 전문가에게는 PromQL 규칙만 전달
- Loki 전문가에게는 LogQL 규칙만 전달
- Tempo 전문가는 Trace ID와 span 해석에만 집중
- 전체 datasource_config.yaml을 한 모델에 주입하면서 발생하는 문법 오염 방지
- 각 도구별 모델·temperature·재시도 정책을 독립적으로 조정 가능
- 도구별 테스트가 명확해짐

<b>단점</b>
- LLM 호출 횟수와 응답 지연 증가
- 장애 분석 시 메트릭→로그→트레이스 연결 상태 관리가 복잡해짐
- 작은 질문도 여러 단계를 거쳐 과도하게 무거워짐
- 각 전문가가 서로 다른 결론을 내릴 때 통합 규칙 필요

<b>절충안</b>
- 단일 SRE 오케스트레이터가 도구와 분석 순서를 결정
- 도구마다 독립된 query generator 프롬프트 사용
- 실제 실행 전 기존 Pydantic 문법 검증 유지
- 도구 결과는 마지막 통합 분석 모델이 종합
- 단순한 up이나 Trace ID 조회는 LLM 없이 템플릿으로 생성

### 구조
```
backend/
├─ main.py
├─ agent/
│  ├─ orchestrator.py
│  ├─ synthesizer.py
│  └─ prompts/
│     ├─ orchestrator.txt
│     ├─ prometheus.txt
│     ├─ loki.txt
│     └─ tempo.txt
├─ tools/
│  ├─ prometheus.py
│  ├─ loki.py
│  └─ tempo.py
└─ models/
   └─ tool_args.py
```

### 목표
```
사용자 질문
    ↓
[오케스트레이터]
고수준 조사 계획 생성
    ↓
[도구별 쿼리 생성기]
PromQL │ LogQL │ Tempo lookup
    ↓
[검증기]
문법·범위·비용 검사
    ↓
[도구 실행기]
Prometheus │ Loki │ Tempo
    ↓
[통합 분석기]
근거 통합·한계 명시
    ↓
사용자 답변
```
### 디렉터리 구조 변화
```
backend/
├─ main.py
├─ agent/
│  ├─ orchestrator.py
│  ├─ workflow.py
│  ├─ synthesizer.py
│  └─ prompts/
│     ├─ orchestrator.txt
│     ├─ prometheus.txt
│     ├─ loki.txt
│     ├─ tempo.txt
│     └─ synthesizer.txt
├─ tools/
│  ├─ prometheus.py
│  ├─ loki.py
│  ├─ tempo.py
│  └─ registry.py
├─ generators/
│  ├─ prometheus.py
│  ├─ loki.py
│  └─ tempo.py
├─ models/
│  ├─ plan.py
│  ├─ queries.py
│  └─ results.py
└─ config/
   ├─ prometheus.yaml
   ├─ loki.yaml
   └─ tempo.yaml
```
### 역할
<b>오케스트레이션</b>
- 질문 의도 분석
- 필요한 데이터소스 결정
- 조사 순서 결정
- 각 단계에 서비스명·시간 범위·필터 전달
- 도구 결과를 보고 다음 단계가 필요한지 결정

<b>오케스트레이션이 하면 안되는 일</b>
- PromQL 작성
- LogQL 작성
- 원본 HTTP API 호출
- 관측성 데이터의 최종 결론 생성

### 통합 분석기
입력:
- 사용자 질문
- 실행된 조사 계획
- 정규화된 도구 결과
- 각 결과의 오류·축약·빈 결과 여부

출력 규칙:
- 데이터로 확인된 사실
- 추론
- 확인되지 않은 부분을 분리
- 사용한 데이터소스와 쿼리 표시
- 빈 결과를 정상 상태로 해석하지 않음
- 오류가 있으면 분석 한계 명시

진행 중 오류 발생 [(트러블 슈팅 1)](#1-input-토큰-한계)



## 트러블 슈팅
### 1. input 토큰 한계
통합 분석기의 토큰 소진
```
finish_reason = length
prompt_tokens = 590
completion_tokens = 3506
total_tokens = 4096
content length = 0
reasoning length = 12097
```
qwen3.5:2b가 4,096 토큰 한도를 내부 reasoning에 모두 사용해 최종 답변을 생성하지 못했음 

이후 [synthesizer.py (line 50)](/backend/agent/synthesizer.py)가 빈 content를 "분석 결과를 생성하지 못했습니다."로 치환한 것

#### 해결방법
Trace ID 직접 라우팅

질문에 32자리 hexadecimal Trace ID가 있고 단순 Trace 분석 요청이면 오케스트레이터 LLM을 건너뜀
```
사용자 질문
  → Trace ID 사전 감지
  → Tempo 계획 직접 생성
  → Tempo 조회
```
복합 요청만 기존 오케스트레이터로 보냄
```
“이 Trace와 관련된 로그와 메트릭도 분석해줘”
  → 오케스트레이터 사용
```
예상 효과:
- fallback 제거
- 오케스트레이터 호출 약 12~18초 제거
- 작은 모델이 trace_id 필드를 누락하는 문제 차단
변경 대상:
- backend/agent/orchestrator.py
- 필요하면 backend/agent/router.py 신규 추가
- backend/tests/test_main.py

Synthesizer reasoning 제한

통합 분석기는 쿼리 생성이 필요 없으므로 긴 reasoning이 필요하지 않음

추가할 설정 예시:
```
SRELENS_SYNTHESIZER_REASONING=none
SRELENS_SYNTHESIZER_MAX_TOKENS=512
```
실행 시 현재 Ollama 버전이 지원하는 비사고 모드를 사용하고, 지원하지 않으면 통합 분석기 전용 non-thinking 모델을 사용하도록 구성

주의할 점은 max_tokens만 줄이면 reasoning이 한도를 먼저 소진할 수 있다는 것
reasoning 비활성화 또는 별도 모델 적용이 우선

변경 대상:
- backend/config/settings.py
- backend/agent/synthesizer.py
- backend/.env.example

결정론적 분석 fallback

다음 조건에서는 "분석 결과를 생성하지 못했습니다."를 반환하지 않도록 변경
- message.content가 비어 있음
- finish_reason == "length"
- LLM 호출 timeout
- LLM 응답 구조 오류

Tempo 결과가 성공했다면 최소한 다음 정보를 코드로 생성
```
Trace ID: ...
조회 상태: 성공
서비스: payment-api
Span 수: ...
전체 소요 시간: ...
오류 Span: ...
가장 느린 Span: ...
```
Tempo 조회 실패 시에도 실제 HTTP 상태와 오류 원인을 반환

예상 효과:
- LLM 실패와 관계없이 항상 의미 있는 답변 제공
- 잘못된 PromQL 오류 같은 hallucination 차단
- 재시도 없이 빠른 응답 가능
변경 대상:
- backend/agent/synthesizer.py
- backend/tools/tempo.py 또는 신규 backend/agent/fallback_analyzer.py

LLM 종료 정보 로깅

현재 로그에 다음 필드를 추가합니다.
```
{
  "finish_reason": "length",
  "prompt_tokens": 590,
  "completion_tokens": 3506,
  "content_chars": 0,
  "reasoning_chars": 12097,
  "fallback_applied": true
}
```
reasoning 원문은 기록하지 않고 길이와 토큰 수만 기록

요약 내역
- 단순 Trace ID 질문에서 오케스트레이터 LLM이 호출되지 않음
- Tempo 성공 시 빈 답변이 절대 반환되지 않음
- finish_reason=length를 모의 재현해도 결정론적 분석 반환
- 기존 /api/chat 응답 구조 유지
- Trace 조회 기준 오케스트레이터 지연 제거

---

## 2026-08-24 공통 InvestigationContext 기반 구현

### 작업 목적

Tempo에서 시작한 조사뿐 아니라 향후 Loki, Prometheus 등 어느 데이터소스에서 조사를 시작하더라도 발견된 Trace ID, 서비스명, 시간 범위 등의 단서를 다음 조회에 안전하게 전달할 수 있도록 공통 상관분석 상태 기반을 구현했습니다.

이번 단계는 데이터소스 간 자동 재조회까지 한 번에 활성화하는 단계가 아니라, 재조회 판단과 쿼리 생성이 공통으로 사용할 신뢰 가능한 `InvestigationContext`를 먼저 구축하는 단계입니다.

### 구현 완료 내역

#### 1. 공통 상관분석 계약 추가

다음 모델을 추가했습니다.

- `EvidenceScope`: 직접 근거(`direct`)와 시간 기반 간접 근거(`temporal`) 구분
- `PivotKind`: Trace ID, Span ID, 서비스명, 인스턴스 ID, HTTP 상태 코드, 환경
- `CorrelationPivot`: 값뿐 아니라 출처 데이터소스, 근거 범위, 원본 쿼리를 함께 보존
- `TimeWindow`: 직접 또는 간접 근거에서 도출한 조사 시간 범위
- `ContextDelta`: 각 데이터소스 조회가 새로 발견한 상관키와 시간 범위
- `InvestigationContext`: 누적 상관키, 시간 범위, 실행한 쿼리, 현재 hop 수

Trace ID는 32자리, Span ID는 16자리 hexadecimal 형식만 허용합니다. 잘못된 식별자는 컨텍스트에 들어가지 않습니다.

#### 2. InvestigationContext 병합 엔진 구현

병합 시 다음 안전 규칙을 적용했습니다.

- 동일 종류와 동일 값의 상관키 중복 제거
- 동일 상관키에 더 강한 직접 근거가 발견되면 `temporal`에서 `direct`로 승격
- Trace ID, 서비스명, Span ID별 최대 보관 개수 제한
- 최대 hop 수 제한
- 실행한 데이터소스와 쿼리를 기록해 향후 동일 쿼리 반복 방지에 활용 가능
- 시간 범위를 무조건 합치지 않고 직접 근거를 우선하는 대표 시간창 선택
- 대표 시간창 앞뒤에 설정된 버퍼를 적용한 조회 시간창 생성

기본 제한값은 다음과 같습니다.

```text
SRELENS_CORRELATION_MAX_HOPS=4
SRELENS_CORRELATION_BUFFER_SECONDS=120
SRELENS_CORRELATION_MAX_TRACE_IDS=10
SRELENS_CORRELATION_MAX_SERVICES=10
SRELENS_CORRELATION_MAX_SPAN_IDS=100
```

#### 3. Tempo 상관정보 추출기 구현

Tempo 원본 응답에서 다음 정보를 결정론적으로 추출합니다.

- Trace ID
- Span ID
- `service.name`
- `service.instance.id`
- `deployment.environment.name`
- HTTP 상태 코드
- 전체 Span 시작 및 종료 시각 기반 직접 시간창

LLM이 상관키 값을 생성하지 않으며, 실제 Tempo 응답에 존재하는 값만 컨텍스트에 반영합니다.

중요한 처리 순서 변경:

```text
Tempo 원본 응답
  -> 상관정보 추출
  -> ContextDelta 생성
  -> 크기 제한에 따른 응답 축약
  -> ToolResult 반환
```

따라서 Trace 응답이 `SRELENS_MAX_OUTPUT_CHARS` 제한으로 잘리더라도 상관분석에 필요한 Trace ID, Span ID, 서비스 및 시간창은 보존됩니다.

#### 4. 워크플로우 연결 및 요청 격리

`SREWorkflow.run()` 호출마다 새로운 `InvestigationContext`를 생성하도록 연결했습니다. 이전 요청의 Trace ID나 서비스 정보가 다음 사용자 요청에 섞이지 않습니다.

각 도구 조회 후 다음 상태를 누적합니다.

- 실행한 쿼리
- 새로 추가된 상관키
- 직접 근거로 승격된 상관키
- 추가된 시간 범위
- 전체 pivot 수와 시간창 수
- 현재 hop 수

기존 `/api/chat` 응답 계약은 변경하지 않았습니다. 공통 컨텍스트는 내부 `WorkflowOutcome`에만 포함되며 현재 Grafana 패널 연동에는 영향이 없습니다.

#### 5. 로깅 보강

다음 구조화 로그 이벤트를 추가했습니다.

- `correlation.delta.extracted`: Tempo 원본 응답에서 추출한 pivot 및 시간창 수
- `correlation.context.updated`: 워크플로우 컨텍스트 병합 결과와 누적 상태

예시:

```json
{
  "event": "correlation.context.updated",
  "datasource": "tempo",
  "added_pivots": 6,
  "promoted_pivots": 0,
  "added_time_windows": 1,
  "total_pivots": 6,
  "total_time_windows": 1,
  "visited_query_count": 1,
  "hop_count": 1
}
```

#### 6. 결정론적 fallback 파서 통합

Tempo 상관정보 추출기와 결정론적 fallback 분석기가 공통 Span/attribute 파서를 사용하도록 정리했습니다. 동일한 Tempo 응답을 서로 다른 로직으로 해석해 서비스명, 오류 Span, 실행 시간 계산 결과가 달라질 위험을 줄였습니다.

통합 분석기 LLM 입력에서는 내부 `context_delta` 메타데이터를 제외했습니다. 상관분석 상태 추가로 인해 프롬프트 토큰이 불필요하게 증가하지 않습니다.

### 검증 결과

실행 명령:

```powershell
.\backend\venv\Scripts\python.exe -m unittest discover -s backend/tests -v
.\backend\venv\Scripts\python.exe -m compileall -q backend
git diff --check
```

결과:

- 기존 회귀 테스트 24개 통과
- 신규 상관분석 테스트 6개 통과
- 전체 30개 테스트 통과
- Python 정적 컴파일 통과
- diff 공백 오류 검사 통과

신규 테스트에서 확인한 항목:

- 잘못된 Trace ID 및 Span ID 거부
- 동일 pivot 중복 제거
- 시간 기반 근거를 직접 근거로 승격
- Trace ID, 서비스, Span ID 개수 상한 적용
- 직접 시간창 우선 선택 및 버퍼 적용
- Tempo 응답 축약 전 상관정보 보존
- 요청 간 InvestigationContext 격리

### 주요 변경 파일

- `backend/models/correlation.py`
- `backend/models/enums.py`
- `backend/models/contracts.py`
- `backend/correlation/context.py`
- `backend/correlation/extractors/base.py`
- `backend/correlation/extractors/tempo.py`
- `backend/tools/tempo.py`
- `backend/agent/workflow.py`
- `backend/agent/fallback_analyzer.py`
- `backend/agent/synthesizer.py`
- `backend/config/settings.py`
- `backend/main.py`
- `backend/.env.example`
- `backend/tests/test_correlation.py`

### 현재 범위와 후속 작업

이번 작업으로 공통 상관분석 상태와 Tempo 추출 경로는 준비됐지만, 다음 기능은 아직 활성화하지 않았습니다.

- Loki 결과에서 Trace ID, 서비스, 시간창을 추출하는 전용 extractor
- Prometheus 결과에서 서비스와 시간창을 추출하는 전용 extractor
- 컨텍스트를 입력으로 Loki, Prometheus, Tempo 후속 쿼리를 결정론적으로 생성하는 query builder
- 직접 근거와 시간 기반 간접 근거를 구분해 자동 재조회하는 CorrelationEngine
- 프로파일 데이터소스 연동

따라서 현재 사용자가 Trace ID와 함께 Prometheus 및 Loki 정보를 요청하면 기존 계획에 포함된 조회는 실행되지만, Tempo에서 발견한 정확한 시간창과 서비스명을 사용해 누락된 데이터소스 조회를 자동 추가하는 기능은 후속 단계에서 구현해야 합니다.

권장 다음 순서는 `Loki/Prometheus extractor -> 컨텍스트 기반 안전 쿼리 builder -> 제한된 자동 재조회 루프 -> 통합 분석기의 근거 범위 표기`입니다.

---

## 2026-09-02 LLM 연결 설정 일반화 및 vLLM 호환성 검증 기반

### 진행 상태

**연결 설정 일반화와 오프라인 호환성 검증을 완료했습니다. 실제 외부 vLLM 검증은 접속 정보 미제공으로 미실행 상태입니다.** 이번 변경만으로 vLLM 전환 완료 또는 특정 외부 모델의 호환성 확보를 선언하지 않습니다.

### 배경 및 범위

내부 Ollama에서 외부 vLLM으로 이동할 때 LLM 연결 계층만 교체하고, 기존 오케스트레이터·격리된 쿼리 생성기·통합 분석기 및 InvestigationContext를 유지하는 것이 목적입니다. OpenAI-compatible Chat Completions와 기존 named function calling 계약을 유지했습니다.

이번에는 모델 선정, 프롬프트 전면 개편, 관측성 데이터소스 변경, 자동 상관분석 확대, 외부 서버 배포를 수행하지 않았습니다.

### 구현 내역

1. **공통 연결 설정**
   - `SRELENS_LLM_BASE_URL`, `SRELENS_LLM_API_KEY`, `SRELENS_LLM_AUTH_MODE` 도입
   - 기존 `SRELENS_OLLAMA_BASE_URL`과 설정 접근자 유지
   - 공통 모델명과 역할별 모델 override 지원; 비어 있는 역할별 모델명은 공통 모델명 상속
   - 연결 timeout, 요청 timeout, SDK 재시도 횟수 명시적 설정
   - URL에 자격 증명·query·fragment가 들어가거나 전체 `/chat/completions` 경로를 지정하면 시작 전 거부
   - 키가 없는 외부 서버는 무인증 사용을 명시적으로 선택해야 함

2. **환경 파일 적용 경로 정리**
   - 실행 디렉터리에 관계없이 `backend/.env` 로딩
   - `.env.example` 자동 로딩 금지
   - 같은 설정 키는 프로세스 환경변수 우선; URL의 신규/기존 별칭도 프로세스 설정 우선
   - 설정 로딩 시 프로세스 환경변수를 변경하지 않음
   - 시크릿 문자열의 `${...}` 확장을 비활성화해 원문 유지
   - `python-dotenv==1.2.3`을 requirements에 추가하고 프로젝트 가상환경에 설치
   - `.env` 파일을 Git에서 제외하고, 테스트 폴더 전체 제외 규칙을 제거해 신규 회귀 테스트가 누락되지 않도록 수정

3. **역할별 reasoning 요청 정책**
   - `omit`: 관련 옵션 미전송, 서버 기본 동작 사용
   - `effort`: `reasoning_effort` 전송
   - `chat_template`: `chat_template_kwargs.enable_thinking` 전송
   - 글로벌 설정과 역할별 override 지원
   - 별도 설정이 없으면 기존 정책 유지: 오케스트레이터/쿼리는 omit, 통합 분석기는 effort=none
   - 생성 토큰 상한: 오케스트레이터 1024, 쿼리 512, 통합 분석기 512(각각 설정 가능)
   - 서버 오류 발생 시 reasoning 옵션을 자동 변경해 재호출하지 않음

4. **호출 오류와 구조화 응답 검증 보강**
   - API 키·서버 응답 원문 대신 오류 종류와 HTTP 상태를 기록하는 안전한 LLM 오류 래퍼 도입
   - 인증, 경로/모델, 요청 옵션, rate limit, 서버 장애, timeout 분류
   - 빈 choices, 누락된 함수 호출, 다른 함수명, 복수 함수 호출, 잘못된 JSON, 객체가 아닌 인자, 토큰 제한 종료를 명시적으로 거부
   - 운영 경로의 기존 결정론적 fallback은 유지
   - 검증용 호출에서는 fallback을 금지해 연결 실패가 성공 답변처럼 보이는 현상을 차단

5. **실제 서버 검증 명령 추가**
   - `python -m backend.llm.check --live`
   - 모델 목록 및 역할별 모델 존재 확인
   - 실제 오케스트레이터와 Prometheus 쿼리 생성기의 named function 경로 검증
   - 실제 통합 분석기의 최종 content 및 토큰 종료 검증
   - reasoning 비활성화 요청과 반환된 reasoning 문자열·태그·토큰 통계 불일치 탐지
   - 합성 데이터만 사용하며 Tempo/Loki/Prometheus 조회는 실행하지 않음
   - SDK 재시도 0회 및 fallback 금지
   - `--live` 없이는 네트워크 호출 없이 `not_run` 반환

6. **운영 로그**
   - `llm.client.configured`: 실제 적용 URL, 인증 모드, 역할별 모델·reasoning 모드, timeout, 재시도 설정
   - `llm.response_received`: 함수 호출의 종료 사유, 토큰 수, content/reasoning 길이
   - 통합 분석기 로그에 reasoning 모드와 제공되는 reasoning 토큰 통계 추가
   - API 키 및 reasoning 원문은 신규 로그에 기록하지 않음

### 검증 결과

2026-09-02 실행 기준:

| 검증 | 결과 |
|---|---|
| 변경 전 기존 테스트 | 30개 통과 |
| 변경 후 전체 테스트 | 53개 통과(신규 23개 포함) |
| 실제 SDK의 요청 직렬화 | httpx.MockTransport 기반 통과 |
| omit / effort / chat_template | 함수 호출과 각 LLM 역할의 요청 필드 검증 통과 |
| 400 / 401 / 404 / 429 / 503 / timeout | 오류 분류 및 비밀정보 미노출 검증 통과 |
| 빈 응답·토큰 소진·비정상 함수 응답 | 실패 판정 및 fallback 분리 검증 통과 |
| 모델 누락·인증 실패 | 생성 요청 전에 검증 중단 확인 |
| Python 컴파일 | 통과 |
| git diff 공백 검사 | 통과 |
| 검증 명령의 비활성 실행 | `not_run` 확인, 서버 접속 없음 |
| 실제 외부 vLLM 서버 | 미실행 — URL·모델명·서버 버전·인증 설정 필요 |

테스트는 실제 OpenAI SDK를 사용하지만 HTTP 응답은 모의 처리합니다. 따라서 위 결과는 요청 구성·응답 처리 코드의 검증이며 외부 서버의 모델 품질, tool calling 지원, reasoning 제어 적용 또는 부하 성능을 검증한 결과가 아닙니다.

### 실행 방법과 후속 조치

상세 설정 및 검증 방법은 `backend/llm/README.md`에 기록했습니다.

```powershell
# 프로젝트 루트에서 회귀 테스트
.\backend\venv\Scripts\python.exe -m unittest discover -s backend/tests -v

# 외부 URL·모델명·인증·reasoning 모드를 설정한 후에만 실행
.\backend\venv\Scripts\python.exe -m backend.llm.check --live
```

실제 vLLM 검증에는 접속 URL, `/v1/models`의 모델명, 설치된 vLLM 버전, 인증 및 chat template 설정이 필요합니다. API 키는 채팅이나 보고서에 기록하지 않고 환경변수 또는 비밀 저장소로 주입합니다.

`omit`은 추론 비활성화를 의미하지 않습니다. 응답에 reasoning이 없더라도 서버가 내부 추론을 수행하지 않았다고 단정할 수 없으며, 토큰 통계가 제공되지 않으면 null로 유지합니다. 실제 서버 검증 후 그 결과를 별도 진행 이력으로 추가할 예정입니다.

### 변경 파일

- `backend/llm/`: 클라이언트 생성, 요청 정책, 실서버 검증 명령, 사용 문서
- `backend/config/settings.py`, `backend/.env.example`, `backend/requirements.txt`, `.gitignore`
- `backend/main.py`
- `backend/agent/llm.py`, `backend/agent/orchestrator.py`, `backend/agent/synthesizer.py`
- `backend/generators/prometheus.py`, `backend/generators/loki.py`, `backend/generators/registry.py`
- `backend/tests/test_llm_connection.py`

설계 시 [OpenAI SDK 문서](https://developers.openai.com/api/reference/python), [vLLM 함수 호출 문서](https://docs.vllm.ai/en/stable/features/tool_calling/), [vLLM reasoning 문서](https://docs.vllm.ai/en/stable/features/reasoning_outputs/)를 확인했습니다. 구체적인 옵션 적용은 최신 문서가 아니라 실제 설치 서버와 모델에서의 검증 결과를 기준으로 확정합니다.
