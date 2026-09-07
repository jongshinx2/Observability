import os
import math
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Mapping
from urllib.parse import urlsplit

import yaml
from dotenv import dotenv_values

from ..llm.options import LLMRequestOptions


BACKEND_DIR = Path(__file__).resolve().parents[1]
DATASOURCE_DIR = Path(__file__).resolve().parent / "datasources"
PROMPTS_DIR = BACKEND_DIR / "agent" / "prompts"


def _get_int(name: str, default: int, env: Mapping[str, str], minimum: int = 1) -> int:
    raw_value = env.get(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name}은 정수여야 합니다: {raw_value}") from exc
    if value < minimum:
        raise RuntimeError(f"{name}은 {minimum} 이상이어야 합니다: {value}")
    return value


def _get_float(name: str, default: float, env: Mapping[str, str]) -> float:
    raw_value = env.get(name, str(default))
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name}은 숫자여야 합니다: {raw_value}") from exc
    if not math.isfinite(value) or value <= 0:
        raise RuntimeError(f"{name}은 0보다 커야 합니다: {value}")
    return value


@dataclass(frozen=True)
class Settings:
    llm_base_url: str
    llm_api_key: str = field(repr=False)
    llm_auth_mode: str
    llm_connect_timeout: float
    llm_max_retries: int
    orchestrator_model: str
    query_model: str
    synthesizer_model: str
    orchestrator_options: LLMRequestOptions
    query_options: LLMRequestOptions
    synthesizer_options: LLMRequestOptions
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

    @property
    def ollama_base_url(self) -> str:
        """Backward-compatible accessor; new code uses llm_base_url."""
        return self.llm_base_url

    @property
    def synthesizer_reasoning_effort(self) -> str:
        return self.synthesizer_options.reasoning_effort

    @property
    def synthesizer_max_tokens(self) -> int:
        return self.synthesizer_options.max_tokens


def _role_options(
    env: Mapping[str, str], role: str, default_mode: str, default_tokens: int,
) -> LLMRequestOptions:
    prefix = f"SRELENS_{role}"
    thinking = env.get(
        f"{prefix}_ENABLE_THINKING", env.get("SRELENS_LLM_ENABLE_THINKING", "false")
    ).strip().lower()
    if thinking not in {"true", "false"}:
        raise RuntimeError(f"{prefix}_ENABLE_THINKING must be true or false")
    try:
        return LLMRequestOptions(
            max_tokens=_get_int(f"{prefix}_MAX_TOKENS", default_tokens, env),
            reasoning_mode=env.get(
                f"{prefix}_REASONING_MODE",
                env.get("SRELENS_LLM_REASONING_MODE", default_mode),
            ).strip().lower(),
            reasoning_effort=env.get(
                f"{prefix}_REASONING_EFFORT",
                env.get("SRELENS_LLM_REASONING_EFFORT", "none"),
            ).strip().lower(),
            enable_thinking=thinking == "true",
        )
    except ValueError as exc:
        raise RuntimeError(f"{prefix} request options: {exc}") from exc


