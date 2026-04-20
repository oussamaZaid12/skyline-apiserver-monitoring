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
from skyline_apiserver.client import utils as client_utils
from skyline_apiserver.config import CONF
from skyline_apiserver.db import alerts_db
from skyline_apiserver.log import LOG
import yaml
import os
router = APIRouter()

# ─── Schemas ─────────────────────────────────────────────────────────────────

class AlertRuleCreate(BaseModel):
    name: str
    instance_id: str
    instance_name: str
    metric: str
    operator: str
    threshold: float
    duration_seconds: int = 60
    notify_ui: bool = True
    notify_email: bool = False
    email_address: Optional[str] = None
    notify_webhook: bool = False
    webhook_url: Optional[str] = None

# ─── Cache en mémoire pour UUID → domain libvirt (optimisation) ──────────────
_domain_cache: dict = {}

# ─── PromQL ──────────────────────────────────────────────────────────────────

METRIC_QUERIES = {
    "cpu":        'rate(libvirt_domain_info_cpu_time_seconds_total{{domain="{domain}"}}[2m]) * 100',
    "ram":        'libvirt_domain_info_memory_usage_bytes{{domain="{domain}"}} / 1024 / 1024',
    "disk_read":  'rate(libvirt_domain_block_stats_read_bytes_total{{domain="{domain}"}}[2m]) / 1024 / 1024',
    "disk_write": 'rate(libvirt_domain_block_stats_write_bytes_total{{domain="{domain}"}}[2m]) / 1024 / 1024',
    "net_in":     'rate(libvirt_domain_interface_stats_receive_bytes_total{{domain="{domain}"}}[2m]) / 1024 / 1024',
    "net_out":    'rate(libvirt_domain_interface_stats_transmit_bytes_total{{domain="{domain}"}}[2m]) / 1024 / 1024',
}

METRIC_LABELS = {
    "cpu": "CPU (%)", "ram": "RAM (MB)",
    "disk_read": "Disk Read (MB/s)", "disk_write": "Disk Write (MB/s)",
    "net_in": "Network In (MB/s)", "net_out": "Network Out (MB/s)",
}

# ─── Helpers ─────────────────────────────────────────────────────────────────

def _serialize(row: dict) -> dict:
    """Convertit les DateTime en ISO string et retire user_id."""
    out = dict(row)
    out.pop("user_id", None)
    for k, v in out.items():
        if isinstance(v, datetime):
            out[k] = v.isoformat()
    return out

# ─── Routes CRUD ─────────────────────────────────────────────────────────────

@router.get("/alerts/rules")
async def list_rules(profile: schemas.Profile = Depends(deps.get_profile_update_jwt)):
    rules = await alerts_db.list_rules(profile.user.id)
    return [_serialize(r) for r in rules]


@router.post("/alerts/rules")
async def create_rule(
    body: AlertRuleCreate,
    profile: schemas.Profile = Depends(deps.get_profile_update_jwt),
):
    rule = await alerts_db.create_rule(profile.user.id, body.dict())
    LOG.info(f"[AlertRules] Règle créée: id={rule['id']} name={rule['name']}")
    return _serialize(rule)


@router.put("/alerts/rules/{rule_id}")
async def update_rule(
    rule_id: int,
    body: AlertRuleCreate,
    profile: schemas.Profile = Depends(deps.get_profile_update_jwt),
):
    rule = await alerts_db.update_rule(rule_id, profile.user.id, body.dict())
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    return _serialize(rule)


@router.delete("/alerts/rules/{rule_id}")
async def delete_rule(
    rule_id: int,
    profile: schemas.Profile = Depends(deps.get_profile_update_jwt),
):
    await alerts_db.delete_rule(rule_id, profile.user.id)
    return {"status": "deleted"}


@router.patch("/alerts/rules/{rule_id}/toggle")
async def toggle_rule(
    rule_id: int,
    profile: schemas.Profile = Depends(deps.get_profile_update_jwt),
):
    result = await alerts_db.toggle_rule(rule_id, profile.user.id)
    if not result:
        raise HTTPException(status_code=404, detail="Rule not found")
    return result


