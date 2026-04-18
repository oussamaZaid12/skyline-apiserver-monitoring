import asyncio
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from typing import List, Optional

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.param_functions import Depends
from pydantic import BaseModel

from skyline_apiserver import schemas
from skyline_apiserver.api import deps
from skyline_apiserver.config import CONF
from skyline_apiserver.utils.httpclient import _http_request

router = APIRouter()

# ─── Schemas Pydantic ────────────────────────────────────────────────────────

class AlertRuleCreate(BaseModel):
    name: str
    instance_id: str
    instance_name: str
    metric: str           # cpu | ram | disk_read | disk_write | net_in | net_out
    operator: str         # gt | lt
    threshold: float
    duration_seconds: int = 60
    notify_ui: bool = True
    notify_email: bool = False
    email_address: Optional[str] = None
    notify_webhook: bool = False
    webhook_url: Optional[str] = None

# ─── Stockage en mémoire ─────────────────────────────────────────────────────

_rules: List[dict] = []
_events: List[dict] = []
_rule_id_counter = 1
_event_id_counter = 1

# ─── Mapping métrique → PromQL ───────────────────────────────────────────────

METRIC_QUERIES = {
    "cpu":        'rate(libvirt_domain_info_cpu_time_seconds_total{{domain="{domain}"}}[1m]) * 100',
    "ram":        'libvirt_domain_info_memory_usage_bytes{{domain="{domain}"}} / 1024 / 1024',
    "disk_read":  'rate(libvirt_domain_block_stats_read_bytes_total{{domain="{domain}"}}[1m]) / 1024 / 1024',
    "disk_write": 'rate(libvirt_domain_block_stats_write_bytes_total{{domain="{domain}"}}[1m]) / 1024 / 1024',
    "net_in":     'rate(libvirt_domain_interface_stats_receive_bytes_total{{domain="{domain}"}}[1m]) / 1024 / 1024',
    "net_out":    'rate(libvirt_domain_interface_stats_transmit_bytes_total{{domain="{domain}"}}[1m]) / 1024 / 1024',
}

METRIC_LABELS = {
    "cpu":        "CPU (%)",
    "ram":        "RAM (MB)",
    "disk_read":  "Disk Read (MB/s)",
    "disk_write": "Disk Write (MB/s)",
    "net_in":     "Network In (MB/s)",
    "net_out":    "Network Out (MB/s)",
}

# ─── Helpers ─────────────────────────────────────────────────────────────────

def _get_rule(rule_id: int, user_id: str) -> dict:
    for r in _rules:
        if r["id"] == rule_id and r["user_id"] == user_id:
            return r
    raise HTTPException(status_code=404, detail="Rule not found")

def _rule_to_response(r: dict) -> dict:
    return {k: v for k, v in r.items() if k != "user_id"}

def _event_to_response(e: dict) -> dict:
    return {k: v for k, v in e.items() if k != "user_id"}

# ─── Routes CRUD ─────────────────────────────────────────────────────────────

@router.get("/alerts/rules")
async def list_rules(
    profile: schemas.Profile = Depends(deps.get_profile_update_jwt),
):
    user_id = profile.user.id
    return [_rule_to_response(r) for r in _rules if r["user_id"] == user_id]


@router.post("/alerts/rules")
async def create_rule(
    body: AlertRuleCreate,
    profile: schemas.Profile = Depends(deps.get_profile_update_jwt),
):
    global _rule_id_counter
    user_id = profile.user.id
    rule = {
        "id": _rule_id_counter,
        "user_id": user_id,
        "is_active": True,
        "created_at": datetime.utcnow().isoformat(),
        **body.dict(),
    }
    _rules.append(rule)
    _rule_id_counter += 1
    return _rule_to_response(rule)


@router.put("/alerts/rules/{rule_id}")
async def update_rule(
    rule_id: int,
    body: AlertRuleCreate,
    profile: schemas.Profile = Depends(deps.get_profile_update_jwt),
):
    rule = _get_rule(rule_id, profile.user.id)
    rule.update(body.dict())
    return _rule_to_response(rule)


@router.delete("/alerts/rules/{rule_id}")
async def delete_rule(
    rule_id: int,
    profile: schemas.Profile = Depends(deps.get_profile_update_jwt),
):
    global _rules
    _rules = [
        r for r in _rules
        if not (r["id"] == rule_id and r["user_id"] == profile.user.id)
    ]
    return {"status": "deleted"}


@router.patch("/alerts/rules/{rule_id}/toggle")
async def toggle_rule(
    rule_id: int,
    profile: schemas.Profile = Depends(deps.get_profile_update_jwt),
):
    rule = _get_rule(rule_id, profile.user.id)
    rule["is_active"] = not rule["is_active"]
    return {"is_active": rule["is_active"]}


