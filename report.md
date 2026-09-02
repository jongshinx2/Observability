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