@router.get("/alerts/active")
async def get_active_alerts(profile: schemas.Profile = Depends(deps.get_profile_update_jwt)):
    events = await alerts_db.list_active_events(profile.user.id)
    serialized = [_serialize(e) for e in events]
    return {"count": len(serialized), "alerts": serialized}


@router.get("/alerts/history")
async def get_history(profile: schemas.Profile = Depends(deps.get_profile_update_jwt)):
    events = await alerts_db.list_history(profile.user.id, limit=50)
    return [_serialize(e) for e in events]


@router.post("/alerts/events/{event_id}/resolve")
async def resolve_event(
    event_id: int,
    profile: schemas.Profile = Depends(deps.get_profile_update_jwt),
):
    ok = await alerts_db.resolve_event(event_id, profile.user.id)
    if not ok:
        raise HTTPException(status_code=404, detail="Event not found")
    return {"status": "resolved"}

# ─── Évaluateur (background) ─────────────────────────────────────────────────

async def evaluate_alerts():
    """Tourne toutes les 30s en arrière-plan. Un seul worker exécute ça."""
    LOG.info("[AlertEval] Evaluator démarré")
    while True:
        await asyncio.sleep(30)
        try:
            rules = await alerts_db.list_active_rules()
            if rules:
                LOG.info(f"[AlertEval] Cycle: {len(rules)} règle(s) active(s)")
            for rule in rules:
                try:
                    await _evaluate_rule(rule)
                except Exception as exc:
                    LOG.error(f"[AlertEval] Erreur règle {rule['id']}: {exc}")
        except Exception as exc:
            LOG.error(f"[AlertEval] Erreur cycle: {exc}")


async def _get_libvirt_domain(instance_uuid: str) -> Optional[str]:
    if instance_uuid in _domain_cache:
        return _domain_cache[instance_uuid]
    try:
        loop = asyncio.get_event_loop()
        def _fetch():
            from novaclient import client as nova_client
            session = client_utils.get_system_session()
            nova = nova_client.Client(
                version="2.1",
                session=session,
                region_name=CONF.openstack.default_region,
            )
            server = nova.servers.get(instance_uuid)
            return getattr(server, "OS-EXT-SRV-ATTR:instance_name", None)

        domain = await loop.run_in_executor(None, _fetch)
        if domain:
            _domain_cache[instance_uuid] = domain
            LOG.info(f"[AlertEval] UUID {instance_uuid[:8]}... → {domain}")
        return domain
    except Exception as exc:
        LOG.error(f"[AlertEval] Nova lookup: {exc}")
        return None


async def _evaluate_rule(rule: dict):
    if rule["instance_id"] == "all":
        return

    domain = await _get_libvirt_domain(rule["instance_id"])
    if not domain:
        return

    query = METRIC_QUERIES[rule["metric"]].format(domain=domain)
    value = await _query_prometheus(query)

    if value is None:
        return

    triggered = (value > rule["threshold"]) if rule["operator"] == "gt" else (value < rule["threshold"])

    if not triggered:
        await alerts_db.resolve_events_for_rule(rule["id"])
        return

    # Créé l'événement SEULEMENT si aucun autre worker ne l'a déjà fait
    event_data = {
        "rule_id": rule["id"],
        "user_id": rule["user_id"],
        "rule_name": rule["name"],
        "instance_id": rule["instance_id"],
        "instance_name": rule["instance_name"],
        "metric": rule["metric"],
        "value": round(value, 2),
        "threshold": rule["threshold"],
    }
    event_id = await alerts_db.create_event_if_no_firing(event_data)
    if event_id is None:
        # Un autre worker a déjà créé l'alerte → on ne duplique pas, pas de notif
        return

    LOG.warning(f"[AlertEval] 🚨 ALERTE: {rule['name']} → valeur={value} seuil={rule['threshold']}")
    await _send_notifications(rule, event_data)


