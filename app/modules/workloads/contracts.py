"""Workload mutation request DTOs and batch-shape validation."""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class AssignmentInput(BaseModel):
    node_id: int = Field(ge=1)
    role: str
    config: dict = Field(default_factory=dict)


class ResourceInput(BaseModel):
    cpu: str = Field(min_length=1, max_length=32)
    memory: str = Field(min_length=2, max_length=32)
    storage_path: str = Field(min_length=2, max_length=512)


class Targets(BaseModel):
    """Host selection for the workload batch endpoint."""

    node_ids: list[int] = Field(min_length=1)


class WorkloadChange(BaseModel):
    client_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    kind: str = Field(pattern=r"^(create|resources|detach)$")
    assignment_id: int | None = Field(default=None, ge=1)
    expected_revision: int | None = Field(default=None, ge=1)
    node_id: int | None = Field(default=None, ge=1)
    role: str | None = None
    image_version: str | None = Field(default=None, pattern=r"^\d+\.\d+\.\d+$")
    config: dict | None = None

    @model_validator(mode="after")
    def validate_shape(self):
        if self.kind == "create":
            if not self.node_id or not self.role or self.config is None:
                raise ValueError("A create change requires node_id, role, and config")
            if self.assignment_id or self.expected_revision:
                raise ValueError("A create change may not include an assignment revision")
        else:
            if not self.assignment_id or not self.expected_revision:
                raise ValueError("An existing workload change requires assignment_id and expected_revision")
            if self.node_id or self.role:
                raise ValueError("An existing workload change may not include node_id or role")
            if self.image_version is not None:
                raise ValueError("An existing workload change may not include an image version")
            if self.kind == "resources" and self.config is None:
                raise ValueError("A resource change requires config")
            if self.kind == "detach" and self.config is not None:
                raise ValueError("A detach change may not include config")
        return self


class WorkloadChangeSet(BaseModel):
    changes: list[WorkloadChange] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_changes(self):
        client_ids = [change.client_id for change in self.changes]
        if len(client_ids) != len(set(client_ids)):
            raise ValueError("Each pending change needs a unique client_id")
        assignment_ids = [change.assignment_id for change in self.changes if change.assignment_id]
        if len(assignment_ids) != len(set(assignment_ids)):
            raise ValueError("A workload can only appear once in a pending change set")
        creates = [(change.node_id, change.role) for change in self.changes if change.kind == "create"]
        if len(creates) != len(set(creates)):
            raise ValueError("A role can only be created once on the same host")
        return self
