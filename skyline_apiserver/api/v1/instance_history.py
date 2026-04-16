# Instance History API
import time
import asyncio
import yaml
from fastapi import APIRouter, HTTPException
from skyline_apiserver.config import CONF
from keystoneauth1.identity.v3 import Password
from keystoneauth1.session import Session as KSSession
from novaclient.client import Client as NovaClient
import httpx

router = APIRouter()

PERIODS = {
    "1h":  {"start": 3600,   "step": 60},
    "6h":  {"start": 21600,  "step": 300},
    "24h": {"start": 86400,  "step": 900},
}


def get_domain_name(instance_id: str) -> str:
    with open("/etc/skyline/skyline.yaml") as f:
        cfg = yaml.safe_load(f)
    os_cfg = cfg.get("openstack", {})
    auth = Password(
        auth_url=os_cfg["keystone_url"],
        username=os_cfg["system_user_name"],
        password=os_cfg["system_user_password"],
        project_name=os_cfg["system_project"],
        user_domain_name=os_cfg["system_user_domain"],
        project_domain_name=os_cfg["system_project_domain"],
    )
    session = KSSession(auth=auth)
    nc = NovaClient("2.1", session=session)
    server = nc.servers.get(instance_id)
    return getattr(server, "OS-EXT-SRV-ATTR:instance_name", None)


async def query_range(metric: str, start: int, end: int, step: int) -> list:
    url = f"{CONF.default.prometheus_endpoint}/api/v1/query_range"
    auth = None
    if CONF.default.prometheus_enable_basic_auth:
        auth = (
            CONF.default.prometheus_basic_auth_user,
            CONF.default.prometheus_basic_auth_password,
        )
    async with httpx.AsyncClient() as client:
        response = await client.get(url, params={
            "query": metric, "start": start, "end": end, "step": step,
        }, auth=auth)
        data = response.json()

    points = []
    results = data.get("data", {}).get("result", [])
    if results:
        for ts, val in results[0].get("values", []):
            points.append({"time": int(ts), "value": round(float(val), 2)})
    return points


@router.get("/instance-history/{instance_id}")
async def get_instance_history(instance_id: str, period: str = "1h"):
    if period not in PERIODS:
        raise HTTPException(status_code=400, detail=f"period must be one of {list(PERIODS.keys())}")

    try:
        loop = asyncio.get_event_loop()
        domain = await loop.run_in_executor(None, get_domain_name, instance_id)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Instance not found: {e}")

    if not domain:
        raise HTTPException(status_code=404, detail="Cannot resolve libvirt domain name")

    now   = int(time.time())
    cfg   = PERIODS[period]
    start = now - cfg["start"]
    step  = cfg["step"]

    cpu_query        = f'rate(libvirt_domain_info_cpu_time_seconds_total{{domain="{domain}"}}[5m]) * 100'
    mem_query        = f'libvirt_domain_info_memory_usage_bytes{{domain="{domain}"}}'
    net_rx_query     = f'rate(libvirt_domain_interface_stats_receive_bytes_total{{domain="{domain}"}}[5m]) * 8 / 1024'
    net_tx_query     = f'rate(libvirt_domain_interface_stats_transmit_bytes_total{{domain="{domain}"}}[5m]) * 8 / 1024'
    disk_read_query  = f'rate(libvirt_domain_block_stats_read_bytes_total{{domain="{domain}"}}[5m])'
    disk_write_query = f'rate(libvirt_domain_block_stats_write_bytes_total{{domain="{domain}"}}[5m])'
    disk_cap_query   = f'libvirt_domain_block_stats_capacity_bytes{{domain="{domain}"}}'
    vcpus_query      = f'libvirt_domain_info_virtual_cpus{{domain="{domain}"}}'

    (cpu_points, mem_points, net_rx_pts, net_tx_pts,
     disk_read_pts, disk_write_pts,
     disk_cap_data, vcpus_data) = await asyncio.gather(
        query_range(cpu_query,        start,    now, step),
        query_range(mem_query,        start,    now, step),
        query_range(net_rx_query,     start,    now, step),
        query_range(net_tx_query,     start,    now, step),
        query_range(disk_read_query,  start,    now, step),
        query_range(disk_write_query, start,    now, step),
        query_range(disk_cap_query,   now - 60, now, 60),
        query_range(vcpus_query,      now - 60, now, 60),
    )

    mem_points       = [{"time": p["time"], "value": round(p["value"] / 1024 / 1024, 1)} for p in mem_points]
    disk_read_pts    = [{"time": p["time"], "value": round(p["value"] / 1024, 2)} for p in disk_read_pts]
    disk_write_pts   = [{"time": p["time"], "value": round(p["value"] / 1024, 2)} for p in disk_write_pts]
    disk_capacity_gb = round(disk_cap_data[0]["value"] / 1024 / 1024 / 1024, 1) if disk_cap_data else 0
    vcpus            = int(vcpus_data[0]["value"]) if vcpus_data else 1

    return {
        "instance_id":      instance_id,
        "domain":           domain,
        "period":           period,
        "vcpus":            vcpus,
        "cpu":              cpu_points,
        "memory_mb":        mem_points,
        "network_rx_kbps":  net_rx_pts,
        "network_tx_kbps":  net_tx_pts,
        "disk_read_kbps":   disk_read_pts,
        "disk_write_kbps":  disk_write_pts,
        "disk_capacity_gb": disk_capacity_gb,
    }