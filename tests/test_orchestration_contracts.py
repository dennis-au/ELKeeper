from pathlib import Path
import unittest
from unittest.mock import patch

from app.modules.orchestration import (
    ExecutionStatus,
    LocalCommandGateway,
    ansible_module,
    ansible_playbook,
    command_spec,
    redacted_command,
    stream_command,
    ElasticsearchRequest,
    PodmanRequest,
    RemoteFileRequest,
    SshRequest,
    ScpRemoteFileGateway,
    SubprocessPodmanGateway,
    SubprocessSshGateway,
    UrllibElasticsearchGateway,
)


class OrchestrationContractTests(unittest.TestCase):
    def test_streaming_executor_forwards_combined_output(self):
        output = []
        status = stream_command(
            ["python", "-c", "import sys; print('stdout'); print('stderr', file=sys.stderr)"], output.append
        )
        self.assertEqual(status, 0)
        self.assertEqual(sorted(line.strip() for line in output), ["stderr", "stdout"])

    def test_playbook_builder_preserves_stable_playbook_variables(self):
        command = ansible_playbook(
            Path("inventory"), Path("cluster-reconcile.yml"), "node-a", Path("controller.key"), Path("run.yaml")
        )
        self.assertEqual(
            command,
            [
                "ansible-playbook",
                "-i",
                "inventory",
                "cluster-reconcile.yml",
                "--limit",
                "node-a",
                "--private-key",
                "controller.key",
                "--extra-vars",
                "@run.yaml",
            ],
        )

    def test_module_builder_and_redaction_contract(self):
        command = ansible_module(Path("inventory"), "node-a", "shell", "echo ok", Path("key"))
        self.assertEqual(command[0], "ansible")
        self.assertEqual(command[5], "shell")
        self.assertEqual(redacted_command(["tool", "--password", "do-not-log"])[2], "[REDACTED]")

    def test_local_gateway_reports_success_and_cleans_registered_paths(self):
        gateway = LocalCommandGateway()
        receipt = gateway.execute(command_spec(["python", "-c", "print('ok')"]))
        self.assertEqual(receipt.status, ExecutionStatus.SUCCEEDED)
        self.assertIn("ok", receipt.stdout)

    def test_local_gateway_reports_timeout_and_start_failure(self):
        gateway = LocalCommandGateway()
        timeout = gateway.execute(command_spec(["python", "-c", "import time; time.sleep(1)"]), timeout=0.01)
        self.assertEqual(timeout.status, ExecutionStatus.TIMED_OUT)
        failed = gateway.execute(command_spec(["definitely-not-a-command"]))
        self.assertEqual(failed.status, ExecutionStatus.FAILED)

    def test_command_spec_rejects_empty_commands(self):
        with self.assertRaises(ValueError):
            command_spec([])

    def test_provider_adapter_requests_are_typed_and_secret_free_by_default(self):
        ssh = SshRequest("192.0.2.10", "root", ("true",))
        podman = PodmanRequest("host-a", "inspect", "ecp-demo")
        elastic = ElasticsearchRequest("https://198.51.100.2:9200", "GET", "/_cluster/health", ca_path="/run/ca.crt")
        remote = RemoteFileRequest("host-a", "/etc/elastic-control/config", mode=0o600)
        self.assertEqual(ssh.port, 22)
        self.assertEqual(podman.arguments, ())
        self.assertIsNone(elastic.payload)
        self.assertEqual(remote.mode, 0o600)

    def test_elasticsearch_adapter_requires_ca_verified_https(self):
        with self.assertRaises(ValueError):
            ElasticsearchRequest("http://198.51.100.2:9200", "GET", "/_cluster/health", ca_path="/run/ca.crt")
        with self.assertRaises(ValueError):
            ElasticsearchRequest("https://198.51.100.2:9200", "GET", "/_cluster/health")

    def test_ssh_adapter_builds_pinned_argv_without_shell_interpolation(self):
        seen = []

        def executor(spec, *, timeout=None):
            seen.append((spec, timeout))
            return __import__("app.modules.orchestration", fromlist=["ExecutionReceipt"]).ExecutionReceipt(
                status=ExecutionStatus.SUCCEEDED
            )

        gateway = SubprocessSshGateway(executor)
        gateway.run(SshRequest("192.0.2.10", "root", ("podman", "ps"), host_key_file="known_hosts"), timeout=3)
        self.assertEqual(
            seen[0][0].argv,
            (
                "ssh", "-p", "22", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                "-o", "UserKnownHostsFile=known_hosts", "root@192.0.2.10", "podman", "ps",
            ),
        )
        with self.assertRaises(ValueError):
            SshRequest("node-a.example", "root", ("true",))

    def test_podman_adapter_uses_remote_ssh_socket_not_tcp(self):
        seen = []

        def executor(spec, *, timeout=None):
            seen.append(spec.argv)
            return __import__("app.modules.orchestration", fromlist=["ExecutionReceipt"]).ExecutionReceipt(
                status=ExecutionStatus.SUCCEEDED
            )

        SubprocessPodmanGateway(executor).execute(PodmanRequest("192.0.2.10", "inspect", "ecp-demo"))
        self.assertIn("ssh://192.0.2.10/run/podman/podman.sock", seen[0])
        self.assertNotIn("tcp://", " ".join(seen[0]))

    def test_elasticsearch_adapter_uses_ca_context_and_returns_http_errors(self):
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return b'{"status":"green"}'

        with patch("app.modules.orchestration.adapters.ssl.create_default_context") as make_context:
            gateway = UrllibElasticsearchGateway(lambda request, **kwargs: Response())
            receipt = gateway.request(
                ElasticsearchRequest("https://198.51.100.2:9200", "GET", "/_cluster/health", ca_path="ca.pem")
            )
        self.assertTrue(receipt.succeeded)
        self.assertIn("green", receipt.stdout)
        self.assertEqual(make_context.call_args.kwargs["cafile"], "ca.pem")

    def test_scp_adapter_cleans_temporary_upload_and_reads_without_shell(self):
        seen = []

        def executor(spec, *, timeout=None):
            seen.append(spec)
            return __import__("app.modules.orchestration", fromlist=["ExecutionReceipt"]).ExecutionReceipt(
                status=ExecutionStatus.SUCCEEDED
            )

        gateway = ScpRemoteFileGateway(executor=executor)
        gateway.put(RemoteFileRequest("192.0.2.10", "/etc/elastic-control/config", b"safe"))
        self.assertEqual(seen[0].argv[0], "scp")
        self.assertFalse(Path(seen[0].temporary_paths[0]).exists())
        self.assertEqual(seen[1].argv[-4:], ("chmod", "600", "--", "/etc/elastic-control/config"))
        gateway.get("192.0.2.10", "/etc/elastic-control/config")
        self.assertEqual(seen[2].argv[-4:], ("root@192.0.2.10", "cat", "--", "/etc/elastic-control/config"))
