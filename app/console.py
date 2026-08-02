import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import ssl
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app import main as core


router = APIRouter()
RUNTIME = core.DATA / "runtime"
CA_CACHE = core.DATA / "cluster-cas"
COLLECT_INTERVAL = int(os.getenv("TELEMETRY_INTERVAL_SECONDS", "5"))
STREAM_TOKEN_TTL = 600
REVEAL_GRANT_TTL = 60
VIRTUAL_STORAGE_TYPES = {
    "autofs", "bpf", "cgroup", "cgroup2", "configfs", "debugfs", "devpts", "devtmpfs",
    "efivarfs", "fusectl", "hugetlbfs", "mqueue", "nsfs", "overlay", "proc", "pstore",
    "ramfs", "rpc_pipefs", "securityfs", "selinuxfs", "sysfs", "tmpfs", "tracefs",
}
UNSAFE_STORAGE_MOUNT_PREFIXES = (
    "/boot", "/dev", "/etc", "/lib", "/lib64", "/proc", "/root", "/run", "/sbin", "/sys",
    "/usr", "/var/lib/containers", "/var/lib/kubelet",
)


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def completed_run(kind, target, message):
    with core.db() as con:
        cursor = con.execute(
            "INSERT INTO runs(kind,target,status,command_json,log,finished_at,context_json) VALUES (?,?, 'succeeded','[]',?,CURRENT_TIMESTAMP,'{}')",
            (kind, target, f"[{utc_now()}] {message}\n"),
        )
        return cursor.lastrowid


def audit(username, action, cluster_id=None, item_id="", detail=""):
    with core.db() as con:
        con.execute(
            "INSERT INTO audit_events(username,action,cluster_id,item_id,detail) VALUES (?,?,?,?,?)",
            (username, action, cluster_id, item_id, detail[:512]),
        )


def signed_scope_token(scope):
    issued = str(int(time.time()))
    payload = f"{scope}:{issued}".encode()
    signature = hmac.new(core.KEY.encode(), payload, hashlib.sha256).digest()
    return core.token_piece(payload) + "." + core.token_piece(signature)


def valid_scope_token(token, scope, ttl=STREAM_TOKEN_TTL):
    try:
        payload_text, signature_text = token.split(".", 1)
        payload = core.read_token_piece(payload_text)
        signature = core.read_token_piece(signature_text)
        expected = hmac.new(core.KEY.encode(), payload, hashlib.sha256).digest()
        signed_scope, issued = payload.decode().rsplit(":", 1)
        return hmac.compare_digest(signature, expected) and signed_scope == scope and int(issued) + ttl >= time.time()
    except (ValueError, UnicodeDecodeError):
        return False


def ca_ssl_context(ca_path):
    context = ssl.create_default_context(cafile=str(ca_path))
    # Preserve CA and hostname verification while accepting CAs issued by
    # controller versions that predated OpenSSL's strict key-usage checks.
    if hasattr(ssl, "VERIFY_X509_STRICT"):
        context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return context


def invalidate_cluster_ca(cluster_id):
    (CA_CACHE / f"cluster-{cluster_id}.crt").unlink(missing_ok=True)


def ssh_args(node):
    key_path = core.active_ssh_key_path()
    known_hosts = core.known_hosts_path([node["id"]] if node.get("id") else None)
    args = [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=15",
        "-i", key_path, "-p", str(node["ssh_port"]),
    ]
    args += core.ssh_host_key_args(node, known_hosts)
    return args + [f"{node['ssh_user']}@{node['address']}"]


