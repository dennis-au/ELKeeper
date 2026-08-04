"""Host inventory and enrollment orchestration behind the host boundary."""

from __future__ import annotations

import os
from pathlib import Path
import secrets
import shlex
import subprocess
from typing import Callable

import yaml
from fastapi import HTTPException

from app.modules.orchestration import ansible_playbook

from .enrollment import host_key_validation_enabled, ssh_host_key_args
from .repository import HostRepository


class HostEnrollmentOrchestrator:
    """Build ephemeral host artifacts without persisting request-only secrets."""

    def __init__(
        self,
        *,
        db_factory: Callable,
        inventories: Path,
        runtime: Path,
        playbooks: Path,
        active_key_path: Callable[[], str],
        enrollment_key: Callable[[], dict | None],
        managed_key_path: Callable[[dict], str],
        known_hosts_path: Callable[..., str],
        launch: Callable[..., int],
        audit: Callable[..., None],
    ):
        self._db_factory = db_factory
        self._inventories = inventories
        self._runtime = runtime
        self._playbooks = playbooks
        self._active_key_path = active_key_path
        self._enrollment_key = enrollment_key
        self._managed_key_path = managed_key_path
        self._known_hosts_path = known_hosts_path
        self._launch = launch
        self._audit = audit

    def inventory(self, run_id, private_key=None, node_ids=None, password_bootstrap=False, pinned_host_key_only=False):
        with self._db_factory() as connection:
            rows = HostRepository.from_connection(connection).inventory_records_in_connection(connection, node_ids)
        key_path = None if password_bootstrap else (private_key or self._active_key_path())
        known_hosts = self._known_hosts_path(node_ids, include_legacy=not pinned_host_key_only)
        variables = {"ansible_ssh_private_key_file": key_path} if key_path else {}
        content = {"all": {"vars": variables, "hosts": {}}}
        for row in rows:
            ssh_args = ssh_host_key_args(row, known_hosts)
            if password_bootstrap:
                ssh_args += [
                    "-o", "ControlMaster=no", "-o", "ControlPath=none", "-o", "ControlPersist=no",
                    "-o", "PubkeyAuthentication=no", "-o", "PreferredAuthentications=password,keyboard-interactive",
                ]
            content["all"]["hosts"][row["name"]] = {
                "ansible_host": row["address"], "ansible_port": row["ssh_port"], "ansible_user": row["ssh_user"],
                "ansible_ssh_common_args": " ".join(shlex.quote(argument) for argument in ssh_args),
            }
        path = self._inventories / f"run-{run_id}.yaml"
        path.write_text(yaml.safe_dump(content, sort_keys=True), encoding="utf-8")
        os.chmod(path, 0o600)
        return path

    def password_test_known_hosts(self, node):
        if not node["ssh_host_key"]:
            return None
        self._runtime.mkdir(parents=True, exist_ok=True)
        known_hosts = self._runtime / f"password-test-{secrets.token_hex(16)}.known-hosts"
        host = node["address"] if node["ssh_port"] == 22 else f"[{node['address']}]:{node['ssh_port']}"
        known_hosts.write_text(f"{host} {node['ssh_host_key']}\n", encoding="utf-8")
        os.chmod(known_hosts, 0o600)
        return known_hosts

    @staticmethod
    def password_test_command(node, password_fd, known_hosts):
        ssh_args = ssh_host_key_args(node, str(known_hosts) if known_hosts else "/dev/null")
        return [
            "sshpass", "-d", str(password_fd), "ssh", *ssh_args,
            "-o", "ControlMaster=no", "-o", "ControlPath=none", "-o", "ControlPersist=no",
            "-o", "PubkeyAuthentication=no", "-o", "PasswordAuthentication=yes",
            "-o", "PreferredAuthentications=password,keyboard-interactive",
            "-o", "NumberOfPasswordPrompts=1", "-o", "ConnectTimeout=8",
            "-p", str(node["ssh_port"]), f"{node['ssh_user']}@{node['address']}", "/usr/bin/true",
        ]

    def verify_ssh_password(self, node, password):
        known_hosts = None
        password_read = password_write = None
        try:
            known_hosts = self.password_test_known_hosts(node)
            password_read, password_write = os.pipe()
            process = subprocess.Popen(
                self.password_test_command(node, password_read, known_hosts),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                pass_fds=(password_read,),
            )
            os.close(password_read)
            password_read = None
            try:
                os.write(password_write, (password + "\n").encode())
            except BrokenPipeError:
                pass
            os.close(password_write)
            password_write = None
            return process.wait() == 0, "Password authentication succeeded."
        except OSError:
            return False, "Password authentication could not be started by the controller."
        finally:
            for descriptor in (password_read, password_write):
                if descriptor is not None:
                    os.close(descriptor)
            if known_hosts:
                Path(known_hosts).unlink(missing_ok=True)

    def test_ssh_password(self, node, password):
        authenticated, message = self.verify_ssh_password(node, password)
        if authenticated:
            return True, message
        if message != "Password authentication could not be started by the controller.":
            return False, "Password authentication failed. Check the SSH user, password, host key, and host reachability."
        return False, message

    def enrollment_variables(self, node, key, password=None, install_controller_key=True):
        known_hosts = self._known_hosts_path([node["id"]], include_legacy=False)
        values = {
            "controller_public_key": key["public_key"],
            "controller_key_path": self._managed_key_path(key),
            "controller_known_hosts_path": known_hosts if host_key_validation_enabled(node) else "/dev/null",
            "controller_ssh_host_key_checking": "yes" if host_key_validation_enabled(node) else "no",
            "controller_ssh_user": node["ssh_user"],
            "controller_address": node["address"],
            "controller_ssh_port": node["ssh_port"],
            "install_controller_key": bool(install_controller_key),
        }
        if password is not None:
            values["ansible_password"] = password
        return values

    @staticmethod
    def enrollment_context(node_id, enabled, key_id="", install_controller_key=False, existing_key=False, auto_name=False, username=""):
        return {
            "enrollment_node_id": node_id, "enrollment_enabled": bool(enabled), "enrollment_key_id": key_id,
            "enrollment_install_key": bool(install_controller_key), "enrollment_existing_key": bool(existing_key),
            "enrollment_auto_name": bool(auto_name), "enrollment_username": username,
        }

    def launch_password_enrollment(self, node, password, install_controller_key, username, auto_name=False):
        key = self._enrollment_key()
        if install_controller_key and not key:
            raise HTTPException(409, "Generate or import a controller-owned SSH key before password bootstrap")
        if install_controller_key:
            variables = self.enrollment_variables(node, key, password, True)
            context = self.enrollment_context(node["id"], node["enabled"], key["key_id"], True, auto_name=auto_name, username=username)
        else:
            variables = {"ansible_password": password, "install_controller_key": False}
            context = self.enrollment_context(node["id"], False, auto_name=auto_name, username=username)
        run_id = self._launch(
            "host-enroll", node["name"],
            lambda inv, variables_path: ansible_playbook(inv, self._playbooks / "host-bootstrap-key.yml", node["name"], self._active_key_path(), variables_path),
            variables=variables, context=context, inventory_nodes=[node["id"]], password_bootstrap=True, pinned_host_key_only=True,
        )
        self._audit(username, "host_password_bootstrap", str(node["id"]), "controller key installation requested")
        return run_id

    def launch_key_enrollment_probe(self, node, username, auto_name=False):
        key = self._enrollment_key()
        if not key:
            raise HTTPException(409, "No controller-owned SSH key is configured")
        key_path = self._managed_key_path(key)
        return self._launch(
            "host-enroll", node["name"],
            lambda inv, _variables: ["ansible", node["name"], "-i", str(inv), "-m", "shell", "-a", "hostname -s | sed 's/^/ECP_HOSTNAME=/'", "-o", "--private-key", key_path],
            context=self.enrollment_context(node["id"], node["enabled"], key["key_id"], True, True, auto_name, username),
            inventory_nodes=[node["id"]], private_key=key_path, pinned_host_key_only=True,
        )
