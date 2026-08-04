"""Public orchestration gateway contracts."""

from .contracts import (
    CommandSpec,
    ExecutionReceipt,
    ExecutionStatus,
    OrchestrationGateway,
    ansible_module,
    ansible_playbook,
    redacted_command,
)
from .service import LocalCommandGateway, command_spec
from .streaming import stream_command
from .adapters import (
    ElasticsearchGateway,
    ElasticsearchRequest,
    PodmanGateway,
    PodmanRequest,
    RemoteFileGateway,
    RemoteFileRequest,
    SshGateway,
    SshRequest,
    ScpRemoteFileGateway,
    SubprocessPodmanGateway,
    SubprocessSshGateway,
    UrllibElasticsearchGateway,
)

__all__ = [
    "CommandSpec",
    "ExecutionReceipt",
    "ExecutionStatus",
    "LocalCommandGateway",
    "OrchestrationGateway",
    "ansible_module",
    "ansible_playbook",
    "command_spec",
    "stream_command",
    "redacted_command",
    "ElasticsearchGateway",
    "ElasticsearchRequest",
    "PodmanGateway",
    "PodmanRequest",
    "RemoteFileGateway",
    "RemoteFileRequest",
    "SshGateway",
    "SshRequest",
    "ScpRemoteFileGateway",
    "SubprocessPodmanGateway",
    "SubprocessSshGateway",
    "UrllibElasticsearchGateway",
]