async def _query_prometheus(query: str) -> Optional[float]:
    endpoint = CONF.default.prometheus_endpoint
    auth = None
    if CONF.default.prometheus_enable_basic_auth:
        auth = (
            CONF.default.prometheus_basic_auth_user,
            CONF.default.prometheus_basic_auth_password,
        )
    try:
        async with httpx.AsyncClient(timeout=10, verify=False) as client:
            resp = await client.get(
                f"{endpoint}/api/v1/query",
                params={"query": query},
                auth=auth,
            )
        results = resp.json().get("data", {}).get("result", [])
        if results:
            return float(results[0]["value"][1])
    except Exception as exc:
        LOG.error(f"[Prometheus] {type(exc).__name__}: {exc}")
    return None


async def _send_notifications(rule: dict, event: dict):
    label = METRIC_LABELS.get(rule["metric"], rule["metric"])
    op = ">" if rule["operator"] == "gt" else "<"
    msg = f"Alerte: {rule['name']}\nVM: {rule['instance_name']}\n{label} {op} {rule['threshold']} — valeur: {event['value']}"

    if rule["notify_email"] and rule.get("email_address"):
        await _send_email(rule["email_address"], f"[Skyline] {rule['name']}", msg)
    if rule["notify_webhook"] and rule.get("webhook_url"):
        await _send_webhook(rule["webhook_url"], rule, event, msg)



_SMTP_CONFIG_CACHE = None

def _load_smtp_config():
    """Charge la config SMTP directement depuis skyline.yaml."""
    global _SMTP_CONFIG_CACHE
    if _SMTP_CONFIG_CACHE is not None:
        return _SMTP_CONFIG_CACHE

    config_paths = [
        "/etc/skyline/skyline.yaml",
        "/etc/kolla/skyline-apiserver/skyline.yaml",
        "/var/lib/kolla/config_files/skyline.yaml",
    ]

    for path in config_paths:
        if os.path.exists(path):
            with open(path) as f:
                data = yaml.safe_load(f) or {}
                default = data.get("default", {}) or {}
                _SMTP_CONFIG_CACHE = {
                    "host":     default.get("smtp_host", "localhost"),
                    "port":     int(default.get("smtp_port", 25)),
                    "user":     default.get("smtp_user"),
                    "password": default.get("smtp_password"),
                    "from":     default.get("smtp_from", "skyline-alerts@openstack.local"),
                }
                LOG.info(f"[SMTP] Config loaded from {path}: host={_SMTP_CONFIG_CACHE['host']}:{_SMTP_CONFIG_CACHE['port']}")
                return _SMTP_CONFIG_CACHE

    LOG.warning("[SMTP] No skyline.yaml found, using defaults")
    _SMTP_CONFIG_CACHE = {"host": "localhost", "port": 25, "user": None, "password": None, "from": "skyline-alerts@openstack.local"}
    return _SMTP_CONFIG_CACHE


async def _send_email(to: str, subject: str, body: str):
    try:
        cfg = _load_smtp_config()
        LOG.info(f"[Email] Envoi vers {to} via {cfg['host']}:{cfg['port']} user={cfg['user']}")

        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = cfg["from"]
        msg["To"] = to

        def _send():
            with smtplib.SMTP(cfg["host"], cfg["port"], timeout=10) as s:
                s.ehlo()
                if cfg["port"] == 587:
                    s.starttls()
                    s.ehlo()
                if cfg["user"] and cfg["password"]:
                    s.login(cfg["user"], cfg["password"])
                s.sendmail(cfg["from"], [to], msg.as_string())

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _send)
        LOG.info(f"[Email] ✓ Envoyé à {to}")
    except Exception as exc:
        LOG.error(f"[Email] ✗ {type(exc).__name__}: {exc}")


async def _send_webhook(url: str, rule: dict, event: dict, text: str):
    payload = {
        "text": text,
        "alert": {
            "rule": rule["name"], "instance": rule["instance_name"],
            "metric": rule["metric"], "value": event["value"],
            "threshold": rule["threshold"],
        },
    }
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(url, json=payload)
    except Exception as exc:
        LOG.error(f"[Webhook] {exc}")