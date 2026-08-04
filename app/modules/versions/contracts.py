"""Pure version parsing and image naming shared by API and workers."""

from __future__ import annotations

import calendar
import re
import time


VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
ROLE_IMAGES = {
    "master": "elasticsearch/elasticsearch", "hot": "elasticsearch/elasticsearch", "warm": "elasticsearch/elasticsearch",
    "ml": "elasticsearch/elasticsearch", "ingest": "elasticsearch/elasticsearch", "coordinating": "elasticsearch/elasticsearch",
    "kibana": "kibana/kibana", "fleet-server": "beats/elastic-agent", "elastic-agent": "beats/elastic-agent", "logstash": "logstash/logstash",
}


def version_key(value: str | None):
    match = VERSION_RE.fullmatch(str(value or ""))
    return tuple(map(int, match.groups())) if match else None


def image_version(image: str | None) -> str:
    value = str(image or "")
    tag = value.rsplit(":", 1)[-1] if ":" in value.rsplit("/", 1)[-1] else ""
    return tag if version_key(tag) else ""


def image_for_role(role: str, version: str) -> str:
    return f"docker.elastic.co/{ROLE_IMAGES[role]}:{version}"


def observation_is_fresh(observation: dict | None, max_age: int = 900) -> bool:
    try:
        observed = time.strptime(observation["observed_at"], "%Y-%m-%d %H:%M:%S")
        return time.time() - calendar.timegm(observed) <= max_age
    except (KeyError, TypeError, ValueError):
        return False
