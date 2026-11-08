"""Public traffic API request and response models, also used for OpenAPI."""
import math
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Action = Literal["SET_WEIGHTS", "ROLLBACK", "RECONCILE"]
Identifier = Annotated[str, Field(min_length=1, max_length=128)]
Weight = Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]
Weights = dict[str, Weight]


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expectedRevision: Annotated[int, Field(ge=0, strict=True)]
    migrationId: Identifier | None = None
    transitionId: Identifier | None = Field(default=None, description="Requires migrationId.")

    @model_validator(mode="after")
    def validate_scope(self):
        if self.transitionId and not self.migrationId:
            raise ValueError("transitionId requires migrationId.")
        return self


class SetWeights(Command):
    action: Literal["SET_WEIGHTS"]
    weights: Annotated[Weights, Field(min_length=1, description="All configured pools; total must be 100.")]
    unknownOperationId: None = None
    executorStoppedRef: None = None

    @field_validator("weights")
    @classmethod
    def validate_total(cls, weights):
        if not math.isclose(sum(weights.values()), 100, abs_tol=0.000001):
            raise ValueError("weights 합계는 100이어야 합니다.")
        return weights


class Rollback(Command):
    action: Literal["ROLLBACK"]
    weights: None = None
    unknownOperationId: None = None
    executorStoppedRef: None = None


class Reconcile(Command):
    action: Literal["RECONCILE"]
    weights: None = None
    unknownOperationId: Annotated[str, Field(min_length=1)]
    executorStoppedRef: Annotated[str, Field(min_length=1, max_length=256, pattern=r"\S")]


class Status(BaseModel):
    revision: int
    unknownOperationIds: list[str]
    weights: Weights
    pool_health: dict[str, dict]
    last_changed_at: str | None
    observedAt: str | None = None


class Accepted(BaseModel):
    operationId: str
    action: Action
    status: Literal["QUEUED"]
    statusUrl: str


class ChangeResult(BaseModel):
    weights: Weights
    traffic_verified: Literal[False]


class RecoveryResult(ChangeResult):
    disposition: Literal["APPLIED", "NO_CHANGE"]


class Operation(BaseModel):
    startedAt: str | None = None
    finishedAt: str | None = None
    operationId: str
    action: Action
    status: Literal["QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "UNKNOWN"]
    beforeWeights: Weights | None
    afterWeights: Weights | None
    migrationId: str | None
    transitionId: str | None
    result: RecoveryResult | ChangeResult | None
    error: str | None


class Error(BaseModel):
    code: str
    message: str
    operationId: None
    blockers: list[str]