@router.get("/alerts/active")
async def get_active_alerts(
    profile: schemas.Profile = Depends(deps.get_profile_update_jwt),
):
    user_id = profile.user.id
    unresolved = [
        _event_to_response(e)
        for e in _events
        if e["user_id"] == user_id and not e["is_resolved"]
    ]
    return {"count": len(unresolved), "alerts": unresolved}


@router.get("/alerts/history")
async def get_history(
    profile: schemas.Profile = Depends(deps.get_profile_update_jwt),
):
    user_id = profile.user.id
    user_events = [e for e in _events if e["user_id"] == user_id]
    return [_event_to_response(e) for e in reversed(user_events[-50:])]


@router.post("/alerts/events/{event_id}/resolve")
async def resolve_event(
    event_id: int,
    profile: schemas.Profile = Depends(deps.get_profile_update_jwt),
):
    user_id = profile.user.id
    for e in _events:
        if e["id"] == event_id and e["user_id"] == user_id:
            e["is_resolved"] = True
            e["resolved_at"] = datetime.utcnow().isoformat()
            return {"status": "resolved"}
    raise HTTPException(status_code=404, detail="Event not found")

# ─── Évaluateur background ────────────────────────────────────────────────────

async def evaluate_alerts():
    """Tourne en arrière-plan, évalue toutes les règles toutes les 30s."""
    while True:
        await asyncio.sleep(30)
        for rule in list(_rules):
            if not rule["is_active"]:
                continue
            try:
                await _evaluate_rule(rule)
            except Exception as exc:
                print(f"[AlertEval] Erreur règle {rule['id']}: {exc}")


async def _evaluate_rule(rule: dict):
    global _event_id_counter

    if rule["instance_id"] == "all":
        return  # TODO: itérer sur toutes les VMs

    raw_id = rule["instance_id"].replace("-", "")
    domain = f"instance-{raw_id[:8]}"

    query = METRIC_QUERIES[rule["metric"]].format(domain=domain)
    value = await _query_prometheus(query)
    if value is None:
        print(f"[AlertEval] Pas de valeur Prometheus pour règle {rule['id']}")
        return

    triggered = (value > rule["threshold"]) if rule["operator"] == "gt" \
                else (value < rule["threshold"])

    if not triggered:
        for e in _events:
            if e["rule_id"] == rule["id"] and not e["is_resolved"]:
                e["is_resolved"] = True
                e["resolved_at"] = datetime.utcnow().isoformat()
        return

    already_firing = any(
        e["rule_id"] == rule["id"] and not e["is_resolved"]
        for e in _events
    )
    if already_firing:
        return

    event = {
        "id": _event_id_counter,
        "user_id": rule["user_id"],
        "rule_id": rule["id"],
        "rule_name": rule["name"],
        "instance_id": rule["instance_id"],
        "instance_name": rule["instance_name"],
        "metric": rule["metric"],
        "value": round(value, 2),
        "threshold": rule["threshold"],
        "triggered_at": datetime.utcnow().isoformat(),
        "resolved_at": None,
        "is_resolved": False,
    }
    _events.append(event)
    _event_id_counter += 1
    print(f"[AlertEval] ALERTE DÉCLENCHÉE: {rule['name']} → {value}")

    await _send_notifications(rule, event)


async def _query_prometheus(query: str) -> Optional[float]:
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
            global_request_id="",
        )
        data = resp.json()
        results = data.get("data", {}).get("result", [])
        if results:
            return float(results[0]["value"][1])
    except Exception as exc:
        print(f"[Prometheus] {exc}")
    return None


async def _send_notifications(rule: dict, event: dict):
    metric_label = METRIC_LABELS.get(rule["metric"], rule["metric"])
    op = ">" if rule["operator"] == "gt" else "<"
    msg = (
        f"Alerte: {rule['name']}\n"
        f"VM: {rule['instance_name']}\n"
        f"{metric_label} {op} {rule['threshold']} "
        f"— valeur actuelle: {event['value']}"
    )

    if rule["notify_email"] and rule.get("email_address"):
        await _send_email(rule["email_address"], f"[Skyline] {rule['name']}", msg)

    if rule["notify_webhook"] and rule.get("webhook_url"):
        await _send_webhook(rule["webhook_url"], rule, event, msg)


async def _send_email(to: str, subject: str, body: str):
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = "skyline-alerts@openstack.local"
        msg["To"] = to
        with smtplib.SMTP("localhost", 25, timeout=5) as s:
            s.sendmail(msg["From"], [to], msg.as_string())
    except Exception as exc:
        print(f"[Email] {exc}")


async def _send_webhook(url: str, rule: dict, event: dict, text: str):
    payload = {
        "text": text,
        "alert": {
            "rule": rule["name"],
            "instance": rule["instance_name"],
            "metric": rule["metric"],
            "value": event["value"],
            "threshold": rule["threshold"],
            "triggered_at": event["triggered_at"],
        },
    }
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(url, json=payload)
    except Exception as exc:
        print(f"[Webhook] {exc}")