async def remote_command(node, *command, timeout=8):
    process = await asyncio.create_subprocess_exec(
        *ssh_args(node), *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError("SSH operation timed out")
    if process.returncode:
        raise RuntimeError(stderr.decode(errors="replace").strip() or "SSH operation failed")
    return stdout


def ssh_error_summary(error):
    message = " ".join(str(error).replace("\r", "\n").split()).lower()
    if "permission denied" in message or "too many authentication failures" in message:
        return "Controller SSH key authentication failed"
    if "host key verification failed" in message or "remote host identification has changed" in message:
        return "SSH host key verification failed"
    if "connection timed out" in message or "operation timed out" in message:
        return "SSH connection timed out"
    if "connection refused" in message:
        return "SSH service refused the connection"
    if "no route to host" in message or "network is unreachable" in message:
        return "SSH host is unreachable"
    if "could not resolve hostname" in message:
        return "SSH address cannot be resolved"
    return "SSH connection failed"


async def host_identity(node):
    script = (
        "if test -r /etc/os-release; then . /etc/os-release; "
        "printf 'ECP_OS=%s\\n' \"${PRETTY_NAME:-${NAME:-unknown}}\"; "
        "else printf 'ECP_OS=%s\\n' \"$(uname -sr)\"; fi; "
        "if command -v podman >/dev/null 2>&1; then podman --version | sed 's/^/ECP_PODMAN=/'; "
        "else printf 'ECP_PODMAN=\\n'; fi"
    )
    output = (await remote_command(node, script)).decode(errors="replace")
    values = {}
    for line in output.splitlines():
        if line.startswith("ECP_OS="):
            values["os_name"] = line[7:].strip()[:256]
        elif line.startswith("ECP_PODMAN="):
            values["podman_version"] = line[11:].replace("podman version ", "", 1).strip()[:128]
    return values.get("os_name", "unknown"), values.get("podman_version", "")


def storage_mount_entries(filesystems):
    for filesystem in filesystems or []:
        if not isinstance(filesystem, dict):
            continue
        yield filesystem
        yield from storage_mount_entries(filesystem.get("children"))


def storage_mount_eligibility(target, fstype, options, available_bytes):
    option_set = {option.strip() for option in options.split(",")}
    if fstype in VIRTUAL_STORAGE_TYPES:
        return False, "virtual filesystem"
    if "rw" not in option_set:
        return False, "read-only mount"
    if available_bytes <= 0:
        return False, "no free space"
    # The root device may be selected, but the UI always expands it to a dedicated
    # non-system workload directory rather than using / as the data path.
    if target == "/":
        return True, ""
    if not core.valid_storage_path(target):
        return False, "system mount"
    if any(target == prefix or target.startswith(prefix + "/") for prefix in UNSAFE_STORAGE_MOUNT_PREFIXES):
        return False, "controller-reserved mount"
    return True, ""


def storage_mounts(payload):
    mounts = []
    seen_targets = set()
    for entry in storage_mount_entries(payload.get("filesystems")):
        target = entry.get("target")
        if not isinstance(target, str) or not target or target in seen_targets:
            continue
        seen_targets.add(target)
        source = str(entry.get("source") or "unknown")
        fstype = str(entry.get("fstype") or "unknown")
        options = str(entry.get("options") or "")
        try:
            size_bytes = max(int(entry.get("size") or 0), 0)
            available_bytes = max(int(entry.get("avail") or 0), 0)
        except (TypeError, ValueError):
            size_bytes = available_bytes = 0
        eligible, reason = storage_mount_eligibility(target, fstype, options, available_bytes)
        mounts.append({
            "mount_point": target,
            "source": source,
            "filesystem": fstype,
            "size_bytes": size_bytes,
            "available_bytes": available_bytes,
            "writable": "rw" in {option.strip() for option in options.split(",")},
            "eligible": eligible,
            "unavailable_reason": reason,
        })
    return sorted(mounts, key=lambda mount: mount["mount_point"])


def cluster_awaits_data_role(cluster):
    assignments = cluster["assignments"]
    return any(item["role"] == "master" for item in assignments) and not any(item["role"] in {"hot", "warm"} for item in assignments)


class PodmanTunnel:
    def __init__(self, node_id):
        self.node_id = node_id
        self.path = RUNTIME / f"podman-{node_id}.sock"
        self.process = None

    async def ensure(self, node):
        if self.process and self.process.returncode is None and self.path.exists():
            return self.path
        await self.close()
        RUNTIME.mkdir(parents=True, exist_ok=True)
        self.path.unlink(missing_ok=True)
        args = ssh_args(node)[:-1] + [
            "-o", "ExitOnForwardFailure=yes", "-o", "StreamLocalBindUnlink=yes", "-NT",
            "-L", f"{self.path}:/run/podman/podman.sock", ssh_args(node)[-1],
        ]
        self.process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        for _ in range(50):
            if self.path.exists():
                return self.path
            if self.process.returncode is not None:
                error = await self.process.stderr.read()
                raise RuntimeError(error.decode(errors="replace").strip() or "Podman SSH tunnel failed")
            await asyncio.sleep(0.1)
        await self.close()
        raise RuntimeError("Podman SSH tunnel did not become ready")

    async def close(self):
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=2)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        self.process = None
        self.path.unlink(missing_ok=True)


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


NODE_TYPE_ORDER = {
    "Hot data": 0,
    "Warm data": 1,
    "Cold data": 2,
    "Frozen data": 3,
    "Content data": 4,
    "Data": 5,
    "Master": 6,
    "Machine learning": 7,
    "Ingest": 8,
    "Coordinating": 9,
    "Other": 10,
}


