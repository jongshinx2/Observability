import sys
from pathlib import Path
import re

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from openai import OpenAIError

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from backend.agent import Orchestrator, SREWorkflow, Synthesizer
from backend.agent.errors import AgentProtocolError
from backend.config.settings import BACKEND_DIR, Settings, load_settings
from backend.generators import QueryGeneratorRegistry
from backend.llm.client import create_llm_client
from backend.models import ChatRequest, ChatResponse
from backend.observability import (
    bind_request_id,
    configure_logging,
    get_logger,
    new_request_id,
    reset_request_id,
    trace_async,
)
from backend.tools import ToolRegistry


settings: Settings = load_settings()
configure_logging(settings.log_level, settings.log_format, settings.log_file)
logger = get_logger("api")
client = create_llm_client(settings)
logger.info("llm.client.configured", extra={
    "event": "llm.client.configured",
    "base_url": settings.llm_base_url,
    "auth_mode": settings.llm_auth_mode,
    "models": [settings.orchestrator_model, settings.query_model, settings.synthesizer_model],
    "reasoning_modes": {
        "orchestrator": settings.orchestrator_options.reasoning_mode,
        "query": settings.query_options.reasoning_mode,
        "synthesizer": settings.synthesizer_options.reasoning_mode,
    },
    "timeout": settings.llm_timeout,
    "max_retries": settings.llm_max_retries,
})

orchestrator = Orchestrator(client, settings.orchestrator_model, settings.orchestrator_options)
generators = QueryGeneratorRegistry(client, settings)
tool_registry = ToolRegistry(settings)
synthesizer = Synthesizer(
    client,
    settings.synthesizer_model,
    request_options=settings.synthesizer_options,
)
workflow = SREWorkflow(
    orchestrator=orchestrator,
    generators=generators,
    tools=tool_registry,
    synthesizer=synthesizer,
    max_steps=settings.max_steps,
    correlation_max_hops=settings.correlation_max_hops,
    correlation_buffer_seconds=settings.correlation_buffer_seconds,
    correlation_max_trace_ids=settings.correlation_max_trace_ids,
    correlation_max_services=settings.correlation_max_services,
    correlation_max_span_ids=settings.correlation_max_span_ids,
)

app = FastAPI(title="SRELens Agent Local API", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    supplied_id = request.headers.get("X-Request-ID", "")
    request_id = (
        supplied_id
        if re.fullmatch(r"[A-Za-z0-9._-]{1,128}", supplied_id)
        else new_request_id()
    )
    token = bind_request_id(request_id)
    logger.info(
        "api.request.started",
        extra={
            "event": "api.request.started",
            "method": request.method,
            "path": request.url.path,
        },
    )
    try:
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "api.request.completed",
            extra={
                "event": "api.request.completed",
                "method": request.method,
                "path": request.url.path,
                "http_status": response.status_code,
            },
        )
        return response
    except Exception as exc:
        logger.exception(
            "api.request.failed",
            extra={
                "event": "api.request.failed",
                "method": request.method,
                "path": request.url.path,
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        )
        raise
    finally:
        reset_request_id(token)


@app.post("/api/chat", response_model=ChatResponse)
@trace_async("api")
async def chat_endpoint(request: ChatRequest) -> ChatResponse:
    try:
        outcome = await workflow.run(request.message, request.user_context or "")
    except OpenAIError as exc:
        raise HTTPException(status_code=502, detail=f"LLM 호출 실패: {exc}") from exc
    except AgentProtocolError as exc:
        raise HTTPException(status_code=422, detail=f"에이전트 구조화 출력 실패: {exc}") from exc

    return ChatResponse(
        query=request.message,
        reply=outcome.reply,
        iterations=outcome.iterations,
    )


if __name__ == "__main__":
    import uvicorn

    app_import = "main:app" if Path.cwd().resolve() == BACKEND_DIR else "backend.main:app"
    uvicorn.run(app_import, host="0.0.0.0", port=8000, reload=True)
