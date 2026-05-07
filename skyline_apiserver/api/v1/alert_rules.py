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
from skyline_apiserver.client.utils import generate_session, nova_client
from skyline_apiserver.config import CONF
from skyline_apiserver.db import alerts_db
from skyline_apiserver.log import LOG
from starlette.concurrency import run_in_threadpool
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

# ─── Cache en mémoire pour UUID → domain libvirt ─────────────────────────────
_domain_cache: dict = {}
_domain_cache_ttl: dict = {}
CACHE_TTL = 3600  # 1 heure

# ─── État de l'évaluateur ────────────────────────────────────────────────────
_evaluator_last_run: Optional[datetime] = None
_evaluator_rules_count: int = 0

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


@router.get("/alerts/evaluator/status")
async def evaluator_status(profile: schemas.Profile = Depends(deps.get_profile_update_jwt)):
    """Endpoint pour vérifier que l'évaluateur tourne correctement."""
    return {
        "last_run": _evaluator_last_run.isoformat() if _evaluator_last_run else None,
        "active_rules": _evaluator_rules_count,
        "status": "running" if _evaluator_last_run else "starting",
    }

# ─── Évaluateur (background) ─────────────────────────────────────────────────

async def evaluate_alerts():
    """Tourne toutes les 30s en arrière-plan. Interval fixe sans drift."""
    global _evaluator_last_run, _evaluator_rules_count
    LOG.info("[AlertEval] Evaluator démarré")

    while True:
        start = asyncio.get_event_loop().time()
        try:
            rules = await alerts_db.list_active_rules()
            _evaluator_rules_count = len(rules)
            _evaluator_last_run = datetime.utcnow()

            if rules:
                LOG.info(f"[AlertEval] Cycle: {len(rules)} règle(s) active(s)")

            # Évaluer toutes les règles en parallèle
            await asyncio.gather(*[
                _evaluate_rule(rule) for rule in rules
            ], return_exceptions=True)

        except Exception as exc:
            LOG.error(f"[AlertEval] Erreur cycle: {exc}")

        # Interval fixe de 30s peu importe la durée du traitement
        elapsed = asyncio.get_event_loop().time() - start
        await asyncio.sleep(max(0, 30 - elapsed))


async def _get_libvirt_domain(instance_uuid: str) -> Optional[str]:
    now = datetime.utcnow().timestamp()
    if instance_uuid in _domain_cache:
        if now - _domain_cache_ttl.get(instance_uuid, 0) < CACHE_TTL:
            return _domain_cache[instance_uuid]
    try:
        async with httpx.AsyncClient(timeout=10, verify=False) as client:
            # 1. Obtenir un token admin
            token_resp = await client.post(
                "http://197.5.133.150:5000/v3/auth/tokens",
                json={
                    "auth": {
                        "identity": {
                            "methods": ["password"],
                            "password": {
                                "user": {
                                    "name": CONF.openstack.system_user_name,
                                    "domain": {"name": CONF.openstack.system_user_domain},
                                    "password": CONF.openstack.system_user_password,
                                }
                            }
                        },
                        "scope": {
                            "project": {
                                "name": CONF.openstack.system_project,
                                "domain": {"name": CONF.openstack.system_project_domain}
                            }
                        }
                    }
                }
            )
            token = token_resp.headers.get("X-Subject-Token")
            if not token:
                LOG.error("[AlertEval] Impossible d'obtenir un token Keystone")
                return None

            # 2. Récupérer le domain libvirt via Nova
            nova_resp = await client.get(
                f"http://197.5.133.150:8774/v2.1/servers/{instance_uuid}",
                headers={"X-Auth-Token": token},
            )
            server = nova_resp.json().get("server", {})
            domain = server.get("OS-EXT-SRV-ATTR:instance_name")

            if domain:
                _domain_cache[instance_uuid] = domain
                _domain_cache_ttl[instance_uuid] = now
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

    event_data = {
        "rule_id":       rule["id"],
        "user_id":       rule["user_id"],
        "rule_name":     rule["name"],
        "instance_id":   rule["instance_id"],
        "instance_name": rule["instance_name"],
        "metric":        rule["metric"],
        "value":         round(value, 2),
        "threshold":     rule["threshold"],
    }

    event_id = await alerts_db.create_event_if_no_firing(event_data)
    if event_id is None:
        # Un autre worker a déjà créé l'alerte → pas de doublon
        return

    LOG.warning(f"[AlertEval] ALERTE: {rule['name']} → valeur={value} seuil={rule['threshold']}")
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
    msg = (
        f"Alerte: {rule['name']}\n"
        f"VM: {rule['instance_name']}\n"
        f"{label} {op} {rule['threshold']} — valeur: {event['value']}"
    )

    tasks = []
    if rule["notify_email"] and rule.get("email_address"):
        tasks.append(_send_email(rule["email_address"], f"[Skyline] {rule['name']}", msg))
    if rule["notify_webhook"] and rule.get("webhook_url"):
        tasks.append(_send_webhook(rule["webhook_url"], rule, event, msg))

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# ─── SMTP ─────────────────────────────────────────────────────────────────────

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
    _SMTP_CONFIG_CACHE = {
        "host": "localhost", "port": 25,
        "user": None, "password": None,
        "from": "skyline-alerts@openstack.local",
    }
    return _SMTP_CONFIG_CACHE


async def _send_email(to: str, subject: str, body: str):
    try:
        cfg = _load_smtp_config()
        LOG.info(f"[Email] Envoi vers {to} via {cfg['host']}:{cfg['port']} user={cfg['user']}")

        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"]    = cfg["from"]
        msg["To"]      = to

        def _send():
            with smtplib.SMTP(cfg["host"], cfg["port"], timeout=10) as s:
                s.ehlo()
                if cfg["port"] == 587:
                    s.starttls()
                    s.ehlo()
                if cfg["user"] and cfg["password"]:
                    s.login(cfg["user"], cfg["password"])
                s.sendmail(cfg["from"], [to], msg.as_string())

        await asyncio.get_event_loop().run_in_executor(None, _send)
        LOG.info(f"[Email] Envoyé à {to}")

    except Exception as exc:
        LOG.error(f"[Email] {type(exc).__name__}: {exc}")


async def _send_webhook(url: str, rule: dict, event: dict, text: str):
    payload = {
        "text": text,
        "alert": {
            "rule":      rule["name"],
            "instance":  rule["instance_name"],
            "metric":    rule["metric"],
            "value":     event["value"],
            "threshold": rule["threshold"],
        },
    }
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(url, json=payload)
        LOG.info(f"[Webhook] Envoyé à {url}")
    except Exception as exc:
        LOG.error(f"[Webhook] {type(exc).__name__}: {exc}")