"""Controller SSH-identity lifecycle service using public host contracts."""

from __future__ import annotations

import sqlite3
from typing import Callable

from fastapi import HTTPException

from app.modules.hosts import HostRepository

from .repository import ControllerIdentityRepository


class ControllerIdentityService:
    def __init__(self, db_factory: Callable, *, seal_secret: Callable[[str], str]):
        self._db = db_factory
        self._seal_secret = seal_secret

    def active_and_candidate(self):
        return ControllerIdentityRepository(self._db).active_and_candidate()

    def stage(self, *, private_value: str, public_key: str, key_id: str, algorithm: str, source: str):
        with self._db() as connection:
            keys = ControllerIdentityRepository.from_connection(connection)
            hosts = HostRepository.from_connection(connection)
            retired_candidates = keys.candidate_key_ids_in_connection(connection)
            installed = hosts.candidate_key_installation_names_in_connection(connection, retired_candidates)
            if installed:
                raise HTTPException(
                    409,
                    "The current candidate is installed on hosts; activate it or revoke it before staging another key: "
                    + ", ".join(installed),
                )
            keys.retire_candidates_in_connection(connection)
            state = "candidate" if hosts.enabled_count_in_connection(connection) else "active"
            if state == "active":
                keys.retire_active_in_connection(connection)
            try:
                row = keys.create_in_connection(
                    connection,
                    key_id=key_id,
                    algorithm=algorithm,
                    public_key=public_key,
                    private_key_encrypted=self._seal_secret(private_value),
                    source=source,
                    state=state,
                )
            except sqlite3.IntegrityError as error:
                raise HTTPException(409, "That SSH key is already registered with the controller") from error
        return row, retired_candidates

    def candidate_activation(self):
        with self._db() as connection:
            keys = ControllerIdentityRepository.from_connection(connection)
            hosts = HostRepository.from_connection(connection)
            active, candidate = keys.active_and_candidate_in_connection(connection)
            if not candidate:
                raise HTTPException(409, "No controller SSH key is staged for activation")
            missing = hosts.missing_candidate_key_names_in_connection(connection, candidate["key_id"])
        if missing:
            raise HTTPException(
                409,
                "Install and verify the candidate key on every enabled host: " + ", ".join(missing),
            )
        return active, candidate

    def activate(self, active, candidate) -> None:
        with self._db() as connection:
            keys = ControllerIdentityRepository.from_connection(connection)
            hosts = HostRepository.from_connection(connection)
            if active:
                keys.retire_by_id_in_connection(connection, active["id"])
            keys.retire_by_id_in_connection(connection, candidate["id"])
            keys.activate_by_id_in_connection(connection, candidate["id"])
            hosts.activate_candidate_key_in_connection(connection, candidate["key_id"])
