FROM quay.io/openstack.kolla/skyline-apiserver:2024.2-ubuntu-noble
USER root

# Répertoires de base
RUN mkdir -p /var/log/kolla/skyline /etc/skyline

# ── Venv isolé : dépendances agent IA ────────────────────────────────────────
# On REMPLACE crewai par langgraph + langchain-groq (pas de conflit pydantic)
RUN python3 -m venv /opt/crewai-venv && \
    /opt/crewai-venv/bin/pip install --no-cache-dir --retries 5 --timeout 60 \
        langgraph \
        langchain-core \
        langchain-groq \
        langchain-community \
        langgraph-checkpoint-sqlite \
        openstacksdk \
        httpx \
        fastapi \
        uvicorn[standard] \
        pydantic \
        --quiet && \
    find /opt/crewai-venv -name "*.pyc" -delete 2>/dev/null || true

# ── Venv Kolla Skyline : ajouter httpx pour le bridge ai_agent.py ────────────
RUN /var/lib/kolla/venv/bin/pip install --no-cache-dir httpx --quiet

# ── Copie des modules Skyline personnalisés ───────────────────────────────────
COPY skyline_apiserver/api/v1/instance_monitoring.py \
     /var/lib/kolla/venv/lib/python3.12/site-packages/skyline_apiserver/api/v1/instance_monitoring.py
COPY skyline_apiserver/api/v1/__init__.py \
     /var/lib/kolla/venv/lib/python3.12/site-packages/skyline_apiserver/api/v1/__init__.py
COPY skyline_apiserver/schemas/monitoring.py \
     /var/lib/kolla/venv/lib/python3.12/site-packages/skyline_apiserver/schemas/monitoring.py
COPY skyline_apiserver/schemas/__init__.py \
     /var/lib/kolla/venv/lib/python3.12/site-packages/skyline_apiserver/schemas/__init__.py
COPY skyline_apiserver/api/v1/vm_ranking.py \
     /var/lib/kolla/venv/lib/python3.12/site-packages/skyline_apiserver/api/v1/vm_ranking.py
COPY skyline_apiserver/api/v1/instance_history.py \
     /var/lib/kolla/venv/lib/python3.12/site-packages/skyline_apiserver/api/v1/instance_history.py

# ── Bridge AI (Phase 1 : appel HTTP vers daemon 8787) ────────────────────────
COPY skyline_apiserver/api/v1/ai_agent.py \
     /var/lib/kolla/venv/lib/python3.12/site-packages/skyline_apiserver/api/v1/ai_agent.py

COPY skyline_apiserver/api/v1/instance_stream.py \
     /var/lib/kolla/venv/lib/python3.12/site-packages/skyline_apiserver/api/v1/instance_stream.py
COPY skyline_apiserver/api/v1/alert_rules.py \
     /var/lib/kolla/venv/lib/python3.12/site-packages/skyline_apiserver/api/v1/alert_rules.py
COPY skyline_apiserver/db/alerts_db.py \
     /var/lib/kolla/venv/lib/python3.12/site-packages/skyline_apiserver/db/alerts_db.py
COPY skyline_apiserver/db/models.py \
     /var/lib/kolla/venv/lib/python3.12/site-packages/skyline_apiserver/db/models.py
COPY skyline_apiserver/main.py \
     /var/lib/kolla/venv/lib/python3.12/site-packages/skyline_apiserver/main.py

# ── Agent daemon (tourne HORS container, via systemd sur le host) ─────────────
# On copie les fichiers dans /opt pour que systemd les trouve
COPY langgraph_runner.py /opt/langgraph_runner.py
COPY agent_service.py    /opt/agent_service.py
RUN chmod +x /opt/agent_service.py
