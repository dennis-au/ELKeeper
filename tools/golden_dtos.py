#!/usr/bin/env python3
"""Redacted API golden-response fixture contracts for Phase 0.

Fixtures are compatibility snapshots only. They are not loaded by the
application at runtime and contain representative, non-secret DTO shapes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "golden"
SECRET_KEY_PARTS = (
    "password",
    "token",
    "secret",
    "private_key",
    "api_key",
    "apikey",
    "credential",
    "passphrase",
)

# Required top-level response types keep fixture drift visible without
# duplicating every field in the live API models during extraction.
GOLDEN_DTO_CONTRACTS: dict[str, dict[str, str]] = {
    "clusters": {"route": "GET /api/clusters", "response_type": "list"},
    "nodes": {"route": "GET /api/nodes", "response_type": "list"},
    "roles": {"route": "GET /api/health", "response_type": "object"},
    "versions": {"route": "GET /api/clusters/{cluster_id}/versions", "response_type": "object"},
    "topology": {"route": "GET /api/clusters/{cluster_id}/topology", "response_type": "object"},
    "dashboard": {"route": "GET /api/dashboard/snapshot", "response_type": "object"},
    "sensitive-items": {"route": "GET /api/clusters/{cluster_id}/sensitive-items", "response_type": "object"},
    "maintenance-plans": {"route": "GET /api/maintenance/plans/{plan_id}", "response_type": "object"},
    "runs": {"route": "GET /api/runs", "response_type": "list"},
}


def redact_payload(value: Any, *, key: str = "") -> Any:
    """Return a recursively redacted payload suitable for fixture generation."""

    if any(part in key.lower() for part in SECRET_KEY_PARTS):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(name): redact_payload(item, key=str(name)) for name, item in value.items()}
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    return value


def stable_json(value: Any) -> str:
    """Serialize a fixture with deterministic key and array formatting."""

    return json.dumps(redact_payload(value), indent=2, sort_keys=True) + "\n"


def fixture_path(name: str, root: Path = FIXTURE_ROOT) -> Path:
    if name not in GOLDEN_DTO_CONTRACTS:
        raise KeyError(f"Unknown golden DTO fixture: {name}")
    return root / f"{name}.json"


def load_fixture(name: str, root: Path = FIXTURE_ROOT) -> dict[str, Any]:
    path = fixture_path(name, root)
    payload = json.loads(path.read_text(encoding="utf-8"))
    validate_fixture(name, payload)
    return payload


def validate_fixture(name: str, fixture: Mapping[str, Any]) -> None:
    """Validate metadata, response type, and absence of secret-shaped keys."""

    contract = GOLDEN_DTO_CONTRACTS.get(name)
    if contract is None:
        raise ValueError(f"Unknown golden DTO fixture: {name}")
    if fixture.get("route") != contract["route"]:
        raise ValueError(f"Fixture {name!r} route does not match its contract")
    response = fixture.get("response")
    response_type = "list" if isinstance(response, list) else "object" if isinstance(response, Mapping) else "other"
    if response_type != contract["response_type"]:
        raise ValueError(f"Fixture {name!r} has response type {response_type!r}")

    def walk(value: Any, key: str = "") -> None:
        if any(part in key.lower() for part in SECRET_KEY_PARTS):
            raise ValueError(f"Fixture {name!r} contains secret-shaped field {key!r}")
        if isinstance(value, Mapping):
            for child_key, child_value in value.items():
                walk(child_value, str(child_key))
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(fixture)


def load_all_fixtures(root: Path = FIXTURE_ROOT) -> dict[str, dict[str, Any]]:
    return {name: load_fixture(name, root) for name in GOLDEN_DTO_CONTRACTS}
