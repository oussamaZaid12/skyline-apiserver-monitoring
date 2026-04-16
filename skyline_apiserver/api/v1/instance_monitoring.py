from __future__ import annotations
from fastapi import status
from fastapi.exceptions import HTTPException
from fastapi.param_functions import Depends, Header
from fastapi.routing import APIRouter
from starlette.concurrency import run_in_threadpool
from skyline_apiserver import schemas
from skyline_apiserver.api import deps
from skyline_apiserver.client import utils
from skyline_apiserver.config import CONF
from skyline_apiserver.types import constants
from skyline_apiserver.utils.httpclient import _http_request
from skyline_apiserver.client.utils import generate_session

router = APIRouter()

def extract_prometheus_value(resp: dict) -> float:
    if resp.get("status") == "success" and resp.get("data", {}).get("result"):
        try:
            return float(resp["data"]["result"][0]["value"][1])
        except (KeyError, IndexError, ValueError):
            return 0.0
    return 0.0

async def query_prometheus(query: str, global_request_id: str = "") -> dict:
    endpoint = CONF.default.prometheus_endpoint
    url = f"{endpoint}/api/v1/query"
    auth = None
    if CONF.default.prometheus_enable_basic_auth:
        auth = (
            CONF.default.prometheus_basic_auth_user,
            CONF.default.prometheus_basic_auth_password,
        )
    try:
        resp = await _http_request(
            method="GET",
            url=url,
            params={"query": query},
            auth=auth,
            global_request_id=global_request_id,
        )
        return resp.json()
    except Exception:
        return {"status": "error", "data": {"result": []}}

@router.get(
    "/instances/{instance_id}/metrics",
    response_model=schemas.InstanceMetricsResponse,
    status_code=status.HTTP_200_OK,
)
async def get_instance_metrics(
    instance_id: str,
    profile: schemas.Profile = Depends(deps.get_profile_update_jwt),
    x_openstack_request_id: str = Header(
        "",
        alias=constants.INBOUND_HEADER,
        pattern=constants.INBOUND_HEADER_REGEX,
    ),
) -> schemas.InstanceMetricsResponse:
    current_session = await generate_session(profile=profile)
    
    # Récupère le nova client
    nc = await utils.nova_client(
        region=profile.region,
        session=current_session,
        global_request_id=x_openstack_request_id,
    )
    
    try:
        instance = await run_in_threadpool(nc.servers.get, instance_id)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Instance not found: {str(e)}")

    # Utilise domain_name (instance_name) pour les requêtes Prometheus
    domain_name = getattr(instance, 'OS-EXT-SRV-ATTR:instance_name', None) or instance.id  # ex: instance-00000005

    cpu_resp        = await query_prometheus(f'rate(libvirt_domain_info_cpu_time_seconds_total{{domain="{domain_name}"}}[5m]) * 100', x_openstack_request_id)
    memory_resp     = await query_prometheus(f'libvirt_domain_info_memory_usage_bytes{{domain="{domain_name}"}}', x_openstack_request_id)
    disk_read_resp  = await query_prometheus(f'rate(libvirt_domain_block_stats_read_bytes_total{{domain="{domain_name}"}}[5m])', x_openstack_request_id)
    disk_write_resp = await query_prometheus(f'rate(libvirt_domain_block_stats_write_bytes_total{{domain="{domain_name}"}}[5m])', x_openstack_request_id)
    net_rx_resp     = await query_prometheus(f'rate(libvirt_domain_interface_stats_receive_bytes_total{{domain="{domain_name}"}}[5m])', x_openstack_request_id)
    net_tx_resp     = await query_prometheus(f'rate(libvirt_domain_interface_stats_transmit_bytes_total{{domain="{domain_name}"}}[5m])', x_openstack_request_id)

    memory_bytes = extract_prometheus_value(memory_resp)
    return schemas.InstanceMetricsResponse(
        instance_id=instance_id,
        instance_name=instance.name,
        instance_status=instance.status,
        cpu_percent=round(extract_prometheus_value(cpu_resp), 2),
        memory_mb=round(memory_bytes / 1024 / 1024, 2),
        memory_bytes=memory_bytes,
        disk_read_bytes_per_sec=round(extract_prometheus_value(disk_read_resp), 2),
        disk_write_bytes_per_sec=round(extract_prometheus_value(disk_write_resp), 2),
        network_rx_bytes_per_sec=round(extract_prometheus_value(net_rx_resp), 2),
        network_tx_bytes_per_sec=round(extract_prometheus_value(net_tx_resp), 2),
    )
