"""Runtime version probe and image-download contracts."""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable


class VersionRuntimeService:
    def __init__(
        self,
        *,
        ansible: Callable,
        workload_name: Callable,
        image_for_role: Callable,
        image_version: Callable,
        default_stack_version: str,
        repository_factory: Callable,
    ):
        self._ansible = ansible
        self._workload_name = workload_name
        self._image_for_role = image_for_role
        self._image_version = image_version
        self._default_stack_version = default_stack_version
        self._repository_factory = repository_factory

    def probe_command(self, inventory, cluster: dict, assignment: dict) -> list[str]:
        workload = self._workload_name(cluster, assignment)
        filebeat_workload = workload + "-filebeat"
        expected_image = self._image_for_role(
            assignment["role"], assignment.get("image_version") or self._default_stack_version
        )
        filebeat_enabled = int(bool(cluster.get("log_monitoring", {}).get("filebeat_enabled")))
        script = (
            f"name={shlex.quote(workload)}; "
            f"filebeat_name={shlex.quote(filebeat_workload)}; "
            f"assignment_id={assignment['id']}; "
            f"expected={shlex.quote(expected_image)}; "
            f"filebeat_enabled={filebeat_enabled}; "
            "if [[ \"$filebeat_enabled\" == 0 ]]; then filebeat_state=disabled; "
            "elif podman container exists \"$filebeat_name\" && [[ $(podman inspect --format '{{{{.State.Running}}}}' \"$filebeat_name\") == true ]]; "
            "then filebeat_state=running; else filebeat_state=degraded; fi; "
            "if ! podman container exists \"$name\"; then if podman image exists \"$expected\"; then cached=1; else cached=0; fi; "
            "printf 'ECP_VERSION=%s|0|%s||%s|%s\\n' \"$assignment_id\" \"$expected\" \"$cached\" \"$filebeat_state\"; exit 0; fi; "
            "podman inspect \"$name\" | python3 -c 'import json,sys; value=json.load(sys.stdin)[0]; "
            "print(\"ECP_VERSION=%s|%s|%s|%s|1|%s\" % (sys.argv[1], \"1\" if value[\"State\"][\"Running\"] else \"0\", "
            "value[\"Config\"][\"Image\"], value[\"Image\"], sys.argv[2]))' \"$assignment_id\" \"$filebeat_state\""
        )
        return self._ansible(inventory, assignment["node_name"], "shell", script)

    def record_observation(self, metadata: dict, output: str, succeeded: bool) -> None:
        match = re.search(
            r"ECP_VERSION=(\d+)\|([01])\|([^|\r\n]*)\|([^|\r\n]*)(?:\|([01]))?(?:\|([a-z_]+))?",
            output,
        )
        assignment_id = metadata["assignment_id"]
        if match:
            _, running, image, digest, cached, filebeat_state = match.groups()
            version = self._image_version(image)
            cached = cached or "0"
            error = "" if succeeded else "Version probe command failed"
        else:
            image = digest = version = ""
            running = cached = "0"
            filebeat_state = None
            error = "Version probe did not return workload details"
        self._repository_factory().record_runtime(
            assignment_id,
            image=image,
            digest=digest,
            version=version,
            running=running == "1",
            cached=cached == "1",
            error=error,
            filebeat_state=filebeat_state,
            filebeat_error="" if filebeat_state in {"running", "disabled"} else "Filebeat companion is not running",
        )

    def download_command(self, inventory, node_name: str, image: str) -> list[str]:
        script = (
            f"image={shlex.quote(image)}; "
            "if podman image exists \"$image\"; then echo \"ECP_IMAGE_CACHED=$image\"; "
            "else podman pull \"$image\"; echo \"ECP_IMAGE_PULLED=$image\"; fi; "
            "podman image inspect \"$image\" | python3 -c 'import json,sys; print(json.load(sys.stdin)[0].get(\"Digest\", \"\"))'"
        )
        return self._ansible(inventory, node_name, "shell", script)
