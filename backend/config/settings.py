import os
from dataclasses import dataclass
from pathlib import Path

import yaml


BACKEND_DIR = Path(__file__).resolve().parents[1]
DATASOURCE_DIR = Path(__file__).resolve().parent / "datasources"
PROMPTS_DIR = BACKEND_DIR / "agent" / "prompts"


def _get_int(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name}은 정수여야 합니다: {raw_value}") from exc
    if value < 1:
        raise RuntimeError(f"{name}은 1 이상이어야 합니다: {value}")
    return value


def _get_float(name: str, default: float) -> float:
    raw_value = os.getenv(name, str(default))
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name}은 숫자여야 합니다: {raw_value}") from exc
    if value <= 0:
        raise RuntimeError(f"{name}은 0보다 커야 합니다: {value}")
    return value


@dataclass(frozen=True)
class Settings:
    ollama_base_url: str
    orchestrator_model: str
    query_model: str
    synthesizer_model: str
    synthesizer_reasoning_effort: str
    synthesizer_max_tokens: int
    correlation_max_hops: int
    correlation_buffer_seconds: int
    correlation_max_trace_ids: int
    correlation_max_services: int
    correlation_max_span_ids: int
    prometheus_url: str
    loki_url: str
    tempo_url: str
    max_steps: int
    max_output_chars: int
    request_timeout: float
    llm_timeout: float
    cors_origins: tuple[str, ...]
    log_level: str
    log_format: str
    log_file: str | None


def load_settings() -> Settings:
    default_model = os.getenv("SRELENS_MODEL_NAME", "qwen3.5:9b")
    legacy_max_steps = _get_int("SRELENS_MAX_ITERATIONS", 5)
    cors_origins = tuple(
        origin.strip()
        for origin in os.getenv(
            "SRELENS_CORS_ORIGINS",
            "http://localhost:3000,http://127.0.0.1:3000",
        ).split(",")
        if origin.strip()
    )
    if not cors_origins:
        raise RuntimeError("SRELENS_CORS_ORIGINS에 하나 이상의 origin이 필요합니다.")

    synthesizer_reasoning_effort = os.getenv(
        "SRELENS_SYNTHESIZER_REASONING_EFFORT",
        "none",
    ).lower()
    if synthesizer_reasoning_effort not in {"none", "low", "medium", "high"}:
        raise RuntimeError(
            "SRELENS_SYNTHESIZER_REASONING_EFFORT는 "
            "none, low, medium, high 중 하나여야 합니다: "
            f"{synthesizer_reasoning_effort}"
        )

    log_level = os.getenv("SRELENS_LOG_LEVEL", "INFO").upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise RuntimeError(f"SRELENS_LOG_LEVEL이 유효하지 않습니다: {log_level}")
    log_format = os.getenv("SRELENS_LOG_FORMAT", "json").lower()
    if log_format not in {"json", "text"}:
        raise RuntimeError(f"SRELENS_LOG_FORMAT은 json 또는 text여야 합니다: {log_format}")
    raw_log_file = os.getenv(
        "SRELENS_LOG_FILE",
        "logs/srelens.log",
    ).strip()
    log_file = None
    if raw_log_file:
        log_path = Path(raw_log_file)
        if not log_path.is_absolute():
            log_path = BACKEND_DIR / log_path
        log_file = str(log_path.resolve())

    return Settings(
        ollama_base_url=os.getenv("SRELENS_OLLAMA_BASE_URL", "http://localhost:11434/v1"),
        orchestrator_model=os.getenv("SRELENS_ORCHESTRATOR_MODEL", default_model),
        query_model=os.getenv("SRELENS_QUERY_MODEL", default_model),
        synthesizer_model=os.getenv("SRELENS_SYNTHESIZER_MODEL", default_model),
        synthesizer_reasoning_effort=synthesizer_reasoning_effort,
        synthesizer_max_tokens=_get_int("SRELENS_SYNTHESIZER_MAX_TOKENS", 512),
        correlation_max_hops=_get_int("SRELENS_CORRELATION_MAX_HOPS", 4),
        correlation_buffer_seconds=_get_int("SRELENS_CORRELATION_BUFFER_SECONDS", 120),
        correlation_max_trace_ids=_get_int("SRELENS_CORRELATION_MAX_TRACE_IDS", 10),
        correlation_max_services=_get_int("SRELENS_CORRELATION_MAX_SERVICES", 10),
        correlation_max_span_ids=_get_int("SRELENS_CORRELATION_MAX_SPAN_IDS", 100),
        prometheus_url=os.getenv(
            "SRELENS_PROMETHEUS_URL",
            "http://localhost:9090/api/v1/query",
        ),
        loki_url=os.getenv(
            "SRELENS_LOKI_URL",
            "http://localhost:3100/loki/api/v1/query_range",
        ),
        tempo_url=os.getenv("SRELENS_TEMPO_URL", "http://localhost:3200/api/traces"),
        max_steps=min(_get_int("SRELENS_MAX_STEPS", legacy_max_steps), 3),
        max_output_chars=_get_int("SRELENS_MAX_OUTPUT_CHARS", 2000),
        request_timeout=_get_float("SRELENS_REQUEST_TIMEOUT", 5.0),
        llm_timeout=_get_float("SRELENS_LLM_TIMEOUT", 120.0),
        cors_origins=cors_origins,
        log_level=log_level,
        log_format=log_format,
        log_file=log_file,
    )


def load_datasource_config(name: str) -> dict:
    path = DATASOURCE_DIR / f"{name}.yaml"
    try:
        with path.open("r", encoding="utf-8") as file:
            config = yaml.safe_load(file)
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError(f"데이터소스 설정 로드 실패: {path}") from exc
    if not isinstance(config, dict):
        raise RuntimeError(f"데이터소스 설정은 객체여야 합니다: {path}")
    return config


def load_prompt(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.txt"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"프롬프트 로드 실패: {path}") from exc
