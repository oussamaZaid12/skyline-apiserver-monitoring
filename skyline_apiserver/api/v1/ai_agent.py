# AI Agent OpenStack — bridge FastAPI → CrewAI subprocess
import json
import subprocess
import tempfile
import os
import asyncio
from fastapi import APIRouter
from pydantic import BaseModel
from fastapi.param_functions import Depends
from skyline_apiserver import schemas
from skyline_apiserver.api import deps
router = APIRouter()
CREWAI_SCRIPT = "/opt/crewai_runner.py"


class ChatRequest(BaseModel):
    message: str
    history: list = []

@router.post("/ai-agent/chat")
async def ai_agent_chat(
    request: ChatRequest,
    profile: schemas.Profile = Depends(deps.get_profile_update_jwt)
):
    try:
        payload = {
        "message": request.message, 
        "history": request.history,
        "keystone_token": profile.keystone_token,
        "project_id": profile.project.id,
        "project_name": profile.project.name,
      }
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, _run_crewai, payload)
        return result
    except Exception as e:
        return {"response": f"Erreur: {str(e)}", "status": "error"}


def _run_crewai(payload: dict) -> dict:
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(payload, f)
        input_file = f.name
    output_file = input_file + ".out"
    try:
        result = subprocess.run(
            ["/opt/crewai-venv/bin/python", CREWAI_SCRIPT, input_file, output_file],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            return {
                "response": f"Erreur agent: {result.stderr[-500:] if result.stderr else 'Unknown'}",
                "status": "error"
            }
        if os.path.exists(output_file):
            with open(output_file) as f:
                return json.load(f)
        return {"response": "Pas de réponse.", "status": "error"}
    finally:
        for path in [input_file, output_file]:
            if os.path.exists(path):
                os.unlink(path)