"""SSE endpoint for real-time instance metrics — custom EventSourceResponse.
Avoids dependency on sse-starlette which breaks starlette version constraint.
Author: Oussama Zaied - ESPRIT
"""
from __future__ import annotations

import asyncio
import json
import time

import httpx
from fastapi import Depends
from fastapi.routing import APIRouter
from starlette.responses import Response
from starlette.types import Send
from loguru import logger as LOG
from starlette.concurrency import run_in_threadpool

from skyline_apiserver import schemas
from skyline_apiserver.api import deps
from skyline_apiserver.client import utils
from skyline_apiserver.client.utils import generate_session
from skyline_apiserver.config import CONF

router = APIRouter()

METRIC_QUERIES = {
    "cpu_percent":      'rate(libvirt_domain_info_cpu_time_seconds_total{{domain="{domain}"}}[2m]) * 100',
    "memory_bytes":     'libvirt_domain_info_memory_usage_bytes{{domain="{domain}"}}',
    "disk_read_bytes":  'rate(libvirt_domain_block_stats_read_bytes_total{{domain="{domain}"}}[2m])',
    "disk_write_bytes": 'rate(libvirt_domain_block_stats_write_bytes_total{{domain="{domain}"}}[2m])',
    "network_rx_bytes": 'rate(libvirt_domain_interface_stats_receive_bytes_total{{domain="{domain}"}}[2m])',
    "network_tx_bytes": 'rate(libvirt_domain_interface_stats_transmit_bytes_total{{domain="{domain}"}}[2m])',
    "vcpus":            'libvirt_domain_info_virtual_cpus{{domain="{domain}"}}',
}

PUSH_INTERVAL = 5


class SSEResponse(Response):
    media_type = "text/event-stream"

    def __init__(self, generator):
        self.generator = generator
        super().__init__(
            content=None,
            status_code=200,
            media_type=self.media_type,
        )
        self.headers["Cache-Control"] = "no-cache"
        self.headers["Connection"] = "keep-alive"
        self.headers["X-Accel-Buffering"] = "no"

    async def __call__(self, scope, receive, send: Send):
        await send({
            "type": "http.response.start",
            "status": self.status_code,
            "headers": self.raw_headers,
        })
        try:
            async for chunk in self.generator:
                if isinstance(chunk, dict):
                    lines = []
                    if "event" in chunk:
                        lines.append(f"event: {chunk['event']}")
                    if "data" in chunk:
                        lines.append(f"data: {chunk['data']}")
                    message = "\n".join(lines) + "\n\n"
                else:
                    message = str(chunk)
                await send({
                    "type": "http.response.body",
                    "body": message.encode("utf-8"),
                    "more_body": True,
                })
        except asyncio.CancelledError:
            LOG.info("[SSE] Client disconnected")
            raise
        finally:
            await send({
                "type": "http.response.body",
                "body": b"",
                "more_body": False,
            })


async def _query_one(client: httpx.AsyncClient, query: str, auth) -> float:
    try:
        url = f"{CONF.default.prometheus_endpoint}/api/v1/query"
        resp = await client.get(url, params={"query": query}, auth=auth, timeout=5.0)
        data = resp.json()
        results = data.get("data", {}).get("result", [])
        if results:
            return float(results[0]["value"][1])
    except Exception as e:
        LOG.debug(f"[Stream] Query error: {e}")
    return 0.0


async def _resolve_domain(instance_id: str, profile) -> str:
    session = await generate_session(profile=profile)
    nc = await utils.nova_client(region=profile.region, session=session, global_request_id="")
    instance = await run_in_threadpool(nc.servers.get, instance_id)
    return getattr(instance, "OS-EXT-SRV-ATTR:instance_name", None)


@router.get("/instances/{instance_id}/metrics-stream")
async def stream_instance_metrics(
    instance_id: str,
    profile: schemas.Profile = Depends(deps.get_profile_update_jwt),
):
    """Stream live metrics via SSE."""
    LOG.info(f"[Stream] Request for instance_id={instance_id}")

    try:
        domain_name = await _resolve_domain(instance_id, profile)
    except Exception as exc:
        LOG.error(f"[Stream] Cannot resolve domain: {exc}")
        domain_name = None

    auth = None
    if getattr(CONF.default, "prometheus_enable_basic_auth", False):
        auth = (
            CONF.default.prometheus_basic_auth_user,
            CONF.default.prometheus_basic_auth_password,
        )

    async def event_generator():
        if not domain_name:
            yield {"event": "error", "data": json.dumps({"error": "Instance not found"})}
            return

        LOG.info(f"[Stream] Opened for {instance_id} -> domain={domain_name}")
        yield {"event": "connected", "data": json.dumps({"domain": domain_name})}

        async with httpx.AsyncClient() as client:
            while True:
                try:
                    queries = [q.format(domain=domain_name) for q in METRIC_QUERIES.values()]
                    results = await asyncio.gather(*[
                        _query_one(client, q, auth) for q in queries
                    ])
                    payload = {
                        "timestamp":       int(time.time()),
                        "cpu_percent":     round(results[0], 2),
                        "memory_mb":       round(results[1] / 1024 / 1024, 2),
                        "disk_read_kbps":  round(results[2] / 1024, 2),
                        "disk_write_kbps": round(results[3] / 1024, 2),
                        "network_rx_kbps": round(results[4] / 1024, 2),
                        "network_tx_kbps": round(results[5] / 1024, 2),
                        "vcpus":           int(results[6]) if results[6] else 1,
                    }
                    yield {"data": json.dumps(payload)}
                    await asyncio.sleep(PUSH_INTERVAL)

                except asyncio.CancelledError:
                    LOG.info(f"[Stream] Cancelled: {instance_id}")
                    break
                except Exception as exc:
                    LOG.error(f"[Stream] Iteration error: {exc}")
                    yield {"event": "error", "data": json.dumps({"error": str(exc)})}
                    await asyncio.sleep(PUSH_INTERVAL)

    return SSEResponse(event_generator())