def load_settings(env_file: str | Path | None = BACKEND_DIR / ".env") -> Settings:
    # Never load .env.example, expand secrets, or mutate the process environment.
    file_env = {
        key: value for key, value in (
            dotenv_values(env_file, encoding="utf-8-sig", interpolate=False)
            if env_file is not None else {}
        ).items() if value is not None
    }
    env = {**file_env, **os.environ}
    # Process settings take priority even when using the legacy URL alias.
    base_url = next((
        layer[key].strip()
        for layer in (os.environ, file_env)
        for key in ("SRELENS_LLM_BASE_URL", "SRELENS_OLLAMA_BASE_URL")
        if layer.get(key, "").strip()
    ), "http://localhost:11434/v1").rstrip("/")
    try:
        url = urlsplit(base_url)
        valid_url = (
            url.scheme in {"http", "https"} and bool(url.hostname)
            and not any(char.isspace() for char in base_url)
            and url.username is None and url.password is None and not url.query and not url.fragment
            and not url.path.endswith(("/chat/completions", "/models"))
            and (url.port is None or url.port > 0)
        )
    except ValueError:
        valid_url = False
    if not valid_url:
        raise RuntimeError("SRELENS_LLM_BASE_URL must be an HTTP(S) API base URL without credentials/query/fragment")
    api_key = env.get("SRELENS_LLM_API_KEY", "").strip()
    local = url.hostname in {"localhost", "127.0.0.1", "::1"}
    auth_mode = env.get(
        "SRELENS_LLM_AUTH_MODE", "api_key" if api_key or not local else "none"
    ).strip().lower()
    if auth_mode not in {"api_key", "none"}:
        raise RuntimeError("SRELENS_LLM_AUTH_MODE must be api_key or none")
    if auth_mode == "api_key" and not api_key:
        raise RuntimeError("Set SRELENS_LLM_API_KEY, or explicitly select SRELENS_LLM_AUTH_MODE=none")
    if any(char.isspace() for char in api_key):
        raise RuntimeError("SRELENS_LLM_API_KEY must not contain whitespace")
    default_model = env.get("SRELENS_MODEL_NAME", "").strip() or "qwen3.5:9b"
    legacy_max_steps = _get_int("SRELENS_MAX_ITERATIONS", 5, env)
    cors_origins = tuple(
        origin.strip()
        for origin in env.get(
            "SRELENS_CORS_ORIGINS",
            "http://localhost:3000,http://127.0.0.1:3000",
        ).split(",")
        if origin.strip()
    )
    if not cors_origins:
        raise RuntimeError("SRELENS_CORS_ORIGINS에 하나 이상의 origin이 필요합니다.")

    log_level = env.get("SRELENS_LOG_LEVEL", "INFO").upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise RuntimeError(f"SRELENS_LOG_LEVEL이 유효하지 않습니다: {log_level}")
    log_format = env.get("SRELENS_LOG_FORMAT", "json").lower()
    if log_format not in {"json", "text"}:
        raise RuntimeError(f"SRELENS_LOG_FORMAT은 json 또는 text여야 합니다: {log_format}")
    raw_log_file = env.get(
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
        llm_base_url=base_url,
        llm_api_key=api_key if auth_mode == "api_key" else "not-required",
        llm_auth_mode=auth_mode,
        llm_connect_timeout=_get_float("SRELENS_LLM_CONNECT_TIMEOUT", 5.0, env),
        llm_max_retries=_get_int("SRELENS_LLM_MAX_RETRIES", 2, env, minimum=0),
        orchestrator_model=env.get("SRELENS_ORCHESTRATOR_MODEL", "").strip() or default_model,
        query_model=env.get("SRELENS_QUERY_MODEL", "").strip() or default_model,
        synthesizer_model=env.get("SRELENS_SYNTHESIZER_MODEL", "").strip() or default_model,
        orchestrator_options=_role_options(env, "ORCHESTRATOR", "omit", 1024),
        query_options=_role_options(env, "QUERY", "omit", 512),
        synthesizer_options=_role_options(env, "SYNTHESIZER", "effort", 512),
        correlation_max_hops=_get_int("SRELENS_CORRELATION_MAX_HOPS", 4, env),
        correlation_buffer_seconds=_get_int("SRELENS_CORRELATION_BUFFER_SECONDS", 120, env),
        correlation_max_trace_ids=_get_int("SRELENS_CORRELATION_MAX_TRACE_IDS", 10, env),
        correlation_max_services=_get_int("SRELENS_CORRELATION_MAX_SERVICES", 10, env),
        correlation_max_span_ids=_get_int("SRELENS_CORRELATION_MAX_SPAN_IDS", 100, env),
        prometheus_url=env.get(
            "SRELENS_PROMETHEUS_URL",
            "http://localhost:9090/api/v1/query",
        ),
        loki_url=env.get(
            "SRELENS_LOKI_URL",
            "http://localhost:3100/loki/api/v1/query_range",
        ),
        tempo_url=env.get("SRELENS_TEMPO_URL", "http://localhost:3200/api/traces"),
        max_steps=min(_get_int("SRELENS_MAX_STEPS", legacy_max_steps, env), 3),
        max_output_chars=_get_int("SRELENS_MAX_OUTPUT_CHARS", 2000, env),
        request_timeout=_get_float("SRELENS_REQUEST_TIMEOUT", 5.0, env),
        llm_timeout=_get_float("SRELENS_LLM_TIMEOUT", 120.0, env),
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
