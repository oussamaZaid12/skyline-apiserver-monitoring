"""
agent_service.py — Daemon AIOps sur 127.0.0.1:8787
Tourne dans /opt/crewai-venv, lancé par systemd.

Endpoints :
  POST /chat         → réponse complète JSON (compat. rétro)
  POST /chat/stream  → Server-Sent Events, token par token
  GET  /health       → liveness probe
  GET  /metrics      → métriques Prometheus text/plain
"""
from __future__ import annotations

import os
import time
import uuid
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field

# Import du graphe LangGraph
from langgraph_runner import run_agent

log = logging.getLogger("aiops.service")
logging.basicConfig(
    level=logging.INFO,
    format='{"ts":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}',
    datefmt="%Y-%m-%dT%H:%M:%S",
)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

# ─────────────────────────────────────────────────────────────────────────────
# Métriques internes simples (Prometheus text format)
# ─────────────────────────────────────────────────────────────────────────────

_metrics: dict = {
    "requests_total": 0,
    "requests_success": 0,
    "requests_error": 0,
    "requests_stream": 0,
    "latency_sum_ms": 0.0,
    "latency_count": 0,
}


def _record(success: bool, elapsed_ms: float, stream: bool = False):
    _metrics["requests_total"] += 1
    _metrics["latency_sum_ms"] += elapsed_ms
    _metrics["latency_count"] += 1
    if success:
        _metrics["requests_success"] += 1
    else:
        _metrics["requests_error"] += 1
    if stream:
        _metrics["requests_stream"] += 1


# ─────────────────────────────────────────────────────────────────────────────
# Modèles Pydantic
# ─────────────────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
    history: list[dict] = Field(default_factory=list)
    keystone_token: str = Field(default="")
    project_id: str = Field(default="")
    # conversation_id stable = mémoire persistante entre sessions
    # Si absent, chaque requête est indépendante (compat. rétro)
    conversation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))


# ─────────────────────────────────────────────────────────────────────────────
# App FastAPI
# ─────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("AIOps agent service starting on 127.0.0.1:8787")
    if not GROQ_API_KEY:
        log.warning("GROQ_API_KEY not set — agent will fail on LLM calls")
    yield
    log.info("AIOps agent service stopped")


app = FastAPI(title="AIOps Agent Service", version="2.0.0", lifespan=lifespan)


# ─────────────────────────────────────────────────────────────────────────────
# GET /health
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {
        "status": "ok",
        "groq_key_set": bool(GROQ_API_KEY),
    }


# ─────────────────────────────────────────────────────────────────────────────
# GET /metrics  (Prometheus text format)
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/metrics", response_class=JSONResponse)
def metrics():
    avg_latency = (
        _metrics["latency_sum_ms"] / _metrics["latency_count"]
        if _metrics["latency_count"] > 0
        else 0.0
    )
    lines = [
        "# HELP aiops_requests_total Total requests",
        "# TYPE aiops_requests_total counter",
        f'aiops_requests_total {_metrics["requests_total"]}',
        "# HELP aiops_requests_success Successful requests",
        f'aiops_requests_success {_metrics["requests_success"]}',
        "# HELP aiops_requests_error Failed requests",
        f'aiops_requests_error {_metrics["requests_error"]}',
        "# HELP aiops_requests_stream Streaming requests",
        f'aiops_requests_stream {_metrics["requests_stream"]}',
        "# HELP aiops_latency_avg_ms Average response latency ms",
        f"aiops_latency_avg_ms {avg_latency:.1f}",
    ]
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse("\n".join(lines))


# ─────────────────────────────────────────────────────────────────────────────
# POST /chat  (réponse complète — compatibilité rétro avec Skyline actuel)
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/chat")
async def chat(req: ChatRequest):
    if not GROQ_API_KEY:
        raise HTTPException(503, "GROQ_API_KEY non configuré")

    t0 = time.perf_counter()
    try:
        import asyncio
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: run_agent(
                message=req.message,
                history=req.history,
                keystone_token=req.keystone_token,
                project_id=req.project_id,
                conversation_id=req.conversation_id,
                groq_api_key=GROQ_API_KEY,
                stream=False,
            ),
        )
        elapsed = (time.perf_counter() - t0) * 1000
        _record(True, elapsed)
        log.info("chat ok conv=%s ms=%.0f", req.conversation_id[:8], elapsed)
        return {"response": response, "status": "success", "conversation_id": req.conversation_id}

    except Exception as exc:
        elapsed = (time.perf_counter() - t0) * 1000
        _record(False, elapsed)
        log.error("chat error: %s", exc)
        return {
            "response": f"Erreur agent: {str(exc)[:300]}",
            "status": "error",
            "conversation_id": req.conversation_id,
        }


# ─────────────────────────────────────────────────────────────────────────────
# POST /chat/stream  (SSE token par token)
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    if not GROQ_API_KEY:
        raise HTTPException(503, "GROQ_API_KEY non configuré")

    async def event_generator() -> AsyncGenerator[str, None]:
        t0 = time.perf_counter()
        accumulated = ""
        try:
            # On appelle run_agent en mode NON-stream dans un executor
            # puis on simule le streaming chunk par chunk
            import asyncio
            loop = asyncio.get_running_loop()

            response = await loop.run_in_executor(
                None,
                lambda: run_agent(
                    message=req.message,
                    history=req.history,
                    keystone_token=req.keystone_token,
                    project_id=req.project_id,
                    conversation_id=req.conversation_id,
                    groq_api_key=GROQ_API_KEY,
                    stream=False,  # mode simple, plus fiable
                ),
            )

            # Découpe la réponse en chunks de 4 mots pour simuler le stream
            words = response.split(" ")
            for i in range(0, len(words), 4):
                chunk = " ".join(words[i:i+4]) + " "
                accumulated += chunk
                payload = json_encode({"chunk": chunk, "done": False})
                yield f"data: {payload}\n\n"
                await asyncio.sleep(0.05)  # rythme naturel

            elapsed = (time.perf_counter() - t0) * 1000
            _record(True, elapsed, stream=True)
            log.info("stream ok conv=%s ms=%.0f", req.conversation_id[:8], elapsed)

            payload = json_encode({
                "chunk": "",
                "done": True,
                "conversation_id": req.conversation_id,
            })
            yield f"data: {payload}\n\n"

        except Exception as exc:
            elapsed = (time.perf_counter() - t0) * 1000
            _record(False, elapsed, stream=True)
            log.error("stream error: %s", exc)
            payload = json_encode({
                "chunk": f"Erreur: {str(exc)[:200]}",
                "done": True,
                "error": True,
            })
            yield f"data: {payload}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def json_encode(obj: dict) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)


# ─────────────────────────────────────────────────────────────────────────────
# Entrypoint
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "agent_service:app",
        host="127.0.0.1",
        port=8787,
        log_level="info",
        # workers=1 obligatoire : le graphe LangGraph n'est pas thread-safe
        # avec un seul checkpointer SQLite en multi-process
        workers=1,
    )
