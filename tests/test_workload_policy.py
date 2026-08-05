import re
import unittest

from fastapi import HTTPException

from app.modules.workloads.policy import WorkloadPolicyService


class WorkloadPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = WorkloadPolicyService(
            role_specs={role: {} for role in ("master", "hot", "warm", "ml", "ingest", "coordinating", "kibana", "logstash")},
            path_blocklist=("/etc", "/usr"),
            cpu_pattern=re.compile(r"^[0-9]+(?:[.][0-9]+)?$"),
            memory_pattern=re.compile(r"^[1-9][0-9]*(?:[.][0-9]+)?[kKmMgGtT]$"),
        )

    @staticmethod
    def config(**overrides):
        return {"cpu": "2", "memory": "16g", "storage_path": "/srv/elastic/workload", **overrides}

    def test_elasticsearch_accepts_an_explicit_heap_up_to_half_of_container_memory(self):
        self.policy.validate_config("master", self.config(jvm_heap="8g"))

    def test_elasticsearch_rejects_a_heap_larger_than_half_of_container_memory(self):
        with self.assertRaisesRegex(HTTPException, "50%"):
            self.policy.validate_config("master", self.config(jvm_heap="12g"))

    def test_kibana_uses_node_heap_and_allows_twelve_gib_of_sixteen_gib(self):
        self.policy.validate_config("kibana", self.config(node_heap="12g"))

    def test_kibana_rejects_jvm_heap_and_node_heap_above_the_runtime_budget(self):
        with self.assertRaisesRegex(HTTPException, "JVM heap"):
            self.policy.validate_config("kibana", self.config(jvm_heap="8g"))
        with self.assertRaisesRegex(HTTPException, "75%"):
            self.policy.validate_config("kibana", self.config(node_heap="13g"))

    def test_logstash_accepts_a_jvm_heap_and_requires_a_pipeline(self):
        self.policy.validate_config("logstash", self.config(jvm_heap="8g", pipeline="input { stdin {} } output { stdout {} }"))
