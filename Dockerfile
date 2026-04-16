FROM quay.io/openstack.kolla/skyline-apiserver:2024.2-ubuntu-noble

COPY skyline_apiserver/api/v1/instance_monitoring.py \
     /var/lib/kolla/venv/lib/python3.12/site-packages/skyline_apiserver/api/v1/instance_monitoring.py

COPY skyline_apiserver/api/v1/__init__.py \
     /var/lib/kolla/venv/lib/python3.12/site-packages/skyline_apiserver/api/v1/__init__.py

COPY skyline_apiserver/schemas/monitoring.py \
     /var/lib/kolla/venv/lib/python3.12/site-packages/skyline_apiserver/schemas/monitoring.py

COPY skyline_apiserver/schemas/__init__.py \
     /var/lib/kolla/venv/lib/python3.12/site-packages/skyline_apiserver/schemas/__init__.py
