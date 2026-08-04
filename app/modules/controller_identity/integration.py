"""Controller-identity operation facade used by application assembly."""

from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Callable

from fastapi import HTTPException
from cryptography.hazmat.primitives import serialization

from app.modules.hosts import HostRepository

from .keys import key_material
from .repository import ControllerIdentityRepository
from .service import ControllerIdentityService


class ControllerIdentityOperations:
    """Own key lifecycle projections and managed key-file handling."""

    def __init__(
        self,
        *,
        db_factory: Callable,
        runtime_dir: Path,
        legacy_key_path: str,
        legacy_known_hosts_path: str,
        seal_secret: Callable[[str], str],
        open_secret: Callable[[str], str],
        audit: Callable[[str, str, str, str], None],
    ) -> None:
        self._db = db_factory
        self._runtime = runtime_dir
        self._legacy_key = legacy_key_path
        self._legacy_known_hosts = legacy_known_hosts_path
        self._seal_secret = seal_secret
        self._open_secret = open_secret
        self._audit = audit

    @staticmethod
    def key_metadata(row):
        if not row:
            return None
        return {
            "key_id": row["key_id"],
            "algorithm": row["algorithm"],
            "public_key": row["public_key"],
            "source": row["source"],
            "state": row["state"],
            "created_at": row["created_at"],
        }

    def legacy_key_metadata(self):
        try:
            private_key = serialization.load_ssh_private_key(Path(self._legacy_key).read_bytes(), password=None)
            _, public_key, key_id, algorithm = key_material(private_key)
            return {
                "key_id": key_id,
                "algorithm": algorithm,
                "public_key": public_key,
                "source": "legacy_mounted",
                "state": "legacy",
                "created_at": None,
            }
        except (FileNotFoundError, PermissionError, TypeError, ValueError):
            return {
                "key_id": "",
                "algorithm": "unknown",
                "public_key": "",
                "source": "legacy_mounted",
                "state": "legacy",
                "created_at": None,
            }

    def key_rows(self):
        return ControllerIdentityRepository(self._db).active_and_candidate()

    def managed_key_path(self, row):
        self._runtime.mkdir(parents=True, exist_ok=True)
        safe_key_id = re.sub(r"[^A-Za-z0-9._-]", "_", row["key_id"])
        path = self._runtime / f"{safe_key_id}.key"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(self._open_secret(row["private_key_encrypted"]), encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(path)
        os.chmod(path, 0o600)
        return str(path)

    def remove_managed_key_path(self, key_id: str) -> None:
        safe_key_id = re.sub(r"[^A-Za-z0-9._-]", "_", key_id)
        (self._runtime / f"{safe_key_id}.key").unlink(missing_ok=True)

    def active_ssh_key_path(self):
        active, _ = self.key_rows()
        return self.managed_key_path(active) if active else self._legacy_key

    def enrollment_key_row(self):
        active, candidate = self.key_rows()
        return candidate or active

    def status(self):
        active, candidate = self.key_rows()
        return {
            "active": self.key_metadata(active) if active else self.legacy_key_metadata(),
            "candidate": self.key_metadata(candidate),
            "managed": bool(active),
        }

    def stage(self, private_key, source):
        private_value, public_key, key_id, algorithm = key_material(private_key)
        row, retired_candidates = ControllerIdentityService(
            self._db,
            seal_secret=self._seal_secret,
        ).stage(
            private_value=private_value,
            public_key=public_key,
            key_id=key_id,
            algorithm=algorithm,
            source=source,
        )
        for retired_key_id in retired_candidates:
            self.remove_managed_key_path(retired_key_id)
        return self.key_metadata(row)

    def candidate_activation(self):
        return ControllerIdentityService(self._db, seal_secret=self._seal_secret).candidate_activation()

    def activate(self, active, candidate, username):
        ControllerIdentityService(self._db, seal_secret=self._seal_secret).activate(active, candidate)
        if active:
            self.remove_managed_key_path(active["key_id"])
        self._audit(username, "controller_ssh_key_activated", candidate["key_id"], "candidate activated after host verification")

    def known_hosts_path(self, node_ids=None, include_legacy=True):
        self._runtime.mkdir(parents=True, exist_ok=True)
        lines = []
        if include_legacy:
            try:
                legacy = Path(self._legacy_known_hosts)
                if legacy.exists():
                    lines.extend(line for line in legacy.read_text(encoding="utf-8").splitlines() if line.strip())
            except (OSError, UnicodeDecodeError):
                pass
        with self._db() as connection:
            rows = HostRepository.from_connection(connection).pinned_host_keys_in_connection(connection, node_ids)
        for row in rows:
            address = row["address"]
            if any(char.isspace() for char in address):
                continue
            host = address if row["ssh_port"] == 22 else f"[{address}]:{row['ssh_port']}"
            lines.append(f"{host} {row['ssh_host_key']}")
        if not lines and include_legacy and not node_ids:
            return self._legacy_known_hosts
        suffix = "all" if not node_ids else "-".join(str(node_id) for node_id in sorted(node_ids))
        path = self._runtime / f"managed_nodes_known_hosts-{suffix}"
        temporary = path.with_suffix(".tmp")
        temporary.write_text("\n".join(dict.fromkeys(lines)) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(path)
        os.chmod(path, 0o600)
        return str(path)


__all__ = ["ControllerIdentityOperations"]
