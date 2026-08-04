"""Cluster port-profile DTOs and pure allocation helpers."""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field, model_validator


DEFAULT_PORTS = {
    "elasticsearch_http": 9200,
    "elasticsearch_transport": 9300,
    "kibana": 5601,
    "fleet": 8220,
    "logstash_api": 9600,
}
PORT_FIELDS = tuple(DEFAULT_PORTS)
ELASTICSEARCH_ROLES = ("master", "hot", "warm", "ml", "ingest", "coordinating")
ROLE_PORT_OFFSETS = {role: index for index, role in enumerate(ELASTICSEARCH_ROLES)}
ROLE_SPECS = {
    "master": {"label": "Master", "ports": ("elasticsearch_http", "elasticsearch_transport")},
    "hot": {"label": "Hot data", "ports": ("elasticsearch_http", "elasticsearch_transport")},
    "warm": {"label": "Warm data", "ports": ("elasticsearch_http", "elasticsearch_transport")},
    "ml": {"label": "Machine learning", "ports": ("elasticsearch_http", "elasticsearch_transport")},
    "ingest": {"label": "Ingest", "ports": ("elasticsearch_http", "elasticsearch_transport")},
    "coordinating": {"label": "Coordinating", "ports": ("elasticsearch_http", "elasticsearch_transport")},
    "kibana": {"label": "Kibana", "ports": ("kibana",)},
    "fleet-server": {"label": "Fleet Server", "ports": ("fleet",)},
    "logstash": {"label": "Logstash", "ports": ("logstash_api",)},
    "elastic-agent": {"label": "Elastic Agent", "ports": ()},
}


class PortProfile(BaseModel):
    elasticsearch_http: int = DEFAULT_PORTS["elasticsearch_http"]
    elasticsearch_transport: int = DEFAULT_PORTS["elasticsearch_transport"]
    kibana: int = DEFAULT_PORTS["kibana"]
    fleet: int = DEFAULT_PORTS["fleet"]
    logstash_api: int = DEFAULT_PORTS["logstash_api"]

    @model_validator(mode="after")
    def unique_ports(self):
        values = [getattr(self, field) for field in PORT_FIELDS]
        if any(port < 1 or port > 65535 for port in values) or len(set(values)) != len(values):
            raise ValueError("Ports must be unique values from 1 through 65535")
        return self


def valid_port(value, fallback):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return number if 1 <= number <= 65535 else fallback


def next_available_port(preferred, used, fallback):
    for start in (valid_port(preferred, fallback), valid_port(fallback, 1), 1):
        for value in range(start, 65536):
            if value not in used:
                used.add(value)
                return value
    raise ValueError("No port is available for a role association")


def default_role_ports(legacy_ports=None):
    supplied = legacy_ports or {}
    legacy = {name: valid_port(supplied.get(name), value) for name, value in DEFAULT_PORTS.items()}
    result = {
        "kibana": {"kibana": legacy["kibana"]}, "fleet-server": {"fleet": legacy["fleet"]},
        "logstash": {"logstash_api": legacy["logstash_api"]}, "elastic-agent": {},
    }
    used = {legacy["kibana"], legacy["fleet"], legacy["logstash_api"]}
    for role, offset in ROLE_PORT_OFFSETS.items():
        result[role] = {
            "elasticsearch_http": next_available_port(legacy["elasticsearch_http"] + offset, used, 9200 + offset),
            "elasticsearch_transport": next_available_port(legacy["elasticsearch_transport"] + offset, used, 9300 + offset),
        }
    return result


class RolePortProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    master: dict[str, int] = Field(default_factory=lambda: default_role_ports()["master"])
    hot: dict[str, int] = Field(default_factory=lambda: default_role_ports()["hot"])
    warm: dict[str, int] = Field(default_factory=lambda: default_role_ports()["warm"])
    ml: dict[str, int] = Field(default_factory=lambda: default_role_ports()["ml"])
    ingest: dict[str, int] = Field(default_factory=lambda: default_role_ports()["ingest"])
    coordinating: dict[str, int] = Field(default_factory=lambda: default_role_ports()["coordinating"])
    kibana: dict[str, int] = Field(default_factory=lambda: default_role_ports()["kibana"])
    fleet_server: dict[str, int] = Field(default_factory=lambda: default_role_ports()["fleet-server"], alias="fleet-server")
    logstash: dict[str, int] = Field(default_factory=lambda: default_role_ports()["logstash"])
    elastic_agent: dict[str, int] = Field(default_factory=dict, alias="elastic-agent")

    @model_validator(mode="after")
    def valid_role_ports(self):
        profiles = self.model_dump(by_alias=True)
        seen = {}
        for role, spec in ROLE_SPECS.items():
            association = profiles[role]
            expected = set(spec["ports"])
            if set(association) != expected:
                raise ValueError(f"{spec['label']} must define exactly: {', '.join(spec['ports']) or 'no inbound ports'}")
            for port in association.values():
                if port < 1 or port > 65535:
                    raise ValueError("Role ports must be values from 1 through 65535")
                if port in seen:
                    raise ValueError(f"Role port {port} is assigned to both {seen[port]} and {spec['label']}")
                seen[port] = spec["label"]
        return self


def stored_role_ports(value, legacy_ports):
    try:
        parsed = json.loads(value or "{}")
        if parsed:
            return RolePortProfile.model_validate(parsed).model_dump(by_alias=True)
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    return default_role_ports(legacy_ports)


def role_port_values(role_ports, role):
    return [role_ports[role][field] for field in ROLE_SPECS[role]["ports"]]
