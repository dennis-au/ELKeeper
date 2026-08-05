"""Workload-owned validation and resource policy."""

from __future__ import annotations

import re
from collections.abc import Mapping

from fastapi import HTTPException


class WorkloadPolicyService:
    def __init__(self, *, role_specs: Mapping[str, Mapping[str, object]], path_blocklist: tuple[str, ...], cpu_pattern: re.Pattern[str], memory_pattern: re.Pattern[str]):
        self._role_specs = role_specs
        self._path_blocklist = path_blocklist
        self._cpu_pattern = cpu_pattern
        self._memory_pattern = memory_pattern

    def valid_storage_path(self, value: str) -> bool:
        if not value.startswith("/") or value == "/" or ":" in value or any(char.isspace() for char in value):
            return False
        return not any(value == path or value.startswith(path + "/") for path in self._path_blocklist)

    @staticmethod
    def memory_mebibytes(value: str) -> int:
        number = float(value[:-1])
        unit = value[-1].lower()
        return int(number * {"k": 1 / 1024, "m": 1, "g": 1024, "t": 1024 * 1024}[unit])

    def validate_config(self, role: str, config: Mapping[str, object]) -> None:
        if role not in self._role_specs:
            raise HTTPException(422, "Unsupported role")
        for key in ("cpu", "memory", "storage_path"):
            if not config.get(key):
                raise HTTPException(422, f"{key} is required")
        if not self._cpu_pattern.fullmatch(str(config["cpu"])) or float(str(config["cpu"])) <= 0:
            raise HTTPException(422, "CPU must be a positive core value")
        if not self._memory_pattern.fullmatch(str(config["memory"])):
            raise HTTPException(422, "Memory must be a positive size such as 4g")
        if not self.valid_storage_path(str(config["storage_path"])):
            raise HTTPException(422, "Storage path must be a safe absolute non-system path")
        if role in {"master", "hot", "warm", "ml", "ingest", "coordinating"} and self.memory_mebibytes(str(config["memory"])) < 2048:
            raise HTTPException(422, "Elasticsearch workloads require at least 2g of memory")
        memory_mib = self.memory_mebibytes(str(config["memory"]))
        jvm_heap = str(config.get("jvm_heap", "")).strip()
        node_heap = str(config.get("node_heap", "")).strip()
        if jvm_heap:
            if role not in {"master", "hot", "warm", "ml", "ingest", "coordinating", "logstash"}:
                raise HTTPException(422, "JVM heap is supported only by Elasticsearch and Logstash workloads")
            if not self._memory_pattern.fullmatch(jvm_heap):
                raise HTTPException(422, "JVM heap must be a positive size such as 8g")
            heap_mib = self.memory_mebibytes(jvm_heap)
            if heap_mib < 512:
                raise HTTPException(422, "JVM heap must be at least 512m")
            if heap_mib > memory_mib // 2:
                raise HTTPException(422, "JVM heap may not exceed 50% of the container memory limit")
        if node_heap:
            if role != "kibana":
                raise HTTPException(422, "Node.js heap is supported only by Kibana")
            if not self._memory_pattern.fullmatch(node_heap):
                raise HTTPException(422, "Node.js heap must be a positive size such as 12g")
            heap_mib = self.memory_mebibytes(node_heap)
            if heap_mib < 512:
                raise HTTPException(422, "Node.js heap must be at least 512m")
            if heap_mib > memory_mib * 3 // 4:
                raise HTTPException(422, "Node.js heap may not exceed 75% of the container memory limit")
        if role == "logstash" and not str(config.get("pipeline", "")).strip():
            raise HTTPException(422, "A Logstash pipeline is required")


__all__ = ["WorkloadPolicyService"]
