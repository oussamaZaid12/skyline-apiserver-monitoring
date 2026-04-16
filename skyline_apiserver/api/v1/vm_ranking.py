# VM Ranking API
import yaml
import asyncio
from fastapi import APIRouter
from skyline_apiserver.config import CONF
from keystoneauth1.identity.v3 import Password
from keystoneauth1.session import Session as KSSession
from novaclient.client import Client as NovaClient
import httpx

router = APIRouter()


def _build_nova_client_from_yaml() -> NovaClient:
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
    return NovaClient("2.1", session=session)


async def get_domain_to_uuid_map() -> dict:
    try:
        nc = await asyncio.get_event_loop().run_in_executor(
            None, _build_nova_client_from_yaml
        )
        servers = await asyncio.get_event_loop().run_in_executor(
            None, lambda: nc.servers.list(search_opts={"all_tenants": True}, limit=500)
        )
        mapping = {}
        for server in servers:
            libvirt_name = getattr(server, "OS-EXT-SRV-ATTR:instance_name", None)
            if libvirt_name:
                mapping[libvirt_name] = {
                    "uuid": server.id,
                    "name": server.name,
                }
        return mapping
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {}


async def query_prometheus(metric: str) -> dict:
    url = f"{CONF.default.prometheus_endpoint}/api/v1/query"
    auth = None
    if CONF.default.prometheus_enable_basic_auth:
        auth = (
            CONF.default.prometheus_basic_auth_user,
            CONF.default.prometheus_basic_auth_password,
        )
    async with httpx.AsyncClient() as client:
        response = await client.get(url, params={"query": metric}, auth=auth)
        return response.json()


@router.get("/vm-ranking")
async def get_vm_ranking():
    cpu_query    = "rate(libvirt_domain_info_cpu_time_seconds_total[5m]) * 100"
    memory_query = "libvirt_domain_info_memory_usage_bytes"
    net_query    = "rate(libvirt_domain_interface_stats_receive_bytes_total[5m])"

    cpu_data, memory_data, net_data = await asyncio.gather(
        query_prometheus(cpu_query),
        query_prometheus(memory_query),
        query_prometheus(net_query),
    )

    loop = asyncio.get_event_loop()
    domain_map = await loop.run_in_executor(None, lambda: asyncio.run(get_domain_to_uuid_map()) if False else {})
    domain_map = await get_domain_to_uuid_map()

    def parse(data):
        result = {}
        for item in data.get("data", {}).get("result", []):
            domain = item["metric"].get("domain", "unknown")
            value  = float(item["value"][1])
            result[domain] = value
        return result

    cpu_values    = parse(cpu_data)
    memory_values = parse(memory_data)
    net_values    = parse(net_data)

    all_domains = set(cpu_values) | set(memory_values) | set(net_values)

    ranking = []
    for domain in all_domains:
        info = domain_map.get(domain, {})
        ranking.append({
            "domain":          domain,
            "uuid":            info.get("uuid", None),
            "name":            info.get("name", domain),
            "cpu_percent":     round(cpu_values.get(domain, 0), 2),
            "memory_mb":       round(memory_values.get(domain, 0) / 1024 / 1024, 1),
            "network_rx_kbps": round(net_values.get(domain, 0) / 1024, 2),
        })

    return {
        "by_cpu":     sorted(ranking, key=lambda x: x["cpu_percent"],     reverse=True),
        "by_memory":  sorted(ranking, key=lambda x: x["memory_mb"],        reverse=True),
        "by_network": sorted(ranking, key=lambda x: x["network_rx_kbps"], reverse=True),
        "total_vms":  len(ranking),
    }