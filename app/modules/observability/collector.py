"""Observability-owned runtime telemetry collector.

The collector is dependency-injected so the application assembly does not leak
into the observability module. Adapters and repositories are supplied through
this public contract.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import datetime as _datetime
import json
import random
import secrets
import os
import time
from typing import Any, Callable

import httpx


@dataclass(frozen=True)
class TelemetryDependencies:
    db_factory: Callable[[], Any]
    workload_name: Callable[[dict, dict], str]
    image_version: Callable[[str], str]
    open_config: Callable[[str], dict]
    seal_config: Callable[[str], str]
    cluster_record: Callable[[Any, int], dict | None]
    runtime: Any
    ca_cache: Any
    fast_collect_interval: int
    slow_collect_interval: int
    cluster_collect_interval: int
    max_fast_host_probes: int
    max_slow_host_probes: int
    poll_jitter_seconds: float
    host_resource_history_seconds: int
    cluster_repository: Any
    host_repository: Any
    observability_repository: Any
    version_repository: Any
    workload_repository: Any
    podman_tunnel_cls: Any
    ssh_pool: Any
    remote_command: Callable[..., Any]
    host_identity: Callable[..., Any]
    host_network_interfaces: Callable[..., Any]
    host_resource_counters: Callable[..., Any]
    ssh_error_summary: Callable[[Exception], str]
    container_name: Callable[[dict], str]
    container_stats: Callable[[dict], dict]
    host_resource_rates: Callable[[dict | None, dict], dict]
    node_breakdown: Callable[[dict, list], list]
    zone_breakdown: Callable[[list], list]
    ca_ssl_context: Callable[[Any], Any]
    cluster_ca_path: Callable[[Any, int], Any]
    invalidate_cluster_ca: Callable[[int], Any]
    utc_now: Callable[[], str]
    cluster_awaits_data_role: Callable[[dict], bool]


datetime = _datetime.datetime
class TelemetryManager:
    def __init__(self, dependencies):
        self._deps = dependencies
        self.host_states = {}
        self.host_counters = {}
        self.host_history = {}
        self.cluster_states = {}
        self.history = {}
        self.tunnels = {}
        self.subscribers = set()
        self.task = None
        self.slow_task = None
        self.cluster_task = None

    async def start(self):
        self._deps.runtime.mkdir(parents=True, exist_ok=True)
        self._deps.ca_cache.mkdir(parents=True, exist_ok=True)
        if not self.task:
            self.task = asyncio.create_task(self._fast_loop())
            self.slow_task = asyncio.create_task(self._slow_loop())
            self.cluster_task = asyncio.create_task(self._cluster_loop())

    async def stop(self):
        tasks = [task for task in (self.task, self.slow_task, self.cluster_task) if task]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.task = self.slow_task = self.cluster_task = None
        await asyncio.gather(*(tunnel.close() for tunnel in self.tunnels.values()), return_exceptions=True)
        self.tunnels.clear()
        await self._deps.ssh_pool.close()

    async def _sleep_until_next(self, started, interval):
        remaining = max(0.0, interval - (asyncio.get_running_loop().time() - started))
        await asyncio.sleep(remaining + random.uniform(0, self._deps.poll_jitter_seconds))

    async def _fast_loop(self):
        while True:
            started = asyncio.get_running_loop().time()
            try:
                await self._collect_fast_hosts()
            except Exception:
                pass
            await self._sleep_until_next(started, self._deps.fast_collect_interval)

    async def _slow_loop(self):
        while True:
            started = asyncio.get_running_loop().time()
            try:
                await self._collect_slow_hosts()
            except Exception:
                pass
            await self._sleep_until_next(started, self._deps.slow_collect_interval)

    async def _cluster_loop(self):
        while True:
            started = asyncio.get_running_loop().time()
            try:
                await self._collect_clusters()
            except Exception:
                pass
            await self._sleep_until_next(started, self._deps.cluster_collect_interval)

    async def collect_once(self):
        nodes = self._deps.host_repository(self._deps.db_factory).list_enabled()
        cluster_ids = self._deps.cluster_repository(self._deps.db_factory).ids()
        await asyncio.gather(
            self._collect_fast_hosts(nodes),
            self._collect_slow_hosts(nodes),
            self._collect_clusters(cluster_ids),
        )

    async def _collect_fast_hosts(self, nodes=None):
        if nodes is None:
            nodes = self._deps.host_repository(self._deps.db_factory).list_enabled()
        semaphore = asyncio.Semaphore(self._deps.max_fast_host_probes)

        async def collect(node):
            async with semaphore:
                try:
                    await self._collect_host_fast(node)
                except Exception as error:
                    state = self.host_states.get(node["id"], {})
                    state["resource_observation_error"] = f"Resource telemetry: {error}"[:300]
                    self.host_states[node["id"]] = state

        await asyncio.gather(*(collect(node) for node in nodes))

    async def _collect_slow_hosts(self, nodes=None):
        if nodes is None:
            nodes = self._deps.host_repository(self._deps.db_factory).list_enabled()
        semaphore = asyncio.Semaphore(self._deps.max_slow_host_probes)

        async def collect(node):
            async with semaphore:
                try:
                    await self._collect_host(node)
                except Exception:
                    pass

        await asyncio.gather(*(collect(node) for node in nodes))

    async def _collect_clusters(self, cluster_ids=None):
        if cluster_ids is None:
            cluster_ids = self._deps.cluster_repository(self._deps.db_factory).ids()
        await asyncio.gather(*(self._collect_cluster(cluster_id) for cluster_id in cluster_ids))

    async def publish(self, event, payload):
        message = {"event": event, "data": payload, "id": f"{int(time.time() * 1000)}-{secrets.token_hex(2)}"}
        for queue in tuple(self.subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(message)

    def subscribe(self):
        queue = asyncio.Queue(maxsize=100)
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue):
        self.subscribers.discard(queue)

    async def _podman_get(self, client, path, version=""):
        candidates = [path]
        if version:
            candidates.append(f"/v{version}{path}")
        last = None
        for candidate in candidates:
            response = await client.get("http://podman" + candidate)
            if response.status_code < 400:
                return response.json()
            last = response
        raise RuntimeError(f"Podman API returned HTTP {last.status_code if last else 'error'}")

    def _record_host_resource_sample(self, node_id, observed_at, counters):
        current = {**counters, "observed_at": observed_at}
        sample = self._deps.host_resource_rates(self.host_counters.get(node_id), current)
        self.host_counters[node_id] = current
        history = self.host_history.setdefault(node_id, [])
        history.append(sample)
        cutoff = time.time() - self._deps.host_resource_history_seconds
        self.host_history[node_id] = [
            item for item in history
            if datetime.fromisoformat(item["observed_at"].replace("Z", "+00:00")).timestamp() >= cutoff
        ][-(self._deps.host_resource_history_seconds // max(self._deps.fast_collect_interval, 1) + 2):]
        return sample

    def _record_workload_runtime(self, node_id, containers):
        by_name = {item["name"]: item for item in containers}
        assignments = self._deps.workload_repository(self._deps.db_factory).active_for_node(node_id)
        clusters = self._deps.cluster_repository(self._deps.db_factory)
        versions = self._deps.version_repository(self._deps.db_factory)
        for assignment in assignments:
            name = self._deps.workload_name({"slug": clusters.slug(assignment["cluster_id"])}, assignment)
            container = by_name.get(name)
            image = container.get("image", "") if container else ""
            digest = container.get("digest", "") if container else ""
            version = self._deps.image_version(image)
            state = str(container.get("state", "")).lower() if container else ""
            status = str(container.get("status", "")).lower() if container else ""
            running = state == "running" or status.startswith("up ")
            error = "" if running and version else (
                "Managed workload container not found" if not container else
                "Runtime image does not report a release tag" if not version else
                "Managed workload container is not running"
            )
            versions.record_runtime(
                assignment["id"],
                image=image,
                digest=digest,
                version=version,
                running=running,
                cached=bool(container),
                error=error,
            )

    def _record_cluster_zoning(self, cluster, breakdown):
        elastic_roles = {"master", "hot", "warm", "ml", "ingest", "coordinating"}
        runtime_zones = {item["name"]: item.get("zone") or "" for item in breakdown}
        members = {member["node_id"]: member for member in cluster["members"]}
        observed = {}
        drift = []
        for assignment in cluster["assignments"]:
            if assignment["role"] not in elastic_roles:
                continue
            runtime_zone = runtime_zones.get(self._deps.workload_name(cluster, assignment), "")
            observed[str(assignment["id"])] = runtime_zone
            expected_zone = members.get(assignment["node_id"], {}).get("zone_id") or ""
            if cluster["zoning"]["mode"] != "disabled" and runtime_zone != expected_zone:
                drift.append(
                    f"{assignment['role']} on {assignment['node_name']} expected {expected_zone or 'no zone'} "
                    f"but reports {runtime_zone or 'no zone'}"
                )
        clusters = self._deps.cluster_repository(self._deps.db_factory)
        current = clusters.zoning_observation(cluster["id"])
        applied_mode = current["applied_mode"] if current else "disabled"
        applied_zones = current["applied_zones"] if current else []
        desired = cluster["zoning"]
        if applied_mode != desired["mode"] or applied_zones != desired["zones"]:
            status = "pending"
            last_error = "Desired zoning configuration has not been applied"
        elif desired["mode"] == "disabled":
            runtime_with_zone = [zone for zone in observed.values() if zone]
            status = "drift" if runtime_with_zone else "disabled"
            last_error = "Zone drift: runtime node attributes remain after zoning was disabled" if runtime_with_zone else ""
        elif drift:
            status = "drift"
            last_error = "Zone drift: " + "; ".join(drift[:4])
        else:
            status = "applied"
            last_error = ""
        clusters.record_runtime_zoning(
            cluster["id"],
            applied_mode=applied_mode,
            applied_zones=applied_zones,
            observed_zones=observed,
            status=status,
            last_error=last_error,
        )

    async def _collect_host_fast(self, node):
        state = self.host_states.get(node["id"])
        if state is None:
            observed = self._deps.observability_repository(self._deps.db_factory).runtime_observation(node["id"])
            if observed:
                state = {
                    "node_id": node["id"], "initialized": bool(observed["initialized"]),
                    "reachable": bool(observed["reachable"]), "podman_socket_active": bool(observed["podman_socket_active"]),
                    "os_name": observed["os_name"], "podman_version": observed["podman_version"],
                    "observed_at": observed["observed_at"], "last_error": observed["last_error"],
                    "network_interfaces": json.loads(observed["network_interfaces_json"] or "{}"),
                    "containers": [], "pods": [],
                }
                self.host_states[node["id"]] = state
        if not state or not state.get("reachable") or not state.get("initialized"):
            return

        observed_at = self._deps.utc_now()
        try:
            sample = self._record_host_resource_sample(node["id"], observed_at, await self._deps.host_resource_counters(node))
        except Exception as error:
            current = self.host_states.get(node["id"], state)
            current["resource_observation_error"] = f"Resource telemetry: {error}"[:300]
            self.host_states[node["id"]] = current
            await self.publish("host_stats", {"node_id": node["id"], "observed_at": observed_at, "sample": None})
            return
        current = self.host_states.get(node["id"], state)
        current["resource_observed_at"] = observed_at
        current["resource_observation_error"] = ""
        self.host_states[node["id"]] = current
        await self.publish("host_stats", {"node_id": node["id"], "observed_at": observed_at, "sample": sample})

    async def _collect_host(self, node):
        observed = self._deps.utc_now()
        tunnel = self.tunnels.get(node["id"])
        try:
            os_name, installed_podman = await self._deps.host_identity(node)
            marker = await self._deps.remote_command(
                node,
                "if test -f /etc/elastic-control-host-init; then printf initialized; else printf uninitialized; fi",
            )
            initialized = marker.decode(errors="replace").strip() == "initialized"
        except Exception as error:
            if tunnel:
                await tunnel.close()
            state = {
                "node_id": node["id"], "reachable": False, "initialized": False, "podman_socket_active": False,
                "os_name": "", "podman_version": "", "observed_at": observed, "last_error": f"SSH: {self._deps.ssh_error_summary(error)}", "containers": [], "pods": [],
            }
        else:
            try:
                network_interfaces = await self._deps.host_network_interfaces(node)
                network_observation_error = ""
            except Exception as error:
                network_interfaces = {}
                network_observation_error = f"Network inventory: {error}"[:300]
            if not initialized:
                if tunnel:
                    await tunnel.close()
                state = {
                    "node_id": node["id"], "reachable": True, "initialized": False, "podman_socket_active": False,
                    "os_name": os_name, "podman_version": installed_podman, "observed_at": observed, "last_error": "", "containers": [], "pods": [],
                    "network_interfaces": network_interfaces, "network_observation_error": network_observation_error,
                }
            else:
                try:
                    tunnel = self.tunnels.setdefault(node["id"], self._deps.podman_tunnel_cls(node["id"]))
                    socket_path = await tunnel.ensure(node)
                    transport = httpx.AsyncHTTPTransport(uds=str(socket_path))
                    async with httpx.AsyncClient(transport=transport, timeout=4) as client:
                        version_data = await self._podman_get(client, "/version")
                        api_version = version_data.get("ApiVersion") or version_data.get("APIVersion") or ""
                        containers = await self._podman_get(client, "/containers/json?all=true", api_version)
                        managed = []
                        for item in containers:
                            name = self._deps.container_name(item)
                            if not name.startswith("ecp-"):
                                continue
                            stats = {}
                            try:
                                stats = self._deps.container_stats(await self._podman_get(client, f"/containers/{item.get('Id') or item.get('ID')}/stats?stream=false", api_version))
                            except Exception:
                                pass
                            managed.append({
                                "id": item.get("Id") or item.get("ID"), "name": name,
                                "image": item.get("Image") or item.get("ImageName") or "",
                                "digest": item.get("ImageID") or item.get("ImageId") or "",
                                "state": item.get("State") or item.get("Status") or "unknown",
                                "status": item.get("Status") or "", "labels": item.get("Labels") or {}, **stats,
                            })
                        try:
                            pods = await self._podman_get(client, "/libpod/pods/json", api_version)
                        except Exception:
                            pods = []
                    version = version_data.get("Version") or version_data.get("version") or installed_podman
                    state = {
                        "node_id": node["id"], "reachable": True, "initialized": True, "podman_socket_active": True,
                        "os_name": os_name, "podman_version": version, "observed_at": observed, "last_error": "", "containers": managed, "pods": pods,
                        "network_interfaces": network_interfaces, "network_observation_error": network_observation_error,
                    }
                except Exception as error:
                    if tunnel:
                        await tunnel.close()
                    state = {
                        "node_id": node["id"], "reachable": True, "initialized": True, "podman_socket_active": False,
                        "os_name": os_name, "podman_version": installed_podman, "observed_at": observed, "last_error": f"Podman: {error}"[:300], "containers": [], "pods": [],
                        "network_interfaces": network_interfaces, "network_observation_error": network_observation_error,
                    }
        previous = self.host_states.get(node["id"], {})
        state["resource_observation_error"] = previous.get("resource_observation_error", "")
        state["resource_observed_at"] = previous.get("resource_observed_at")
        self.host_states[node["id"]] = state
        self._deps.observability_repository(self._deps.db_factory).record_host_runtime(
            node["id"], state, observed_at=observed
        )
        if state["podman_socket_active"]:
            self._record_workload_runtime(node["id"], state["containers"])
        await self.publish("host_stats", {"node_id": node["id"], "observed_at": observed, "kind": "inventory"})

    async def _ensure_cluster_ca(self, cluster, master, node):
        path = self._deps.cluster_ca_path(self._deps.ca_cache, cluster["id"])
        if path.exists() and path.stat().st_size:
            return path
        remote = f"/etc/elastic-control/clusters/{cluster['slug']}/workloads/ecp-{cluster['slug']}-master-{master['node_id']}/certs/ca.crt"
        content = await self._deps.remote_command(node, "cat", remote)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(content)
        os.chmod(temporary, 0o644)
        temporary.replace(path)
        return path

    async def _monitoring_key(self, cluster, client):
        clusters = self._deps.cluster_repository(self._deps.db_factory)
        credentials = self._deps.open_config(clusters.secrets_json(cluster["id"]))
        if credentials.get("monitoring_api_key"):
            return credentials["monitoring_api_key"], True
        response = await client.post(
            "/_security/api_key",
            auth=("elastic", credentials["elastic_password"]),
            json={
                "name": f"elastic-control-dashboard-{cluster['id']}",
                "role_descriptors": {
                    "elastic-control-monitor": {
                        "cluster": ["monitor"],
                        "indices": [{"names": ["*"], "privileges": ["monitor", "view_index_metadata"]}],
                    }
                },
            },
        )
        response.raise_for_status()
        payload = response.json()
        encoded = base64.b64encode(f"{payload['id']}:{payload['api_key']}".encode()).decode()
        credentials["monitoring_api_key"] = encoded
        clusters.replace_secrets_json(cluster["id"], self._deps.seal_config(json.dumps(credentials)))
        return encoded, False

    def _clear_monitoring_key(self, cluster_id):
        clusters = self._deps.cluster_repository(self._deps.db_factory)
        credentials = self._deps.open_config(clusters.secrets_json(cluster_id))
        if "monitoring_api_key" not in credentials:
            return
        credentials.pop("monitoring_api_key")
        clusters.replace_secrets_json(cluster_id, self._deps.seal_config(json.dumps(credentials)))

    async def _collect_cluster(self, cluster_id):
        with self._deps.db_factory() as con:
            cluster = self._deps.cluster_record(con, cluster_id)
            master = next((item for item in cluster["assignments"] if item["role"] == "master"), None)
            if not master:
                self.cluster_states[cluster_id] = {"cluster_id": cluster_id, "status": "unknown", "observed_at": self._deps.utc_now(), "last_error": "No master is assigned"}
                return
            member = next(item for item in cluster["members"] if item["node_id"] == master["node_id"])
        node = self._deps.host_repository(self._deps.db_factory).get(master["node_id"])
        if not node:
            self.cluster_states[cluster_id] = {"cluster_id": cluster_id, "status": "unknown", "observed_at": self._deps.utc_now(), "last_error": "Master host is no longer in inventory"}
            return
        observed = self._deps.utc_now()
        if self._deps.cluster_awaits_data_role(cluster):
            state = {
                "cluster_id": cluster_id, "status": "awaiting_data", "observed_at": observed, "last_error": "",
                "nodes": sum(item["role"] in {"master", "hot", "warm", "ml", "ingest", "coordinating"} for item in cluster["assignments"]),
                "data_nodes": 0, "active_primary_shards": 0, "active_shards": 0,
                "unassigned_shards": 0, "unassigned_primary_shards": 0, "indices": 0,
                "documents": 0, "store_bytes": 0, "disk_total_bytes": 0,
                "disk_available_bytes": 0, "heap_used_bytes": 0, "heap_max_bytes": 0,
                "pending_tasks": 0, "index_health": {}, "node_breakdown": [], "effective_settings": {},
            }
            self.cluster_states[cluster_id] = state
            self.history.setdefault(cluster_id, []).append(state)
            cutoff = time.time() - 900
            self.history[cluster_id] = [item for item in self.history[cluster_id] if datetime.fromisoformat(item["observed_at"].replace("Z", "+00:00")).timestamp() >= cutoff]
            await self.publish("cluster_metrics", state)
            return
        try:
            ca_path = await self._ensure_cluster_ca(cluster, master, node)
            base = f"https://{member['user_address']}:{cluster['role_ports']['master']['elasticsearch_http']}"
            async with httpx.AsyncClient(base_url=base, verify=self._deps.ca_ssl_context(ca_path), timeout=5) as client:
                async def collect_metrics(key):
                    headers = {"Authorization": "ApiKey " + key}

                    async def get(path):
                        response = await client.get(path, headers=headers)
                        response.raise_for_status()
                        return response.json()

                    health, stats, nodes, pending, settings = await asyncio.gather(
                        get("/_cluster/health?level=indices"),
                        get("/_cluster/stats"),
                        get("/_nodes/stats/fs,jvm,process,os"),
                        get("/_cluster/pending_tasks"),
                        get("/_cluster/settings?include_defaults=true&flat_settings=true"),
                    )
                    try:
                        allocation = await get("/_cat/allocation?format=json&bytes=b&h=node,shards")
                    except httpx.HTTPError:
                        allocation = []
                    try:
                        shutdown = await get("/_nodes/shutdown")
                    except httpx.HTTPError:
                        shutdown = None
                    return health, stats, nodes, pending, settings, allocation, shutdown

                key, cached_key = await self._monitoring_key(cluster, client)
                try:
                    health, stats, nodes, pending, settings, allocation, shutdown = await collect_metrics(key)
                except httpx.HTTPStatusError as error:
                    if error.response.status_code != 401 or not cached_key:
                        raise
                    self._clear_monitoring_key(cluster_id)
                    key, _ = await self._monitoring_key(cluster, client)
                    health, stats, nodes, pending, settings, allocation, shutdown = await collect_metrics(key)
            node_stats = nodes.get("nodes", {})
            fs_total = sum(item.get("fs", {}).get("total", {}).get("total_in_bytes", 0) for item in node_stats.values())
            fs_available = sum(item.get("fs", {}).get("total", {}).get("available_in_bytes", 0) for item in node_stats.values())
            heap_used = sum(item.get("jvm", {}).get("mem", {}).get("heap_used_in_bytes", 0) for item in node_stats.values())
            heap_max = sum(item.get("jvm", {}).get("mem", {}).get("heap_max_in_bytes", 0) for item in node_stats.values())
            breakdown = self._deps.node_breakdown(node_stats, allocation)
            state = {
                "cluster_id": cluster_id,
                "cluster_name": health.get("cluster_name", ""),
                "cluster_uuid": health.get("cluster_uuid", ""),
                "status": health.get("status", "unknown"), "observed_at": observed, "last_error": "",
                "nodes": health.get("number_of_nodes", 0), "data_nodes": health.get("number_of_data_nodes", 0),
                "active_primary_shards": health.get("active_primary_shards", 0), "active_shards": health.get("active_shards", 0),
                "initializing_shards": health.get("initializing_shards", 0), "relocating_shards": health.get("relocating_shards", 0),
                "unassigned_shards": health.get("unassigned_shards", 0), "unassigned_primary_shards": health.get("unassigned_primary_shards", 0),
                "indices": stats.get("indices", {}).get("count", 0), "documents": stats.get("indices", {}).get("docs", {}).get("count", 0),
                "store_bytes": stats.get("indices", {}).get("store", {}).get("size_in_bytes", 0),
                "disk_total_bytes": fs_total, "disk_available_bytes": fs_available,
                "heap_used_bytes": heap_used, "heap_max_bytes": heap_max,
                "pending_tasks": len(pending.get("tasks", [])), "index_health": health.get("indices", {}),
                "node_breakdown": breakdown,
                "zone_breakdown": self._deps.zone_breakdown(breakdown),
                "effective_settings": settings,
                "stale_shutdown_record": True if shutdown is None else bool(shutdown.get("nodes", [])),
            }
            provider = cluster.get("provider") or {}
            if (
                not provider.get("expected_cluster_uuid")
                and provider.get("provider_type") == "native_podman"
                and provider.get("ownership_state") == "verified"
                and state["cluster_name"] == cluster["name"]
                and state["cluster_uuid"]
            ):
                self._deps.cluster_repository(self._deps.db_factory).set_expected_cluster_uuid_if_missing(
                    cluster_id, state["cluster_uuid"]
                )
            self._record_cluster_zoning(cluster, breakdown)
        except Exception as error:
            if "CERTIFICATE_VERIFY_FAILED" in str(error):
                self._deps.invalidate_cluster_ca(cluster_id)
            previous = self.cluster_states.get(cluster_id, {})
            state = {**previous, "cluster_id": cluster_id, "status": "unknown", "observed_at": observed, "last_error": str(error)[:300]}
        self.cluster_states[cluster_id] = state
        self.history.setdefault(cluster_id, []).append(state)
        cutoff = time.time() - 900
        self.history[cluster_id] = [item for item in self.history[cluster_id] if datetime.fromisoformat(item["observed_at"].replace("Z", "+00:00")).timestamp() >= cutoff]
        await self.publish("cluster_metrics", state)

    def snapshot(self):
        with self._deps.db_factory() as con:
            cluster_ids = self._deps.cluster_repository.from_connection(con).ids()
            clusters = [self._deps.cluster_record(con, cluster_id) for cluster_id in cluster_ids]
        nodes = self._deps.host_repository(self._deps.db_factory).list()
        observations = self._deps.observability_repository(self._deps.db_factory).runtime_observations()
        host_clusters = {}
        for cluster in clusters:
            for member in cluster["members"]:
                host_clusters.setdefault(member["node_id"], []).append({
                    "id": cluster["id"], "name": cluster["name"], "theme_color": cluster["theme_color"],
                })
        hosts = []
        alerts = []
        for node in nodes:
            observed = self.host_states.get(node["id"]) or observations.get(node["id"]) or {}
            host = {
                **node, "enabled": bool(node["enabled"]),
                "initialized": bool(observed.get("initialized", 0)), "reachable": bool(observed.get("reachable", 0)),
                "podman_socket_active": bool(observed.get("podman_socket_active", 0)), "os_name": observed.get("os_name", ""), "podman_version": observed.get("podman_version", ""),
                "observed_at": observed.get("observed_at"),
                "resource_observed_at": (self.host_states.get(node["id"]) or {}).get("resource_observed_at"),
                "last_error": observed.get("last_error", ""),
                "containers": observed.get("containers", []), "pods": observed.get("pods", []),
            }
            hosts.append(host)
            if node["enabled"] and (not host["reachable"] or not host["podman_socket_active"]):
                alerts.append({"severity": "warning", "source": "host", "source_id": node["id"], "message": f"{node['name']}: {host['last_error'] or 'Podman socket is unavailable'}"})
        host_map = {item["id"]: item for item in hosts}
        summaries = []
        for cluster in clusters:
            metrics = self.cluster_states.get(cluster["id"], {})
            if self._deps.cluster_awaits_data_role(cluster):
                metrics = {
                    **metrics, "cluster_id": cluster["id"], "status": "awaiting_data",
                    "observed_at": metrics.get("observed_at") or self._deps.utc_now(), "last_error": "",
                }
            health = metrics.get("status", "unknown") if cluster["assignments"] else "unknown"
            if health not in {"green", "yellow", "red", "awaiting_data"}:
                health = "unknown"
            member_hosts = [host_map[item["node_id"]] for item in cluster["members"] if item["node_id"] in host_map]
            if health == "green" and any(not host["reachable"] for host in member_hosts):
                health = "yellow"
            if metrics.get("unassigned_primary_shards", 0):
                alerts.append({"severity": "critical", "source": "cluster", "source_id": cluster["id"], "message": f"{cluster['name']}: unassigned primary shards"})
            elif metrics.get("unassigned_shards", 0):
                alerts.append({"severity": "warning", "source": "cluster", "source_id": cluster["id"], "message": f"{cluster['name']}: unassigned replica shards"})
            if metrics.get("last_error"):
                alerts.append({"severity": "warning", "source": "cluster", "source_id": cluster["id"], "message": f"{cluster['name']}: {metrics['last_error']}"})
            if cluster["zoning_status"]["status"] in {"drift", "failed"}:
                detail = cluster["zoning_status"].get("last_error") or f"Zoning status is {cluster['zoning_status']['status']}"
                alerts.append({"severity": "warning", "source": "cluster", "source_id": cluster["id"], "message": f"{cluster['name']}: {detail}"})
            monitoring = dict(cluster["log_monitoring"])
            companion_states = [assignment.get("filebeat", {}).get("state", "disabled") for assignment in cluster["assignments"]]
            if not monitoring["filebeat_enabled"]:
                monitoring["companion_state"] = "disabled"
            elif any(state == "degraded" for state in companion_states):
                monitoring["companion_state"] = "degraded"
            elif companion_states and all(state == "running" for state in companion_states):
                monitoring["companion_state"] = "running"
            else:
                monitoring["companion_state"] = "pending"
            summaries.append({
                "id": cluster["id"], "name": cluster["name"], "slug": cluster["slug"], "theme_color": cluster["theme_color"],
                "health": health, "node_count": len(cluster["members"]), "workload_count": len(cluster["assignments"]),
                "metrics": metrics, "history": self.history.get(cluster["id"], []), "log_monitoring": monitoring,
            })
        cross_cluster_host_usage = [{
            "node_id": host["id"], "name": host["name"], "reachable": host["reachable"],
            "observed_at": host.get("resource_observed_at") or host.get("observed_at"), "last_error": host["last_error"],
            "resource_observation_error": (self.host_states.get(host["id"]) or {}).get("resource_observation_error", ""),
            "clusters": host_clusters[host["id"]], "history": self.host_history.get(host["id"], []),
        } for host in hosts if host["id"] in host_clusters]
        return {
            "generated_at": self._deps.utc_now(), "clusters": summaries, "hosts": hosts, "alerts": alerts,
            "cross_cluster_host_usage": cross_cluster_host_usage,
        }
