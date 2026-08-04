"""Public host-management contracts."""

from .contracts import HostAddress, HostSpec, Node, NodeEnrollment, NodePasswordTest, NodeUpdate
from .repository import HostRepository
from .service import HostService, enabled_host
from .enrollment import enrollment_hostname, host_key_validation_enabled, ssh_host_key_args, unique_node_name
from .orchestration import HostEnrollmentOrchestrator
from .integration import HostLifecycleOperations, HostOperations
from .network import host_network_interfaces, parse_network_interfaces
from .remote import HostRemoteInspectionService, ssh_error_summary
from .storage import storage_mount_entries, storage_mount_eligibility, storage_mounts
from .http import build_batch_router, build_inventory_router, build_lifecycle_router, build_management_router, build_router

__all__ = [
    "HostAddress",
    "HostRepository",
    "HostService",
    "enabled_host",
    "HostSpec",
    "Node",
    "NodeEnrollment",
    "NodePasswordTest",
    "NodeUpdate",
    "HostEnrollmentOrchestrator",
    "HostOperations",
    "HostLifecycleOperations",
    "HostRemoteInspectionService",
    "ssh_error_summary",
    "storage_mount_entries",
    "storage_mount_eligibility",
    "storage_mounts",
    "host_network_interfaces",
    "parse_network_interfaces",
    "enrollment_hostname",
    "host_key_validation_enabled",
    "ssh_host_key_args",
    "unique_node_name",
    "build_router",
    "build_lifecycle_router",
    "build_batch_router",
    "build_inventory_router",
    "build_management_router",
]
