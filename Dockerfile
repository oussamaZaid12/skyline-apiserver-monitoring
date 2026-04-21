FROM quay.io/openstack.kolla/skyline-apiserver:2024.2-ubuntu-noble
USER root
RUN mkdir -p /var/log/kolla/skyline /etc/skyline
RUN python3 -m venv /opt/crewai-venv && \
    /opt/crewai-venv/bin/pip install crewai langchain-groq openstacksdk litellm --quiet
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
COPY skyline_apiserver/api/v1/ai_agent.py \
     /var/lib/kolla/venv/lib/python3.12/site-packages/skyline_apiserver/api/v1/ai_agent.py
COPY skyline_apiserver/api/v1/instance_stream.py \
     /var/lib/kolla/venv/lib/python3.12/site-packages/skyline_apiserver/api/v1/instance_stream.py
COPY crewai_runner.py /opt/crewai_runner.py
RUN chmod +x /opt/crewai_runner.py
COPY skyline_apiserver/api/v1/alert_rules.py \
     /var/lib/kolla/venv/lib/python3.12/site-packages/skyline_apiserver/api/v1/alert_rules.py
COPY skyline_apiserver/db/alerts_db.py \
     /var/lib/kolla/venv/lib/python3.12/site-packages/skyline_apiserver/db/alerts_db.py
COPY skyline_apiserver/db/models.py \
     /var/lib/kolla/venv/lib/python3.12/site-packages/skyline_apiserver/db/models.py
COPY skyline_apiserver/main.py \
     /var/lib/kolla/venv/lib/python3.12/site-packages/skyline_apiserver/main.py