def elastic_node_type(roles):
    role_set = set(roles)
    for role, label in (
        ("data_hot", "Hot data"),
        ("data_warm", "Warm data"),
        ("data_cold", "Cold data"),
        ("data_frozen", "Frozen data"),
        ("data_content", "Content data"),
        ("data", "Data"),
        ("master", "Master"),
        ("ml", "Machine learning"),
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
        for field in (
            "shards", "disk_total_bytes", "disk_available_bytes", "disk_used_bytes",
            "heap_used_bytes", "heap_max_bytes",
        ):
            aggregate[field] += int(node.get(field) or 0)
    return sorted(zones.values(), key=lambda item: (item["zone"] == "unassigned", item["zone"]))


class TelemetryManager:
    def __init__(self):
        self.host_states = {}
        self.cluster_states = {}
        self.history = {}
        self.tunnels = {}
        self.subscribers = set()
        self.task = None

    async def start(self):
        RUNTIME.mkdir(parents=True, exist_ok=True)
        CA_CACHE.mkdir(parents=True, exist_ok=True)
        if not self.task:
            self.task = asyncio.create_task(self._loop())

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
        await asyncio.gather(*(tunnel.close() for tunnel in self.tunnels.values()), return_exceptions=True)

    async def _loop(self):
        while True:
            try:
                await self.collect_once()
            except Exception:
                pass
            await asyncio.sleep(COLLECT_INTERVAL)

    async def collect_once(self):
        with core.db() as con:
            nodes = [dict(row) for row in con.execute("SELECT * FROM nodes WHERE enabled=1 ORDER BY id")]
            cluster_ids = [row["id"] for row in con.execute("SELECT id FROM clusters ORDER BY id")]
        if nodes:
            await asyncio.gather(*(self._collect_host(node) for node in nodes))
        for cluster_id in cluster_ids:
            await self._collect_cluster(cluster_id)

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

    def _record_workload_runtime(self, node_id, containers):
        by_name = {item["name"]: item for item in containers}
        with core.db() as con:
            assignments = con.execute(
                "SELECT cluster_assignments.id, cluster_assignments.node_id, cluster_assignments.role, clusters.slug "
                "FROM cluster_assignments JOIN clusters ON clusters.id=cluster_assignments.cluster_id "
                "WHERE cluster_assignments.node_id=? AND cluster_assignments.state='active'",
                (node_id,),
            ).fetchall()
            for assignment in assignments:
                name = core.workload_name({"slug": assignment["slug"]}, assignment)
                container = by_name.get(name)
                image = container.get("image", "") if container else ""
                digest = container.get("digest", "") if container else ""
                version = core.image_version(image)
                state = str(container.get("state", "")).lower() if container else ""
                status = str(container.get("status", "")).lower() if container else ""
                running = state == "running" or status.startswith("up ")
                error = "" if running and version else (
                    "Managed workload container not found" if not container else
                    "Runtime image does not report a release tag" if not version else
                    "Managed workload container is not running"
                )
                con.execute(
                    "INSERT INTO workload_observations(assignment_id,image,digest,version,running,cached,observed_at,error) "
                    "VALUES (?,?,?,?,?,?,CURRENT_TIMESTAMP,?) "
                    "ON CONFLICT(assignment_id) DO UPDATE SET image=excluded.image,digest=excluded.digest,version=excluded.version,"
                    "running=excluded.running,cached=excluded.cached,observed_at=excluded.observed_at,error=excluded.error",
                    (assignment["id"], image, digest, version, int(running), int(bool(container)), error),
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
            runtime_zone = runtime_zones.get(core.workload_name(cluster, assignment), "")
            observed[str(assignment["id"])] = runtime_zone
            expected_zone = members.get(assignment["node_id"], {}).get("zone_id") or ""
            if cluster["zoning"]["mode"] != "disabled" and runtime_zone != expected_zone:
                drift.append(
                    f"{assignment['role']} on {assignment['node_name']} expected {expected_zone or 'no zone'} "
                    f"but reports {runtime_zone or 'no zone'}"
                )
        with core.db() as con:
            current = con.execute(
                "SELECT applied_mode,applied_zones_json FROM cluster_zoning_observations WHERE cluster_id=?",
                (cluster["id"],),
            ).fetchone()
            applied_mode = current["applied_mode"] if current else "disabled"
            applied_zones = json.loads(current["applied_zones_json"] or "[]") if current else []
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
            con.execute(
                "INSERT INTO cluster_zoning_observations(cluster_id,applied_mode,applied_zones_json,observed_zones_json,status,observed_at,last_error) "
                "VALUES (?,?,?,?,?,CURRENT_TIMESTAMP,?) "
                "ON CONFLICT(cluster_id) DO UPDATE SET observed_zones_json=excluded.observed_zones_json,status=excluded.status,"
                "observed_at=excluded.observed_at,last_error=excluded.last_error",
                (cluster["id"], applied_mode, json.dumps(applied_zones), json.dumps(observed), status, last_error),
            )

    async def _collect_host(self, node):
        observed = utc_now()
        tunnel = self.tunnels.get(node["id"])
        try:
            os_name, installed_podman = await host_identity(node)
            marker = await remote_command(
                node,
                "if test -f /etc/elastic-control-host-init; then printf initialized; else printf uninitialized; fi",
            )
            initialized = marker.decode(errors="replace").strip() == "initialized"
        except Exception as error:
            if tunnel:
                await tunnel.close()
            state = {
                "node_id": node["id"], "reachable": False, "initialized": False, "podman_socket_active": False,
                "os_name": "", "podman_version": "", "observed_at": observed, "last_error": f"SSH: {ssh_error_summary(error)}", "containers": [], "pods": [],
            }
        else:
            if not initialized:
                if tunnel:
                    await tunnel.close()
                state = {
                    "node_id": node["id"], "reachable": True, "initialized": False, "podman_socket_active": False,
                    "os_name": os_name, "podman_version": installed_podman, "observed_at": observed, "last_error": "", "containers": [], "pods": [],
                }
            else:
                try:
                    tunnel = self.tunnels.setdefault(node["id"], PodmanTunnel(node["id"]))
                    socket_path = await tunnel.ensure(node)
                    transport = httpx.AsyncHTTPTransport(uds=str(socket_path))
                    async with httpx.AsyncClient(transport=transport, timeout=4) as client:
                        version_data = await self._podman_get(client, "/version")
                        api_version = version_data.get("ApiVersion") or version_data.get("APIVersion") or ""
                        containers = await self._podman_get(client, "/containers/json?all=true", api_version)
                        managed = []
                        for item in containers:
                            name = container_name(item)
                            if not name.startswith("ecp-"):
                                continue
                            stats = {}
                            try:
                                stats = container_stats(await self._podman_get(client, f"/containers/{item.get('Id') or item.get('ID')}/stats?stream=false", api_version))
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
                    }
                except Exception as error:
                    if tunnel:
                        await tunnel.close()
                    state = {
                        "node_id": node["id"], "reachable": True, "initialized": True, "podman_socket_active": False,
                        "os_name": os_name, "podman_version": installed_podman, "observed_at": observed, "last_error": f"Podman: {error}"[:300], "containers": [], "pods": [],
                    }
        self.host_states[node["id"]] = state
        with core.db() as con:
            con.execute(
                "INSERT INTO host_runtime_observations(node_id,initialized,reachable,podman_socket_active,os_name,podman_version,observed_at,last_error) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(node_id) DO UPDATE SET initialized=excluded.initialized,reachable=excluded.reachable,podman_socket_active=excluded.podman_socket_active,os_name=excluded.os_name,podman_version=excluded.podman_version,observed_at=excluded.observed_at,last_error=excluded.last_error",
                (node["id"], int(state["initialized"]), int(state["reachable"]), int(state["podman_socket_active"]), state["os_name"], state["podman_version"], observed, state["last_error"]),
            )
        if state["podman_socket_active"]:
            self._record_workload_runtime(node["id"], state["containers"])
        await self.publish("host_stats", state)

    async def _ensure_cluster_ca(self, cluster, master, node):
        path = CA_CACHE / f"cluster-{cluster['id']}.crt"
        if path.exists() and path.stat().st_size:
            return path
        remote = f"/etc/elastic-control/clusters/{cluster['slug']}/workloads/ecp-{cluster['slug']}-master-{master['node_id']}/certs/ca.crt"
        content = await remote_command(node, "cat", remote)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(content)
        os.chmod(temporary, 0o644)
        temporary.replace(path)
        return path

    async def _monitoring_key(self, cluster, client):
        with core.db() as con:
            row = con.execute("SELECT secrets_json FROM clusters WHERE id=?", (cluster["id"],)).fetchone()
            credentials = core.open_config(row["secrets_json"])
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
        with core.db() as con:
            con.execute("UPDATE clusters SET secrets_json=? WHERE id=?", (core.seal_config(json.dumps(credentials)), cluster["id"]))
        return encoded, False

    def _clear_monitoring_key(self, cluster_id):
        with core.db() as con:
            row = con.execute("SELECT secrets_json FROM clusters WHERE id=?", (cluster_id,)).fetchone()
            credentials = core.open_config(row["secrets_json"])
            if "monitoring_api_key" not in credentials:
                return
            credentials.pop("monitoring_api_key")
            con.execute("UPDATE clusters SET secrets_json=? WHERE id=?", (core.seal_config(json.dumps(credentials)), cluster_id))

    async def _collect_cluster(self, cluster_id):
        with core.db() as con:
            cluster = core.cluster_record(con, cluster_id)
            master = next((item for item in cluster["assignments"] if item["role"] == "master"), None)
            if not master:
                self.cluster_states[cluster_id] = {"cluster_id": cluster_id, "status": "unknown", "observed_at": utc_now(), "last_error": "No master is assigned"}
                return
            member = next(item for item in cluster["members"] if item["node_id"] == master["node_id"])
            node = dict(con.execute("SELECT * FROM nodes WHERE id=?", (master["node_id"],)).fetchone())
        observed = utc_now()
        if cluster_awaits_data_role(cluster):
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
            async with httpx.AsyncClient(base_url=base, verify=ca_ssl_context(ca_path), timeout=5) as client:
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
                    return health, stats, nodes, pending, settings, allocation

                key, cached_key = await self._monitoring_key(cluster, client)
                try:
                    health, stats, nodes, pending, settings, allocation = await collect_metrics(key)
                except httpx.HTTPStatusError as error:
                    if error.response.status_code != 401 or not cached_key:
                        raise
                    self._clear_monitoring_key(cluster_id)
                    key, _ = await self._monitoring_key(cluster, client)
                    health, stats, nodes, pending, settings, allocation = await collect_metrics(key)
            node_stats = nodes.get("nodes", {})
            fs_total = sum(item.get("fs", {}).get("total", {}).get("total_in_bytes", 0) for item in node_stats.values())
            fs_available = sum(item.get("fs", {}).get("total", {}).get("available_in_bytes", 0) for item in node_stats.values())
            heap_used = sum(item.get("jvm", {}).get("mem", {}).get("heap_used_in_bytes", 0) for item in node_stats.values())
            heap_max = sum(item.get("jvm", {}).get("mem", {}).get("heap_max_in_bytes", 0) for item in node_stats.values())
            breakdown = node_breakdown(node_stats, allocation)
            state = {
                "cluster_id": cluster_id, "status": health.get("status", "unknown"), "observed_at": observed, "last_error": "",
                "nodes": health.get("number_of_nodes", 0), "data_nodes": health.get("number_of_data_nodes", 0),
                "active_primary_shards": health.get("active_primary_shards", 0), "active_shards": health.get("active_shards", 0),
                "unassigned_shards": health.get("unassigned_shards", 0), "unassigned_primary_shards": health.get("unassigned_primary_shards", 0),
                "indices": stats.get("indices", {}).get("count", 0), "documents": stats.get("indices", {}).get("docs", {}).get("count", 0),
                "store_bytes": stats.get("indices", {}).get("store", {}).get("size_in_bytes", 0),
                "disk_total_bytes": fs_total, "disk_available_bytes": fs_available,
                "heap_used_bytes": heap_used, "heap_max_bytes": heap_max,
                "pending_tasks": len(pending.get("tasks", [])), "index_health": health.get("indices", {}),
                "node_breakdown": breakdown,
                "zone_breakdown": zone_breakdown(breakdown),
                "effective_settings": settings,
            }
            self._record_cluster_zoning(cluster, breakdown)
        except Exception as error:
            if "CERTIFICATE_VERIFY_FAILED" in str(error):
                invalidate_cluster_ca(cluster_id)
            previous = self.cluster_states.get(cluster_id, {})
            state = {**previous, "cluster_id": cluster_id, "status": "unknown", "observed_at": observed, "last_error": str(error)[:300]}
        self.cluster_states[cluster_id] = state
        self.history.setdefault(cluster_id, []).append(state)
        cutoff = time.time() - 900
        self.history[cluster_id] = [item for item in self.history[cluster_id] if datetime.fromisoformat(item["observed_at"].replace("Z", "+00:00")).timestamp() >= cutoff]
        await self.publish("cluster_metrics", state)

    def snapshot(self):
        with core.db() as con:
            clusters = [core.cluster_record(con, row["id"]) for row in con.execute("SELECT id FROM clusters ORDER BY name")]
            nodes = [dict(row) for row in con.execute("SELECT * FROM nodes ORDER BY name")]
            observations = {row["node_id"]: dict(row) for row in con.execute("SELECT * FROM host_runtime_observations")}
        hosts = []
        alerts = []
        for node in nodes:
            observed = self.host_states.get(node["id"]) or observations.get(node["id"]) or {}
            host = {
                **node, "enabled": bool(node["enabled"]),
                "initialized": bool(observed.get("initialized", 0)), "reachable": bool(observed.get("reachable", 0)),
                "podman_socket_active": bool(observed.get("podman_socket_active", 0)), "os_name": observed.get("os_name", ""), "podman_version": observed.get("podman_version", ""),
                "observed_at": observed.get("observed_at"), "last_error": observed.get("last_error", ""),
                "containers": observed.get("containers", []), "pods": observed.get("pods", []),
            }
            hosts.append(host)
            if node["enabled"] and (not host["reachable"] or not host["podman_socket_active"]):
                alerts.append({"severity": "warning", "source": "host", "source_id": node["id"], "message": f"{node['name']}: {host['last_error'] or 'Podman socket is unavailable'}"})
        host_map = {item["id"]: item for item in hosts}
        summaries = []
        for cluster in clusters:
            metrics = self.cluster_states.get(cluster["id"], {})
            if cluster_awaits_data_role(cluster):
                metrics = {
                    **metrics, "cluster_id": cluster["id"], "status": "awaiting_data",
                    "observed_at": metrics.get("observed_at") or utc_now(), "last_error": "",
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
        return {"generated_at": utc_now(), "clusters": summaries, "hosts": hosts, "alerts": alerts}


telemetry = TelemetryManager()
reveal_grants = {}


class RevealGrantInput(BaseModel):
    cluster_id: int = Field(ge=1)
    password: str = Field(min_length=1, max_length=512)


class RevealInput(BaseModel):
    grant_token: str = Field(min_length=20, max_length=512)
    purpose: str = Field(default="reveal", pattern=r"^(reveal|copy)$")


@router.get("/api/dashboard/snapshot")
async def dashboard_snapshot(_: str = Depends(core.user)):
    return telemetry.snapshot()


@router.post("/api/dashboard/stream-token")
async def dashboard_stream_token(_: str = Depends(core.user)):
    return {"token": signed_scope_token("dashboard"), "expires_in": STREAM_TOKEN_TTL}


@router.get("/api/dashboard/events")
async def dashboard_events(request: Request, token: str = ""):
    header = request.headers.get("authorization", "")
    authorized = core.token_user(header[7:]) if header.startswith("Bearer ") else None
    if not authorized and not valid_scope_token(token, "dashboard"):
        raise HTTPException(401, "Authentication required")

    async def stream():
        queue = telemetry.subscribe()
        last_runs = None
        try:
            snapshot = telemetry.snapshot()
            yield "event: snapshot\ndata: " + json.dumps(snapshot) + "\n\n"
            while True:
                if await request.is_disconnected():
                    return
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=3)
                    yield f"id: {message['id']}\nevent: {message['event']}\ndata: {json.dumps(message['data'])}\n\n"
                except asyncio.TimeoutError:
                    with core.db() as con:
                        runs = [dict(row) for row in con.execute("SELECT id,kind,target,status,created_at,finished_at FROM runs ORDER BY id DESC LIMIT 12")]
                    encoded = json.dumps(runs, sort_keys=True)
                    if encoded != last_runs:
                        yield "event: run\ndata: " + json.dumps({"runs": runs}) + "\n\n"
                        last_runs = encoded
                    else:
                        yield ": heartbeat\n\n"
        finally:
            telemetry.unsubscribe(queue)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/api/nodes/{node_id}/runtime")
async def node_runtime(node_id: int, _: str = Depends(core.user)):
    with core.db() as con:
        node = con.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
        if not node:
            raise HTTPException(404, "Host not found")
        observed = con.execute("SELECT * FROM host_runtime_observations WHERE node_id=?", (node_id,)).fetchone()
    state = telemetry.host_states.get(node_id) or (dict(observed) if observed else {})
    return {
        "node_id": node_id, "initialized": bool(state.get("initialized", 0)), "reachable": bool(state.get("reachable", 0)),
        "podman_socket_active": bool(state.get("podman_socket_active", 0)), "os_name": state.get("os_name", ""), "podman_version": state.get("podman_version", ""),
        "observed_at": state.get("observed_at"), "last_error": state.get("last_error", ""),
        "containers": state.get("containers", []), "pods": state.get("pods", []),
    }


@router.get("/api/nodes/{node_id}/storage")
async def node_storage(node_id: int, _: str = Depends(core.user)):
    node = enabled_node(node_id)
    try:
        output = await remote_command(
            node,
            "findmnt", "--json", "--bytes", "--real",
            "--output", "TARGET,SOURCE,FSTYPE,OPTIONS,SIZE,AVAIL",
            timeout=12,
        )
        payload = json.loads(output)
        if not isinstance(payload, dict):
            raise ValueError("Host storage inventory has an invalid format")
    except (json.JSONDecodeError, RuntimeError, UnicodeDecodeError, ValueError) as error:
        raise HTTPException(503, f"Could not inventory host storage: {str(error)[:160]}") from error
    return {"node_id": node_id, "observed_at": utc_now(), "mounts": storage_mounts(payload)}


def enabled_node(node_id):
    with core.db() as con:
        row = con.execute("SELECT * FROM nodes WHERE id=? AND enabled=1", (node_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Enabled host not found")
    return dict(row)


@router.post("/api/nodes/{node_id}/initialize")
async def initialize_node(node_id: int, _: str = Depends(core.user)):
    node = enabled_node(node_id)
    run_id = core.launch("host-init", node["name"], lambda inv, _variables: [
        "ansible-playbook", "-i", str(inv), str(core.PLAYBOOKS / "host-init.yml"), "--limit", node["name"], "--private-key", core.active_ssh_key_path(),
    ])
    return {"run_id": run_id}


@router.post("/api/nodes/{node_id}/reboot")
async def reboot_node(node_id: int, _: str = Depends(core.user)):
    node = enabled_node(node_id)
    run_id = core.launch("host-reboot", node["name"], lambda inv, _variables: [
        "ansible-playbook", "-i", str(inv), str(core.PLAYBOOKS / "host-reboot.yml"), "--limit", node["name"], "--private-key", core.active_ssh_key_path(),
    ])
    return {"run_id": run_id}


@router.post("/api/nodes/{node_id}/deinitialize")
async def deinitialize_node(node_id: int, _: str = Depends(core.user)):
    node = enabled_node(node_id)
    with core.db() as con:
        if con.execute("SELECT 1 FROM cluster_assignments WHERE node_id=?", (node_id,)).fetchone():
            raise HTTPException(409, "Detach or purge all managed workloads before de-initializing this host")
    run_id = core.launch("host-deinit", node["name"], lambda inv, _variables: [
        "ansible-playbook", "-i", str(inv), str(core.PLAYBOOKS / "host-deinit.yml"), "--limit", node["name"], "--private-key", core.active_ssh_key_path(),
    ])
    return {"run_id": run_id}


@router.get("/api/clusters/{cluster_id}/settings")
async def cluster_settings(cluster_id: int, _: str = Depends(core.user)):
    with core.db() as con:
        cluster = core.cluster_record(con, cluster_id)
    return {
        "cluster_id": cluster_id, "theme_color": cluster["theme_color"], "desired_version": cluster["desired_version"],
        "network_defaults": cluster["network_defaults"], "elasticsearch_settings": cluster["elasticsearch_settings"],
    }


@router.put("/api/clusters/{cluster_id}/settings")
async def update_cluster_settings(cluster_id: int, settings: core.ElasticsearchSettings, _: str = Depends(core.user)):
    with core.db() as con:
        cluster = core.cluster_record(con, cluster_id)
        con.execute("UPDATE clusters SET elasticsearch_settings_json=? WHERE id=?", (settings.model_dump_json(), cluster_id))
        master = next((item for item in cluster["assignments"] if item["role"] == "master"), None)
        if master:
            member = next(item for item in cluster["members"] if item["node_id"] == master["node_id"])
            credentials = core.open_config(con.execute("SELECT secrets_json FROM clusters WHERE id=?", (cluster_id,)).fetchone()["secrets_json"])
    if not master:
        return {"updated": True, "run_id": completed_run("cluster-settings", cluster["name"], "Stored settings; no master is assigned")}
    payload = {
        "cluster": {"id": cluster_id, "name": cluster["name"], "slug": cluster["slug"], "ports": cluster["ports"]},
        "bootstrap": {"node_name": master["node_name"], "node_id": master["node_id"], "user_address": member["user_address"]},
        "credentials": credentials,
        "settings": settings.model_dump(),
    }
    run_id = core.launch("cluster-settings", cluster["name"], lambda inv, variables_path: [
        "ansible-playbook", "-i", str(inv), str(core.PLAYBOOKS / "cluster-settings.yml"), "--limit", master["node_name"],
        "--private-key", core.active_ssh_key_path(), "--extra-vars", "@" + str(variables_path),
    ], variables=payload)
    return {"updated": True, "run_id": run_id}


def sensitive_catalog(con, cluster_id):
    cluster = core.cluster_record(con, cluster_id)
    credentials = core.open_config(con.execute("SELECT secrets_json FROM clusters WHERE id=?", (cluster_id,)).fetchone()["secrets_json"])
    items = []
    for key, label in (("elastic_password", "Elastic superuser password"), ("kibana_password", "kibana_system password"), ("monitoring_api_key", "Dashboard monitoring API key")):
        items.append({"id": f"cluster.{key}", "label": label, "category": "Credentials", "source": "controller", "db_key": key, "available": bool(credentials.get(key))})
    master = next((item for item in cluster["assignments"] if item["role"] == "master"), None)
    if master:
        node = con.execute("SELECT * FROM nodes WHERE id=?", (master["node_id"],)).fetchone()
        base = f"/etc/elastic-control/clusters/{cluster['slug']}"
        items += [
            {"id": "cluster.ca_certificate", "label": "Cluster CA certificate", "category": "Certificates", "source": master["node_name"], "node": dict(node), "path": f"{base}/ca/ca.crt", "certificate": True},
            {"id": "cluster.ca_private_key", "label": "Cluster CA private key", "category": "Private keys", "source": master["node_name"], "node": dict(node), "path": f"{base}/ca/ca.key"},
        ]
    for assignment in cluster["assignments"]:
        node = con.execute("SELECT * FROM nodes WHERE id=?", (assignment["node_id"],)).fetchone()
        workload = f"ecp-{cluster['slug']}-{assignment['role']}-{assignment['node_id']}"
        base = f"/etc/elastic-control/clusters/{cluster['slug']}/workloads/{workload}"
        prefix = f"assignment.{assignment['id']}"
        items += [
            {"id": f"{prefix}.certificate", "label": f"{workload} certificate", "category": "Certificates", "source": assignment["node_name"], "node": dict(node), "path": f"{base}/certs/node.crt", "certificate": True},
            {"id": f"{prefix}.private_key", "label": f"{workload} private key", "category": "Private keys", "source": assignment["node_name"], "node": dict(node), "path": f"{base}/certs/node.key"},
        ]
        if assignment["role"] == "fleet-server":
            items.append({"id": f"{prefix}.fleet_service_token", "label": f"{workload} service token", "category": "Fleet", "source": assignment["node_name"], "node": dict(node), "path": f"{base}/config/fleet-service-token"})
        if assignment["role"] == "elastic-agent":
            items.append({"id": f"{prefix}.enrollment_token", "label": f"{workload} enrollment token", "category": "Fleet", "source": assignment["node_name"], "node": dict(node), "path": f"{base}/config/agent-enrollment-token"})
    return cluster, credentials, items


async def remote_sensitive_metadata(item):
    if "node" not in item:
        return item
    try:
        if item.get("certificate"):
            content = await remote_command(item["node"], "cat", item["path"])
            certificate = x509.load_pem_x509_certificate(content)
            item["fingerprint"] = certificate.fingerprint(hashes.SHA256()).hex(":").upper()
            item["expires_at"] = certificate.not_valid_after_utc.isoformat().replace("+00:00", "Z")
        else:
            await remote_command(item["node"], "test", "-s", item["path"])
        item["available"] = True
    except Exception:
        item["available"] = False
    return item


@router.get("/api/clusters/{cluster_id}/sensitive-items")
async def sensitive_items(cluster_id: int, _: str = Depends(core.user)):
    with core.db() as con:
        _, _, catalog = sensitive_catalog(con, cluster_id)
    enriched = await asyncio.gather(*(remote_sensitive_metadata(dict(item)) for item in catalog))
    public_items = []
    for item in enriched:
        public = {key: value for key, value in item.items() if key not in {"node", "path", "db_key", "certificate"}}
        if item["category"] in {"Certificates", "Private keys"}:
            public["storage_path"] = item["path"]
        public["masked_value"] = "********"
        public_items.append(public)
    return {"items": public_items}


@router.post("/api/auth/reveal-grants")
async def create_reveal_grant(input: RevealGrantInput, username: str = Depends(core.user)):
    with core.db() as con:
        core.cluster_record(con, input.cluster_id)
        row = con.execute("SELECT password_hash FROM users WHERE username=?", (username,)).fetchone()
    if not row or not core.valid_password(input.password, row["password_hash"]):
        audit(username, "reveal-grant-rejected", input.cluster_id)
        raise HTTPException(401, "Re-authentication failed")
    token = secrets.token_urlsafe(32)
    reveal_grants[token] = {"username": username, "cluster_id": input.cluster_id, "expires": time.time() + REVEAL_GRANT_TTL}
    audit(username, "reveal-grant-created", input.cluster_id)
    return {"grant_token": token, "expires_in": REVEAL_GRANT_TTL}


@router.post("/api/clusters/{cluster_id}/sensitive-items/{item_id}/reveal")
async def reveal_sensitive_item(cluster_id: int, item_id: str, input: RevealInput, username: str = Depends(core.user)):
    grant = reveal_grants.get(input.grant_token)
    if not grant or grant["expires"] < time.time() or grant["username"] != username or grant["cluster_id"] != cluster_id:
        raise HTTPException(403, "Reveal grant is missing, expired, or scoped to another cluster")
    with core.db() as con:
        _, credentials, catalog = sensitive_catalog(con, cluster_id)
    item = next((entry for entry in catalog if entry["id"] == item_id), None)
    if not item:
        raise HTTPException(404, "Sensitive item not found")
    if item.get("db_key"):
        value = credentials.get(item["db_key"], "")
    else:
        try:
            value = (await remote_command(item["node"], "cat", item["path"])).decode()
        except Exception as error:
            raise HTTPException(503, f"Sensitive item is unavailable: {str(error)[:160]}")
    if not value:
        raise HTTPException(404, "Sensitive item is not configured")
    audit(username, input.purpose, cluster_id, item_id)
    return {"value": value, "hide_after": 30}
