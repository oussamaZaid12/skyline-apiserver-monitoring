"""
ai_agent.py — Bridge FastAPI Skyline → Agent Service (Phase 1)
Remplace l'ancienne version subprocess.

Changements :
  - Appel HTTP httpx vers 127.0.0.1:8787 (plus de subprocess)
  - Endpoint /ai-agent/chat       → compat. rétro (JSON)
  - Endpoint /ai-agent/chat/stream → SSE proxy vers frontend
  - conversation_id stable par utilisateur (mémoire long terme)
"""
from __future__ import annotations

import hashlib
import logging
import httpx

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from skyline_apiserver import schemas
from skyline_apiserver.api import deps
from fastapi.param_functions import Depends
from fastapi.responses import StreamingResponse, JSONResponse, Response

router = APIRouter()
log = logging.getLogger("skyline.ai_agent")

AGENT_BASE = "http://127.0.0.1:8787"

# Timeout pour la réponse complète (non-streaming)
HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0)
# Timeout pour le premier octet en streaming (le reste n'a pas de timeout)
STREAM_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)


# ─────────────────────────────────────────────────────────────────────────────
# Modèles
# ─────────────────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    history: list = []


def _conversation_id(profile: schemas.Profile) -> str:
    """
    Génère un conversation_id stable par utilisateur + projet.
    Permet la mémoire persistante entre sessions Skyline.
    """
    key = f"{profile.user.id}:{profile.project.id}"
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def _build_payload(
    req: ChatRequest,
    profile: schemas.Profile,
    conversation_id: str,
) -> dict:
    return {
        "message": req.message,
        "history": req.history,
        "keystone_token": profile.keystone_token,
        "project_id": profile.project.id,
        "conversation_id": conversation_id,
    }


# ─────────────────────────────────────────────────────────────────────────────
# POST /ai-agent/chat  (réponse JSON complète — compat. rétro)
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/ai-agent/chat")
async def ai_agent_chat(
    request: ChatRequest,
    profile: schemas.Profile = Depends(deps.get_profile_update_jwt),
):
    conv_id = _conversation_id(profile)
    payload = _build_payload(request, profile, conv_id)

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.post(f"{AGENT_BASE}/chat", json=payload)
            resp.raise_for_status()
            return resp.json()

    except httpx.ConnectError:
        log.error("Agent service unreachable on %s", AGENT_BASE)
        return {
            "response": (
                "Le service agent est indisponible. "
                "Vérifiez que aiops-agent.service est démarré."
            ),
            "status": "error",
        }
    except httpx.TimeoutException:
        return {
            "response": "Délai d'attente dépassé (120s). Réessayez avec une question plus simple.",
            "status": "error",
        }
    except Exception as exc:
        log.error("ai_agent_chat error: %s", exc)
        return {"response": f"Erreur: {str(exc)[:200]}", "status": "error"}


# ─────────────────────────────────────────────────────────────────────────────
# POST /ai-agent/chat/stream  (proxy SSE vers le frontend React)
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/ai-agent/chat/stream")
@router.post("/ai-agent/chat/stream")
async def ai_agent_chat_stream(
    request: ChatRequest,
    profile: schemas.Profile = Depends(deps.get_profile_update_jwt),
):
    """
    Retourne la réponse complète formatée en SSE.
    Le frontend reçoit un seul événement 'done' avec tout le texte.
    Le streaming mot-par-mot est géré côté AiAgent.jsx (simulation).
    """
    conv_id = _conversation_id(profile)
    payload = _build_payload(request, profile, conv_id)

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.post(f"{AGENT_BASE}/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()
            response_text = data.get("response", "Pas de réponse.")

        import json
        # Un seul événement SSE avec la réponse complète
        event = json.dumps({
            "chunk": response_text,
            "done": True,
            "conversation_id": conv_id,
        }, ensure_ascii=False)

        return Response(
            content=f"data: {event}\n\n",
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    except Exception as exc:
        import json
        event = json.dumps({
            "chunk": f"Erreur: {str(exc)[:200]}",
            "done": True,
            "error": True,
        })
        return Response(
            content=f"data: {event}\n\n",
            media_type="text/event-stream",
        )
