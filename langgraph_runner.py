"""
langgraph_runner.py — Agent AIOps OpenStack (Phase 2)
Nouveautés vs Phase 1 :
  - 3 nouveaux outils OpenSearch : search_logs, get_instance_logs, get_error_summary
  - Supervisor router : détecte intent diagnostic vs opérationnel
  - Prompt enrichi pour RCA (Root Cause Analysis)
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import time
import yaml
from functools import wraps
from typing import Annotated, TypedDict

from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.prebuilt import ToolNode, tools_condition

from langchain_core.messages import HumanMessage, SystemMessage, AIMessage
from langchain_core.tools import tool
from langchain_groq import ChatGroq

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='{"ts":"%(asctime)s","level":"%(levelname)s","msg":%(message)s}',
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("aiops")

_SESSION: dict = {}
SQLITE_DB = "/opt/aiops_checkpoints.db"

# OpenSearch
OPENSEARCH_URL = "http://197.5.133.150:9200"
OPENSEARCH_INDEX = "flog-*"

# ─────────────────────────────────────────────────────────────────────────────
# Utilitaires
# ─────────────────────────────────────────────────────────────────────────────

def sanitize(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r"<[^>]{0,200}>", "", text)
    return text[:2000].strip()


def _load_skyline_cfg() -> dict:
    with open("/etc/kolla/skyline-apiserver/skyline.yaml") as f:
        return yaml.safe_load(f)


_cache: dict[str, tuple[float, str]] = {}
_CACHE_TTL = 60


def _cache_get(key: str) -> str | None:
    if key in _cache:
        ts, val = _cache[key]
        if time.time() - ts < _CACHE_TTL:
            return val
    return None


def _cache_set(key: str, val: str) -> None:
    _cache[key] = (time.time(), val)


def timed(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        t0 = time.perf_counter()
        try:
            result = fn(*args, **kwargs)
            elapsed = (time.perf_counter() - t0) * 1000
            log.info(
                '"tool":"%s","status":"ok","ms":%.0f,"result_len":%d',
                fn.__name__, elapsed, len(str(result)),
            )
            return result
        except Exception as exc:
            elapsed = (time.perf_counter() - t0) * 1000
            log.error(
                '"tool":"%s","status":"error","ms":%.0f,"error":"%s"',
                fn.__name__, elapsed, str(exc)[:200],
            )
            return f"Erreur {fn.__name__}: {exc}"
    return wrapper


# ─────────────────────────────────────────────────────────────────────────────
# Connexion OpenStack
# ─────────────────────────────────────────────────────────────────────────────

def get_os_connection():
    import openstack
    cfg = _load_skyline_cfg()
    os_cfg = cfg["openstack"]
    token = _SESSION.get("keystone_token")
    project_id = _SESSION.get("project_id")

    if not token or not project_id:
        raise ValueError("Token Keystone manquant. Connectez-vous à Skyline d'abord.")

    return openstack.connect(
        auth_type="token",
        auth={
            "auth_url": os_cfg["keystone_url"],
            "token": token,
            "project_id": project_id,
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# Utilitaire OpenSearch
# ─────────────────────────────────────────────────────────────────────────────

def _os_search(query: dict, size: int = 20) -> list[dict]:
    """Exécute une requête OpenSearch et retourne les hits."""
    import httpx
    body = {"size": size, "query": query,
            "sort": [{"@timestamp": {"order": "desc"}}]}
    r = httpx.post(
        f"{OPENSEARCH_URL}/{OPENSEARCH_INDEX}/_search",
        json=body,
        timeout=15,
    )
    r.raise_for_status()
    hits = r.json().get("hits", {}).get("hits", [])
    return [h["_source"] for h in hits]


def _format_log(doc: dict) -> str:
    """Formate un document log en une ligne lisible."""
    ts        = doc.get("@timestamp", "")[:19].replace("T", " ")
    service   = doc.get("programname", "?")
    level     = doc.get("log_level", "HTTP")
    payload   = doc.get("Payload", "")[:200]
    req_id    = doc.get("request_id", "")
    req_short = req_id[:20] if req_id else ""
    return f"[{ts}] {service} {level}: {payload}" + (f" ({req_short})" if req_short else "")


# ─────────────────────────────────────────────────────────────────────────────
# Outils OpenStack (Phase 1 — inchangés)
# ─────────────────────────────────────────────────────────────────────────────

@tool
@timed
def list_instances(query: str = "all") -> str:
    """Liste toutes les instances OpenStack avec statut et IP."""
    conn = get_os_connection()
    servers = list(conn.compute.servers())
    if not servers:
        return "Aucune instance trouvée."
    lines = []
    for s in servers:
        ips = [addr["addr"] for net in s.addresses.values() for addr in net]
        lines.append(
            f"- {s.name} | statut: {s.status} | IP: {', '.join(ips) or 'N/A'} | ID: {s.id}"
        )
    return "\n".join(lines)


@tool
@timed
def list_flavors(query: str = "all") -> str:
    """Liste les flavors disponibles (vCPUs, RAM, disk)."""
    cached = _cache_get("flavors")
    if cached:
        return cached
    conn = get_os_connection()
    result = "\n".join(
        f"- {f.name}: {f.vcpus} vCPUs, {f.ram}MB RAM, {f.disk}GB disk"
        for f in conn.compute.flavors()
    )
    _cache_set("flavors", result)
    return result


@tool
@timed
def list_images(query: str = "all") -> str:
    """Liste les images disponibles."""
    cached = _cache_get("images")
    if cached:
        return cached
    conn = get_os_connection()
    result = "\n".join(
        f"- {img.name} | ID: {img.id} | statut: {img.status}"
        for img in conn.image.images()
    )
    _cache_set("images", result)
    return result


@tool
@timed
def list_networks(query: str = "all") -> str:
    """Liste les réseaux disponibles."""
    cached = _cache_get("networks")
    if cached:
        return cached
    conn = get_os_connection()
    result = "\n".join(
        f"- {n.name} | ID: {n.id} | statut: {n.status}"
        for n in conn.network.networks()
    )
    _cache_set("networks", result)
    return result


@tool
@timed
def create_instance(params: str) -> str:
    """
    Crée une instance OpenStack.
    Format: name=X,flavor=Y,image=Z,network=W
    Appeler list_flavors, list_images, list_networks avant,
    puis demander confirmation à l'utilisateur.
    """
    conn = get_os_connection()
    p = dict(item.split("=", 1) for item in params.split(","))
    name    = p.get("name", "vm-agent")
    flavor  = p.get("flavor", "m1.small")
    image   = p.get("image", "cirros")
    network = p.get("network", "internal")

    flavor_obj  = conn.compute.find_flavor(flavor)
    image_obj   = conn.image.find_image(image)
    network_obj = conn.network.find_network(network)

    if not flavor_obj:  return f"Flavor '{flavor}' introuvable."
    if not image_obj:   return f"Image '{image}' introuvable."
    if not network_obj: return f"Réseau '{network}' introuvable."

    server = conn.compute.create_server(
        name=name,
        flavor_id=flavor_obj.id,
        image_id=image_obj.id,
        networks=[{"uuid": network_obj.id}],
    )
    return f"Instance '{name}' créée. ID: {server.id} | Statut: {server.status}"


@tool
@timed
def delete_instance(name_or_id: str) -> str:
    """
    Supprime une instance par son nom ou ID.
    Demander confirmation explicite avant d'appeler.
    """
    conn = get_os_connection()
    server = conn.compute.find_server(name_or_id)
    if not server:
        return f"Instance '{name_or_id}' introuvable."
    conn.compute.delete_server(server.id)
    return f"Instance '{name_or_id}' supprimée avec succès."


@tool
@timed
def get_instance_metrics(name_or_id: str) -> str:
    """Retourne les métriques CPU/RAM d'une instance via Prometheus."""
    import httpx
    conn = get_os_connection()
    server = conn.compute.find_server(name_or_id)
    if not server:
        return f"Instance '{name_or_id}' introuvable."

    domain = getattr(server, "OS-EXT-SRV-ATTR:instance_name", None)
    if not domain:
        return "Impossible de récupérer le nom libvirt."

    cfg = _load_skyline_cfg()
    prom_url  = cfg["default"]["prometheus_endpoint"]
    prom_user = cfg["default"]["prometheus_basic_auth_user"]
    prom_pass = cfg["default"]["prometheus_basic_auth_password"]
    auth = (prom_user, prom_pass)

    def prom_query(q):
        r = httpx.get(
            f"{prom_url}/api/v1/query",
            params={"query": q}, auth=auth, timeout=10,
        )
        results = r.json().get("data", {}).get("result", [])
        return float(results[0]["value"][1]) if results else 0.0

    cpu    = prom_query(
        f'rate(libvirt_domain_info_cpu_time_seconds_total{{domain="{domain}"}}[5m]) * 100'
    )
    mem_mb = prom_query(
        f'libvirt_domain_info_memory_usage_bytes{{domain="{domain}"}}'
    ) / 1024 / 1024

    return (
        f"Instance '{server.name}':\n"
        f"  CPU:    {cpu:.1f}%\n"
        f"  RAM:    {mem_mb:.0f} MB\n"
        f"  Statut: {server.status}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Outils OpenSearch (Phase 2 — NOUVEAUX)
# ─────────────────────────────────────────────────────────────────────────────

@tool
@timed
def search_logs(params: str) -> str:
    """
    Recherche dans les logs OpenStack via OpenSearch.
    Format params: query=TEXT,service=SERVICE,level=LEVEL,minutes=N
    Exemples:
      query=NoValidHost,service=nova-api,level=ERROR,minutes=60
      query=authentication failed,level=ERROR,minutes=30
      query=instance,service=nova-compute,minutes=120
    Paramètres optionnels — utilise seulement ceux pertinents.
    """
    p: dict = {}
    for item in params.split(","):
        if "=" in item:
            k, v = item.split("=", 1)
            p[k.strip()] = v.strip()

    query_text = p.get("query", "")
    service    = p.get("service", "")
    level      = p.get("level", "")
    minutes    = int(p.get("minutes", "60"))

    # Construction de la requête OpenSearch
    must: list = []

    if query_text:
        must.append({"match": {"Payload": {"query": query_text, "operator": "or"}}})

    if service:
        must.append({"match": {"programname": service}})

    if level:
        # Normalise les variantes (INFO/info/Warning/WARNING...)
        level_variants = [level, level.upper(), level.lower(), level.capitalize()]
        must.append({"terms": {"log_level": level_variants}})

    # Filtre temporel
    must.append({
        "range": {
            "@timestamp": {
                "gte": f"now-{minutes}m",
                "lte": "now",
            }
        }
    })

    query = {"bool": {"must": must}} if must else {"match_all": {}}

    try:
        docs = _os_search(query, size=15)
        if not docs:
            return f"Aucun log trouvé pour: {params}"

        lines = [f"=== {len(docs)} logs trouvés (params: {params}) ==="]
        for doc in docs:
            lines.append(_format_log(doc))
        return "\n".join(lines)

    except Exception as e:
        return f"Erreur OpenSearch: {e}"


@tool
@timed
def get_instance_logs(instance_name: str) -> str:
    """
    Récupère les logs récents liés à une instance spécifique.
    Cherche par nom ET par UUID dans nova, neutron, cinder.
    Exemple: get_instance_logs("felcloud")
    """
    try:
        # Récupérer l'UUID via Nova pour chercher dans les logs
        search_terms = [instance_name]
        try:
            conn = get_os_connection()
            server = conn.compute.find_server(instance_name)
            if server:
                search_terms.append(server.id)
                search_terms.append(server.id.replace("-", ""))
        except Exception:
            pass

        # Construire une requête OR sur tous les termes
        should = [
            {"match": {"Payload": term}}
            for term in search_terms
        ]

        query = {
            "bool": {
                "must": [
                    {"range": {"@timestamp": {"gte": "now-24h", "lte": "now"}}},
                    {"terms": {
                        "log_level": [
                            "ERROR", "WARNING", "WARN",
                            "error", "warning", "Warning"
                        ]
                    }},
                ],
                "should": should,
                "minimum_should_match": 1,
            }
        }

        docs = _os_search(query, size=20)

        # Si rien en erreur, cherche tous niveaux
        if not docs:
            query["bool"].pop("must")
            query = {
                "bool": {
                    "must": [
                        {"range": {"@timestamp": {"gte": "now-24h"}}},
                    ],
                    "should": should,
                    "minimum_should_match": 1,
                }
            }
            docs = _os_search(query, size=10)

        if not docs:
            return (
                f"Aucun log trouvé pour '{instance_name}' "
                f"(UUID: {search_terms[1] if len(search_terms) > 1 else 'inconnu'}) "
                f"dans les dernières 24h."
            )

        uuid_str = search_terms[1][:8] if len(search_terms) > 1 else "?"
        lines = [f"=== Logs '{instance_name}' (UUID: {uuid_str}...) — {len(docs)} entrées ==="]
        for doc in docs:
            lines.append(_format_log(doc))
        return "\n".join(lines)

    except Exception as e:
        return f"Erreur get_instance_logs: {e}"


@tool
@timed
def get_error_summary(minutes: str = "60") -> str:
    """
    Résumé des erreurs récentes dans toute l'infrastructure OpenStack.
    Groupe les erreurs par service et affiche les messages les plus fréquents.
    Utile pour un diagnostic global : 'y a-t-il des problèmes en ce moment ?'
    Paramètre minutes: fenêtre temporelle (défaut 60 minutes).
    """
    try:
        n_minutes = int(minutes)

        # Récupère tous les logs ERROR récents
        query = {
            "bool": {
                "must": [
                    {"terms": {
                        "log_level": ["ERROR", "error", "CRITICAL"]
                    }},
                    {"range": {
                        "@timestamp": {
                            "gte": f"now-{n_minutes}m",
                            "lte": "now",
                        }
                    }},
                ]
            }
        }

        docs = _os_search(query, size=50)

        if not docs:
            return f"Aucune erreur détectée dans les {n_minutes} dernières minutes. Infrastructure saine."

        # Groupe par service
        from collections import defaultdict
        by_service: dict = defaultdict(list)
        for doc in docs:
            svc = doc.get("programname", "unknown")
            by_service[svc].append(doc.get("Payload", "")[:150])

        lines = [f"=== Résumé erreurs — {n_minutes} dernières minutes ({len(docs)} erreurs) ==="]
        for svc, payloads in sorted(by_service.items(), key=lambda x: -len(x[1])):
            lines.append(f"\n[{svc}] — {len(payloads)} erreur(s):")
            # Déduplique les messages similaires
            seen = set()
            for p in payloads[:5]:
                key = p[:60]
                if key not in seen:
                    seen.add(key)
                    lines.append(f"  • {p}")

        return "\n".join(lines)

    except Exception as e:
        return f"Erreur get_error_summary: {e}"


# ─────────────────────────────────────────────────────────────────────────────
# Liste complète des outils (Phase 1 + Phase 2)
# ─────────────────────────────────────────────────────────────────────────────

TOOLS = [
    # Phase 1 — OpenStack API
    list_instances,
    list_flavors,
    list_images,
    list_networks,
    create_instance,
    delete_instance,
    get_instance_metrics,
    # Phase 2 — OpenSearch logs
    search_logs,
    get_instance_logs,
    get_error_summary,
]


# ─────────────────────────────────────────────────────────────────────────────
# State LangGraph
# ─────────────────────────────────────────────────────────────────────────────

class AgentState(TypedDict):
    messages: Annotated[list, add_messages]


# ─────────────────────────────────────────────────────────────────────────────
# Prompt système — enrichi Phase 2 pour RCA
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Tu es un assistant AIOps pour infrastructure OpenStack. Réponds toujours en français.

RÈGLES FONDAMENTALES :
1. Utilise TOUJOURS un outil avant de répondre sur les instances, flavors, images, réseaux ou métriques.
2. Ne fabrique jamais de données. Tout ce que tu dis vient d'un outil.
3. Demande confirmation avant create_instance ou delete_instance.

OUTILS DISPONIBLES :
- list_instances, list_flavors, list_images, list_networks : inventaire OpenStack
- create_instance, delete_instance : actions (confirmation requise)
- get_instance_metrics : CPU/RAM via Prometheus
- search_logs : recherche dans les logs OpenSearch (query, service, level, minutes)
- get_instance_logs : logs d'une instance spécifique (erreurs et warnings)
- get_error_summary : résumé des erreurs récentes par service

QUAND DIAGNOSTIQUER (mots-clés : pourquoi, erreur, problème, ne démarre pas, SHUTOFF, lent) :
  Étape 1 → list_instances pour voir le statut actuel
  Étape 2 → get_instance_logs(nom) pour les erreurs liées à l'instance
  Étape 3 → get_instance_metrics(nom) pour CPU/RAM si l'instance tourne
  Étape 4 → search_logs si besoin d'approfondir un service spécifique
  Étape 5 → Synthèse : Symptôme / Cause probable / Recommandation

FORMAT DE RÉPONSE DIAGNOSTIC :
  🔍 Symptôme : ce que tu observes
  🔎 Analyse : ce que les logs et métriques révèlent
  ✅ Recommandation : action concrète à effectuer"""


# ─────────────────────────────────────────────────────────────────────────────
# Construction du graphe LangGraph
# ─────────────────────────────────────────────────────────────────────────────

def build_graph(llm):
    tool_node = ToolNode(tools=TOOLS)

    def agent_node(state: AgentState) -> AgentState:
        messages = [SystemMessage(content=SYSTEM_PROMPT)] + state["messages"]
        response = llm.invoke(messages)
        return {"messages": [response]}

    builder = StateGraph(AgentState)
    builder.add_node("agent", agent_node)
    builder.add_node("tools", tool_node)
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", tools_condition)
    builder.add_edge("tools", "agent")

    return builder


# ─────────────────────────────────────────────────────────────────────────────
# Point d'entrée
# ─────────────────────────────────────────────────────────────────────────────

def run_agent(
    message: str,
    history: list[dict],
    keystone_token: str,
    project_id: str,
    conversation_id: str,
    groq_api_key: str,
    stream: bool = False,
):
    global _SESSION
    _SESSION = {
        "keystone_token": keystone_token,
        "project_id": project_id,
    }

    llm = ChatGroq(
        model="llama-3.3-70b-versatile",
        api_key=groq_api_key,
        temperature=0.1,
        max_tokens=2048,
        timeout=30,
        max_retries=2,
    ).bind_tools(TOOLS)

    with SqliteSaver.from_conn_string(SQLITE_DB) as checkpointer:
        graph = build_graph(llm).compile(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": conversation_id}}

        lc_messages = []
        for msg in history[-4:]:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "user":
                lc_messages.append(HumanMessage(content=content))
            else:
                lc_messages.append(AIMessage(content=content))

        lc_messages.append(HumanMessage(content=sanitize(message)))
        input_state = {"messages": lc_messages}

        result = graph.invoke(
        input_state,
        config,
        {"recursion_limit": 6},   # max 3 tool calls
    )
        final = result["messages"][-1]
        return getattr(final, "content", str(final))