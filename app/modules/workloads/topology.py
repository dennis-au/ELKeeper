"""Credential-free topology and access-target rendering."""

from __future__ import annotations


def configured_access_urls(cluster: dict, valid_ipv4, role_ports: dict | None = None) -> list[dict]:
    urls = []
    members = {member["node_id"]: member for member in cluster["members"]}
    definitions = {
        "master": ("Elasticsearch API", "api", "https", "elasticsearch_http"),
        "hot": ("Elasticsearch API", "api", "https", "elasticsearch_http"),
        "warm": ("Elasticsearch API", "api", "https", "elasticsearch_http"),
        "ml": ("Elasticsearch API", "api", "https", "elasticsearch_http"),
        "ingest": ("Elasticsearch API", "api", "https", "elasticsearch_http"),
        "coordinating": ("Elasticsearch API", "api", "https", "elasticsearch_http"),
        "kibana": ("Kibana", "browser", "https", "kibana"),
        "fleet-server": ("Fleet Server", "api", "https", "fleet"),
        "logstash": ("Logstash API", "api", "http", "logstash_api"),
    }
    ports_by_role = role_ports or cluster["role_ports"]
    for assignment in cluster["assignments"]:
        definition = definitions.get(assignment["role"])
        if not definition or assignment["node_id"] not in members:
            continue
        label, audience, scheme, port_name = definition
        host = members[assignment["node_id"]].get("user_address")
        if not valid_ipv4(host):
            continue
        port = ports_by_role[assignment["role"]][port_name]
        urls.append({
            "assignment_id": assignment["id"], "role": assignment["role"], "label": label,
            "audience": audience, "host": host, "port": port,
            "url": f"{scheme}://{host}:{port}",
        })
    return sorted(urls, key=lambda item: (item["audience"] != "browser", item["label"], item["host"], item["port"]))


def render_topology(cluster: dict, role_specs: dict, es_roles: dict, valid_ipv4, width: int = 78) -> tuple[str, list[dict]]:
    access_urls = configured_access_urls(cluster, valid_ipv4)

    def fit(value, available):
        value = str(value)
        return value if len(value) <= available else value[: max(0, available - 3)] + "..."

    def outer(value):
        return "|" + fit(value, width).ljust(width) + "|"

    def role_box(lines, assignment, user_address, data_address, access):
        inner_width = width - 8
        config = assignment["config"]
        workload = f"ecp-{cluster['slug']}-{assignment['role']}-{assignment['node_id']}"
        details = [role_specs[assignment["role"]]["label"], f"Name     : {workload}"]
        if assignment["role"] in es_roles:
            ports = cluster["role_ports"][assignment["role"]]
            details += [
                f"Roles    : {es_roles[assignment['role']]}",
                f"HTTP     : https://{user_address or 'not configured'}:{ports['elasticsearch_http']}",
                f"Transport: {data_address or 'not configured'}:{ports['elasticsearch_transport']}/tcp (TLS)",
            ]
        elif assignment["role"] == "elastic-agent":
            details.append("Connection: outbound TLS")
        elif access:
            details.append(f"URL      : {access['url']}")
        details += [
            f"CPU      : {config.get('cpu', '?')} cores  Memory: {config.get('memory', '?')}",
            f"Storage  : {config.get('storage_path', '?')}",
        ]
        maintenance = assignment.get("maintenance")
        if isinstance(maintenance, dict):
            state = maintenance.get("lifecycle_state", "unknown")
            checkpoint = maintenance.get("checkpoint")
            recovery = checkpoint.get("recovery_classification") if isinstance(checkpoint, dict) else None
            details.append(f"Maintenance: {state}{f' ({recovery})' if recovery else ''}")
        lines.append("|  +" + "-" * (width - 6) + "+  |")
        for detail in details:
            lines.append("|  | " + fit(detail, inner_width).ljust(inner_width) + " |  |")
        lines.append("|  +" + "-" * (width - 6) + "+  |")

    def connector(lines, source, target, port):
        center = width // 2
        lines.extend([" " * center + "|", "  Elasticsearch transport", "  " + fit(f"{source} -> {target}:{port}/tcp (TLS)", width - 2), " " * center + "v"])

    lines = [f"Elastic Stack topology: {cluster['name']}"]
    if access_urls:
        lines += ["", "Configured user access:"]
        for access in access_urls:
            label = f"{access['label']} ({access['audience']})"
            lines.append(f"  {label:<29} {access['url']}")
    members = {member["node_id"]: member for member in cluster["members"]}
    grouped = {}
    for assignment in cluster["assignments"]:
        grouped.setdefault(assignment["node_id"], []).append(assignment)
    access_by_assignment = {access["assignment_id"]: access for access in access_urls}
    node_ids = [member["node_id"] for member in cluster["members"] if member["node_id"] in grouped]
    for index, node_id in enumerate(node_ids):
        assignments = grouped[node_id]
        member = members[node_id]
        lines += [
            "", "+" + "=" * width + "+", outer(f" HOST: {member['name']}"),
            outer(f" Zone    : {member['zone_id'] or 'not assigned'}"),
            outer(f" Network : {member['network_mode'] or 'dedicated'}"),
            outer(f" User NIC: {member.get('user_interface') or 'not configured'}  {member.get('user_address') or 'not configured'}"),
            outer(f" Data NIC: {member.get('data_interface') or 'not configured'}  {member.get('data_address') or 'not configured'}"),
            "+" + "=" * width + "+",
        ]
        for assignment in assignments:
            lines.append(outer(""))
            role_box(lines, assignment, member.get("user_address"), member.get("data_address"), access_by_assignment.get(assignment["id"]))
        lines += [outer(""), "+" + "=" * width + "+"]
        if index + 1 < len(node_ids):
            next_assignments = grouped[node_ids[index + 1]]
            if (member.get("data_address") and members[node_ids[index + 1]].get("data_address") and any(item["role"] in es_roles for item in assignments) and any(item["role"] in es_roles for item in next_assignments)):
                target = next(item for item in next_assignments if item["role"] in es_roles)
                connector(lines, member["data_address"], members[node_ids[index + 1]]["data_address"], cluster["role_ports"][target["role"]]["elasticsearch_transport"])
    if not grouped:
        lines.append("No managed workloads are assigned.")
    return "\n".join(lines) + "\n", access_urls
