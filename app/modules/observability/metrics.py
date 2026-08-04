"""Pure telemetry parsing and dashboard aggregation helpers."""

from __future__ import annotations

from datetime import datetime


HOST_RESOURCE_COUNTERS = {
    "cpu_total", "cpu_idle", "memory_total_bytes", "memory_available_bytes",
    "network_rx_bytes", "network_tx_bytes", "disk_read_bytes", "disk_write_bytes",
}


def container_name(item):
    names = item.get("Names") or item.get("names") or []
    if isinstance(names, str):
        names = [names]
    return (names[0] if names else item.get("Name") or item.get("name") or "").lstrip("/")


def container_stats(item):
    cpu = item.get("cpu_stats", {})
    previous = item.get("precpu_stats", {})
    cpu_delta = cpu.get("cpu_usage", {}).get("total_usage", 0) - previous.get("cpu_usage", {}).get("total_usage", 0)
    system_delta = cpu.get("system_cpu_usage", 0) - previous.get("system_cpu_usage", 0)
    online = cpu.get("online_cpus") or len(cpu.get("cpu_usage", {}).get("percpu_usage") or []) or 1
    cpu_percent = (cpu_delta / system_delta * online * 100) if cpu_delta > 0 and system_delta > 0 else 0
    memory = item.get("memory_stats", {})
    networks = item.get("networks", {}) or {}
    return {
        "cpu_percent": round(cpu_percent, 2),
        "memory_usage": memory.get("usage", 0),
        "memory_limit": memory.get("limit", 0),
        "network_rx": sum(value.get("rx_bytes", 0) for value in networks.values()),
        "network_tx": sum(value.get("tx_bytes", 0) for value in networks.values()),
    }


def parse_host_resource_counters(output):
    counters = {}
    for line in output.decode(errors="replace").splitlines():
        key, separator, value = line.partition("=")
        if not separator or key not in HOST_RESOURCE_COUNTERS:
            continue
        try:
            counters[key] = int(value.strip())
        except ValueError:
            continue
    missing = HOST_RESOURCE_COUNTERS - counters.keys()
    if missing or any(value < 0 for value in counters.values()):
        raise RuntimeError("Host resource counters are incomplete")
    return counters


def host_resource_rates(previous, current):
    sample = {
        "observed_at": current["observed_at"],
        "cpu_percent": None,
        "memory_usage_bytes": max(current["memory_total_bytes"] - current["memory_available_bytes"], 0),
        "memory_total_bytes": current["memory_total_bytes"],
        "network_rx_bytes_per_second": None,
        "network_tx_bytes_per_second": None,
        "disk_read_bytes_per_second": None,
        "disk_write_bytes_per_second": None,
    }
    if not previous:
        return sample
    try:
        elapsed = datetime.fromisoformat(current["observed_at"].replace("Z", "+00:00")).timestamp() - datetime.fromisoformat(previous["observed_at"].replace("Z", "+00:00")).timestamp()
    except (KeyError, TypeError, ValueError):
        return sample
    if elapsed <= 0:
        return sample
    total_delta = current["cpu_total"] - previous["cpu_total"]
    idle_delta = current["cpu_idle"] - previous["cpu_idle"]
    if total_delta > 0 and 0 <= idle_delta <= total_delta:
        sample["cpu_percent"] = round((total_delta - idle_delta) / total_delta * 100, 2)
    for counter, field in (
        ("network_rx_bytes", "network_rx_bytes_per_second"),
        ("network_tx_bytes", "network_tx_bytes_per_second"),
        ("disk_read_bytes", "disk_read_bytes_per_second"),
        ("disk_write_bytes", "disk_write_bytes_per_second"),
    ):
        delta = current[counter] - previous[counter]
        if delta >= 0:
            sample[field] = round(delta / elapsed, 2)
    return sample


NODE_TYPE_ORDER = {
    "Hot data": 0, "Warm data": 1, "Cold data": 2, "Frozen data": 3,
    "Content data": 4, "Data": 5, "Master": 6, "Machine learning": 7,
    "Ingest": 8, "Coordinating": 9, "Other": 10,
}


def elastic_node_type(roles):
    role_set = set(roles)
    for role, label in (
        ("data_hot", "Hot data"), ("data_warm", "Warm data"),
        ("data_cold", "Cold data"), ("data_frozen", "Frozen data"),
        ("data_content", "Content data"), ("data", "Data"),
        ("master", "Master"), ("ml", "Machine learning"),
        ("ingest", "Ingest"),
    ):
        if role in role_set:
            return label
    return "Coordinating" if roles else "Other"


def node_breakdown(node_stats, allocation):
    shard_counts = {}
    for item in allocation if isinstance(allocation, list) else []:
        name = str(item.get("node") or "").strip()
        if not name:
            continue
        try:
            shard_counts[name] = int(str(item.get("shards", 0)))
        except (TypeError, ValueError):
            shard_counts[name] = 0
    breakdown = []
    for node_id, node in node_stats.items():
        name = str(node.get("name") or node_id)
        roles = [str(role) for role in node.get("roles", [])]
        fs = node.get("fs", {}).get("total", {})
        jvm = node.get("jvm", {}).get("mem", {})
        total = fs.get("total_in_bytes", 0) or 0
        available = fs.get("available_in_bytes", 0) or 0
        breakdown.append({
            "id": node_id,
            "name": name,
            "node_type": elastic_node_type(roles),
            "roles": roles,
            "zone": str(node.get("attributes", {}).get("zone") or ""),
            "shards": shard_counts.get(name, 0),
            "disk_total_bytes": total,
            "disk_available_bytes": available,
            "disk_used_bytes": max(total - available, 0),
            "heap_used_bytes": jvm.get("heap_used_in_bytes", 0) or 0,
            "heap_max_bytes": jvm.get("heap_max_in_bytes", 0) or 0,
        })
    return sorted(breakdown, key=lambda item: (NODE_TYPE_ORDER[item["node_type"]], item["name"].lower()))


def zone_breakdown(nodes):
    zones = {}
    for node in nodes:
        zone = node.get("zone") or "unassigned"
        aggregate = zones.setdefault(zone, {
            "zone": zone, "nodes": 0, "shards": 0,
            "disk_total_bytes": 0, "disk_available_bytes": 0, "disk_used_bytes": 0,
            "heap_used_bytes": 0, "heap_max_bytes": 0,
        })
        aggregate["nodes"] += 1
        for field in ("shards", "disk_total_bytes", "disk_available_bytes", "disk_used_bytes", "heap_used_bytes", "heap_max_bytes"):
            aggregate[field] += int(node.get(field) or 0)
    return sorted(zones.values(), key=lambda item: (item["zone"] == "unassigned", item["zone"]))


__all__ = [
    "HOST_RESOURCE_COUNTERS", "NODE_TYPE_ORDER", "container_name", "container_stats",
    "parse_host_resource_counters", "host_resource_rates", "elastic_node_type",
    "node_breakdown", "zone_breakdown",
]
