import os
#!/opt/crewai-venv/bin/python
# CrewAI runner — tourne dans le venv isolé /opt/crewai-venv
import sys
import json
import yaml


def get_os_connection():
    import openstack
    with open("/etc/skyline/skyline.yaml") as f:
        cfg = yaml.safe_load(f)
    os_cfg = cfg["openstack"]
    return openstack.connect(
        auth_url=os_cfg["keystone_url"],
        username=os_cfg["system_user_name"],
        password=os_cfg["system_user_password"],
        project_name=os_cfg["system_project"],
        user_domain_name=os_cfg["system_user_domain"],
        project_domain_name=os_cfg["system_project_domain"],
    )


from crewai.tools import tool


@tool("list_instances")
def list_instances(query: str = "all") -> str:
    """Liste toutes les instances OpenStack avec statut et IP."""
    try:
        conn = get_os_connection()
        servers = list(conn.compute.servers(all_projects=True))
        if not servers:
            return "Aucune instance trouvée."
        result = []
        for s in servers:
            ips = [addr["addr"] for net in s.addresses.values() for addr in net]
            result.append(f"- {s.name} | statut: {s.status} | IP: {', '.join(ips) or 'N/A'} | ID: {s.id}")
        return "\n".join(result)
    except Exception as e:
        return f"Erreur: {e}"


@tool("list_flavors")
def list_flavors(query: str = "all") -> str:
    """Liste les flavors disponibles (vCPUs, RAM, disk)."""
    try:
        conn = get_os_connection()
        result = [f"- {f.name}: {f.vcpus} vCPUs, {f.ram}MB RAM, {f.disk}GB disk"
                  for f in conn.compute.flavors()]
        return "\n".join(result)
    except Exception as e:
        return f"Erreur: {e}"


@tool("list_images")
def list_images(query: str = "all") -> str:
    """Liste les images disponibles."""
    try:
        conn = get_os_connection()
        result = [f"- {img.name} | ID: {img.id} | statut: {img.status}"
                  for img in conn.image.images()]
        return "\n".join(result)
    except Exception as e:
        return f"Erreur: {e}"


@tool("list_networks")
def list_networks(query: str = "all") -> str:
    """Liste les réseaux disponibles."""
    try:
        conn = get_os_connection()
        result = [f"- {n.name} | ID: {n.id} | statut: {n.status}"
                  for n in conn.network.networks()]
        return "\n".join(result)
    except Exception as e:
        return f"Erreur: {e}"


@tool("create_instance")
def create_instance(params: str) -> str:
    """
    Crée une instance. params format: name=X,flavor=Y,image=Z,network=W
    Exemple: name=web-4,flavor=m1.small,image=cirros,network=internal
    """
    try:
        conn = get_os_connection()
        p = dict(item.split("=") for item in params.split(","))
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
    except Exception as e:
        return f"Erreur création: {e}"


@tool("delete_instance")
def delete_instance(name_or_id: str) -> str:
    """Supprime une instance par son nom ou ID."""
    try:
        conn = get_os_connection()
        server = conn.compute.find_server(name_or_id)
        if not server:
            return f"Instance '{name_or_id}' introuvable."
        conn.compute.delete_server(server.id)
        return f"Instance '{name_or_id}' supprimée avec succès."
    except Exception as e:
        return f"Erreur suppression: {e}"


@tool("get_instance_metrics")
def get_instance_metrics(name_or_id: str) -> str:
    """Retourne les métriques CPU/RAM d'une instance."""
    try:
        import httpx
        conn = get_os_connection()
        server = conn.compute.find_server(name_or_id)
        if not server:
            return f"Instance '{name_or_id}' introuvable."

        domain = getattr(server, "OS-EXT-SRV-ATTR:instance_name", None)
        if not domain:
            return "Impossible de récupérer le nom libvirt."

        with open("/etc/skyline/skyline.yaml") as f:
            cfg = yaml.safe_load(f)
        prom_url  = cfg["default"]["prometheus_endpoint"]
        prom_user = cfg["default"]["prometheus_basic_auth_user"]
        prom_pass = cfg["default"]["prometheus_basic_auth_password"]
        auth = (prom_user, prom_pass)

        def query(q):
            r = httpx.get(f"{prom_url}/api/v1/query", params={"query": q}, auth=auth, timeout=10)
            results = r.json().get("data", {}).get("result", [])
            return float(results[0]["value"][1]) if results else 0.0

        cpu    = query(f'rate(libvirt_domain_info_cpu_time_seconds_total{{domain="{domain}"}}[5m]) * 100')
        mem_mb = query(f'libvirt_domain_info_memory_usage_bytes{{domain="{domain}"}}') / 1024 / 1024

        return f"Instance '{server.name}':\n  CPU: {cpu:.1f}%\n  RAM: {mem_mb:.0f} MB\n  Statut: {server.status}"
    except Exception as e:
        return f"Erreur métriques: {e}"


def main():
    input_file  = sys.argv[1]
    output_file = sys.argv[2]

    with open(input_file) as f:
        payload = json.load(f)

    message = payload.get("message", "")
    history = payload.get("history", [])

    GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

    from crewai import Agent, Task, Crew, LLM

    llm = LLM(
        model="groq/llama-3.3-70b-versatile",
        api_key=GROQ_API_KEY,
        temperature=0.1,
    )

    agent = Agent(
        role="Assistant OpenStack",
        goal="Aider l'utilisateur à gérer son infrastructure OpenStack via langage naturel.",
        backstory=(
            "You are an OpenStack expert assistant. "
            "You MUST use the available tools to answer questions. "
            "Always call tools directly without explaining what you will do. "
            "IMPORTANT: When a user wants to CREATE an instance, NEVER create immediately. "
            "First call list_flavors, list_images, list_networks, then ask the user to confirm "
            "name, flavor, image, and network. Only create when all info is provided. "
            "When a user wants to DELETE, always ask for confirmation first. "
            "Respond in French after getting tool results."
        ),
        tools=[list_instances, list_flavors, list_images, list_networks,
               create_instance, delete_instance, get_instance_metrics],
        llm=llm,
        verbose=False,
    )

    context = ""
    for msg in history[-6:]:
        role = "Utilisateur" if msg.get("role") == "user" else "Assistant"
        context += f"{role}: {msg.get('content', '')}\n"

    task = Task(
        description=f"Conversation history:\n{context}\nUser request: {message}\nFollow rules: ask before creating/deleting.",
        expected_output="A conversational response in French based on tool results.",
        agent=agent,
    )

    crew = Crew(agents=[agent], tasks=[task], verbose=False)
    result = crew.kickoff()

    with open(output_file, "w") as f:
        json.dump({"response": str(result), "status": "success"}, f)


if __name__ == "__main__":
    main()