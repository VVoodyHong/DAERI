"""Strict inputs for the implemented local task and approval tools."""

from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    model_validator,
)


type NonEmptyString = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]
type PositiveInt = Annotated[int, Field(ge=1)]
type NonNegativeInt = Annotated[int, Field(ge=0)]
type PageSize = Annotated[int, Field(ge=1, le=100)]


def unique_ids(values: list[str]) -> list[str]:
    if len(values) != len(set(values)):
        raise ValueError("IDs must not be repeated")
    return values


type UniqueIDs = Annotated[list[NonEmptyString], AfterValidator(unique_ids), Field(json_schema_extra={"uniqueItems": True})]
type JsonObject = dict[str, JsonValue]
type JsonPointer = Annotated[str, StringConstraints(pattern=r"^(?:/(?:[^~]|~[01])*)*$")]
type Scalar = str | int | float | bool | None


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


# Unvalidated None defaults allow omission while supplied JSON null stays invalid.
class ContextSelection(InputModel):
    profile_id: NonEmptyString = Field(default=None)
    department_id: NonEmptyString = Field(default=None)
    environment_id: NonEmptyString = Field(default=None)


class DesiredOutput(InputModel):
    format: NonEmptyString = Field(default=None)
    description: NonEmptyString = Field(default=None)


class TaskCreate(InputModel):
    request_id: NonEmptyString
    request_text: NonEmptyString
    context: ContextSelection = Field(default=None)
    desired_output: DesiredOutput = Field(default=None)


class TaskPatch(InputModel):
    model_config = ConfigDict(json_schema_extra={
        "minProperties": 1,
        "oneOf": [
            {"required": ["question"], "maxProperties": 1},
            {"not": {"required": ["question"]}},
        ],
    })

    question: NonEmptyString = Field(default=None)
    clarification: NonEmptyString = Field(default=None)
    context: ContextSelection = Field(default=None)
    desired_output: DesiredOutput = Field(default=None)

    @model_validator(mode="after")
    def check_patch(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("patch must contain at least one change")
        if "question" in self.model_fields_set and len(self.model_fields_set) != 1:
            raise ValueError("question cannot be combined with other changes")
        return self


class TaskUpdate(InputModel):
    model_config = ConfigDict(json_schema_extra={
        "oneOf": [{"required": ["patch"]}, {"required": ["action"]}],
    })

    request_id: NonEmptyString
    task_id: NonEmptyString
    expected_task_revision: PositiveInt
    patch: TaskPatch = Field(default=None)
    action: Literal["pause"] = Field(default=None)

    @model_validator(mode="after")
    def check_change_branch(self) -> Self:
        if len(self.model_fields_set & {"patch", "action"}) != 1:
            raise ValueError("exactly one of patch and action is required")
        return self


class TaskGet(InputModel):
    model_config = ConfigDict(json_schema_extra={
        "oneOf": [{"required": ["task_id"]}, {"required": ["profile_id"]}],
    })

    task_id: NonEmptyString = Field(default=None)
    profile_id: NonEmptyString = Field(default=None)
    cursor: NonEmptyString = Field(default=None)
    limit: PageSize = Field(default=None)

    @model_validator(mode="after")
    def check_lookup_target(self) -> Self:
        if len(self.model_fields_set & {"task_id", "profile_id"}) != 1:
            raise ValueError("exactly one of task_id and profile_id is required")
        return self


class TaskResume(InputModel):
    request_id: NonEmptyString
    task_id: NonEmptyString
    expected_task_revision: PositiveInt


class TaskCancel(InputModel):
    request_id: NonEmptyString
    task_id: NonEmptyString
    expected_task_revision: PositiveInt
    reason: NonEmptyString = Field(default=None)


class MemorySearch(InputModel):
    task_id: NonEmptyString
    query: NonEmptyString
    instance_ids: Annotated[UniqueIDs, Field(min_length=1)] = Field(default=None)
    limit: Annotated[int, Field(ge=1, le=50)] = Field(default=None)


class ResourceList(InputModel):
    task_id: NonEmptyString
    types: Annotated[UniqueIDs, Field(min_length=1)] = Field(default=None)
    cursor: NonEmptyString = Field(default=None)
    limit: PageSize = Field(default=None)


class ResourceLimit(InputModel):
    instance_id: NonEmptyString
    metric_id: NonEmptyString
    maximum: Annotated[int | float, Field(ge=0)]


class PlanBudget(InputModel):
    max_tool_calls: NonNegativeInt
    max_external_requests: NonNegativeInt | None
    max_runtime_ms: NonNegativeInt
    resource_limits: list[ResourceLimit]

    @model_validator(mode="after")
    def check_metric_pairs(self) -> Self:
        pairs = [(limit.instance_id, limit.metric_id) for limit in self.resource_limits]
        if len(pairs) != len(set(pairs)):
            raise ValueError("resource metric pairs must not be repeated")
        return self


class KeySelectionSegment(InputModel):
    key: str


class IndexSelectionSegment(InputModel):
    index: NonNegativeInt


class EachSelectionSegment(InputModel):
    each: Literal[True]

    @model_validator(mode="before")
    @classmethod
    def strict_true(cls, value):
        if isinstance(value, dict) and type(value.get("each")) is not bool:
            raise ValueError("each requires the JSON boolean true")
        return value


class FieldSelector(InputModel):
    segments: list[KeySelectionSegment | IndexSelectionSegment | EachSelectionSegment]


def unique_selectors(values):
    from .policy import validate_selectors
    from .storage import StoreError

    try:
        validate_selectors([item.model_dump() for item in values])
    except StoreError:
        raise ValueError("field selectors must be unique") from None
    return values


type Selectors = Annotated[list[FieldSelector], AfterValidator(unique_selectors), Field(json_schema_extra={"uniqueItems": True})]
type MemoryCategory = Literal["service_rule", "resource_structure", "procedure", "lesson"]


class MaskingRule(InputModel):
    field: FieldSelector
    action: Literal["omit", "redact"]


class MemoryRule(InputModel):
    categories: Annotated[list[MemoryCategory], AfterValidator(unique_ids), Field(json_schema_extra={"uniqueItems": True})]
    source_fields: Selectors
    context_sources: Annotated[list[Literal["request", "clarification"]], AfterValidator(unique_ids), Field(json_schema_extra={"uniqueItems": True})]
    retention: Literal["none", "source_bound", "long_term"]
    allow_model_recall: bool


class ResourceOutputRule(InputModel):
    instance_id: NonEmptyString
    allowed_model_fields: Selectors
    export_fields: Selectors
    retained_fields: Selectors = Field(default=None)
    memory: MemoryRule = Field(default=None)
    masking: list[MaskingRule]


class PlanOutput(InputModel):
    format: NonEmptyString
    destination: NonEmptyString
    resource_rules: list[ResourceOutputRule]
    # Retain old request shapes for idempotent replay, not new result generation.
    table_rules: list[JsonObject] = Field(default_factory=list, deprecated=True)
    artifact_rules: list[JsonObject] = Field(default_factory=list, deprecated=True)


class InputBinding(InputModel):
    binding_id: NonEmptyString
    source_step_id: NonEmptyString
    source_pointer: JsonPointer
    target_pointer: Annotated[JsonPointer, Field(min_length=1)] | None


class StepCondition(InputModel):
    model_config = ConfigDict(json_schema_extra={"allOf": [
        {"if": {"properties": {"operator": {"const": "exists"}}}, "then": {"properties": {"value": False}}, "else": {"required": ["value"]}},
        {"if": {"properties": {"operator": {"const": "eq"}}}, "then": {"properties": {"value": {"not": {"type": "array"}}}}},
        {"if": {"properties": {"operator": {"const": "in"}}}, "then": {"properties": {"value": {"type": "array", "minItems": 1}}}},
    ]})
    binding_id: NonEmptyString
    operator: Literal["exists", "eq", "in"]
    value: Scalar | list[Scalar] = None

    @model_validator(mode="after")
    def condition_value(self) -> Self:
        if self.operator == "exists":
            if "value" in self.model_fields_set:
                raise ValueError("exists forbids a value")
        elif "value" not in self.model_fields_set:
            raise ValueError("comparison requires a value")
        elif self.operator == "in":
            if not isinstance(self.value, list) or not self.value:
                raise ValueError("in requires a nonempty scalar array")
        elif isinstance(self.value, list):
            raise ValueError("eq requires one scalar value")
        return self


class CallRef(InputModel):
    task_id: NonEmptyString
    call_id: NonEmptyString


class ReconciliationTargets(InputModel):
    model_config = ConfigDict(json_schema_extra={"anyOf": [
        {"properties": {field: {"minItems": 1}}} for field in ("source_step_ids", "prior_call_ids", "external_call_refs")
    ]})
    source_step_ids: UniqueIDs
    prior_call_ids: UniqueIDs
    external_call_refs: Annotated[list[CallRef], Field(json_schema_extra={"uniqueItems": True})]

    @model_validator(mode="after")
    def targets(self) -> Self:
        pairs = [(item.task_id, item.call_id) for item in self.external_call_refs]
        if not (self.source_step_ids or self.prior_call_ids or pairs) or len(pairs) != len(set(pairs)):
            raise ValueError("confirmation targets must be nonempty and unique")
        return self


class PlanStep(InputModel):
    model_config = ConfigDict(json_schema_extra={
        "if": {"properties": {"purpose": {"const": "reconcile"}}},
        "then": {"required": ["reconciliation"]},
        "else": {"properties": {"reconciliation": False}},
    })
    step_id: NonEmptyString
    description: NonEmptyString
    purpose: Literal["collect", "change", "verify", "reconcile"]
    depends_on: UniqueIDs
    instance_id: NonEmptyString
    capability_id: NonEmptyString
    contract_ref: NonEmptyString
    scope: JsonObject
    input_bindings: list[InputBinding]
    conditions: list[StepCondition] = Field(default=None)
    reconciliation: ReconciliationTargets = Field(default=None)

    @model_validator(mode="after")
    def reconciliation_scope(self) -> Self:
        if (self.purpose == "reconcile") != ("reconciliation" in self.model_fields_set):
            raise ValueError("only reconcile requires confirmation targets")
        return self


class CompletionCriterion(InputModel):
    criterion_id: NonEmptyString
    description: NonEmptyString
    required: bool
    step_ids: UniqueIDs


class PlanDraft(InputModel):
    purpose: NonEmptyString
    steps: list[PlanStep]
    budget: PlanBudget
    output: PlanOutput
    memory_search_refs: Annotated[UniqueIDs, Field(min_length=1)]
    memory_refs: UniqueIDs
    memory_limitations: list[NonEmptyString]
    completion_criteria: Annotated[list[CompletionCriterion], Field(min_length=1)]
    previous_plan_id: NonEmptyString = Field(default=None)
    carried_evidence_refs: UniqueIDs = Field(default_factory=list)

    @model_validator(mode="after")
    def check_completion_ids(self) -> Self:
        unique_ids([criterion.criterion_id for criterion in self.completion_criteria])
        return self


class PlanSubmit(InputModel):
    request_id: NonEmptyString
    task_id: NonEmptyString
    expected_task_revision: PositiveInt
    draft: PlanDraft


class PlanGet(InputModel):
    task_id: NonEmptyString
    plan_id: NonEmptyString


class ApprovalRequest(InputModel):
    model_config = ConfigDict(json_schema_extra={
        "if": {"properties": {"stage": {"const": "request"}}},
        "then": {"properties": {
            "approval_request_id": False, "decision": False,
            "user_response": False, "user_message_ref": False,
        }},
        "else": {"required": ["approval_request_id", "decision", "user_response"]},
    })

    request_id: NonEmptyString
    task_id: NonEmptyString
    expected_task_revision: PositiveInt
    plan_id: NonEmptyString
    plan_version: PositiveInt
    stage: Literal["request", "respond"]
    approval_request_id: NonEmptyString = Field(default=None)
    decision: Literal["approve", "request_changes", "decline", "cancel"] | None = None
    user_response: NonEmptyString = Field(default=None)
    user_message_ref: NonEmptyString = Field(default=None)

    @model_validator(mode="after")
    def check_stage_fields(self) -> Self:
        response_fields = {"approval_request_id", "decision", "user_response", "user_message_ref"}
        if self.stage == "request" and self.model_fields_set & response_fields:
            raise ValueError("request stage cannot contain response fields")
        required_fields = {"approval_request_id", "decision", "user_response"}
        if self.stage == "respond" and not required_fields <= self.model_fields_set:
            raise ValueError("respond stage requires the request, decision and actual reply")
        return self


class CapabilityGet(InputModel):
    task_id: NonEmptyString
    instance_id: NonEmptyString
    capability_id: NonEmptyString


class BindingSelection(InputModel):
    binding_id: NonEmptyString
    source_call_id: NonEmptyString


class CapabilityExecute(CapabilityGet):
    expected_task_revision: PositiveInt
    plan_id: NonEmptyString
    plan_version: PositiveInt
    step_id: NonEmptyString
    call_id: NonEmptyString
    approval_id: NonEmptyString
    contract_ref: NonEmptyString
    parameters: JsonObject
    bindings: list[BindingSelection] = Field(default_factory=list)
    reconcile_of_call_id: NonEmptyString = Field(default=None)
    reconcile_task_id: NonEmptyString = Field(default=None)


class MemoryDraft(InputModel):
    category: Literal["service_rule", "resource_structure", "procedure", "lesson"]
    statement: NonEmptyString
    applicability: NonEmptyString
    instance_ids: Annotated[UniqueIDs, Field(min_length=1)]
    verification_method: NonEmptyString


class ReportMemoryCandidate(InputModel):
    draft: MemoryDraft
    source_refs: Annotated[UniqueIDs, Field(min_length=1, max_length=100)]
    memory_ref: NonEmptyString = Field(default=None)


class ResultSubmit(InputModel):
    model_config = ConfigDict(json_schema_extra={"oneOf": [
        {"properties": {"plan_id": {"type": "null"}, "plan_version": {"type": "null"}}},
        {"properties": {"plan_id": {"type": "string"}, "plan_version": {"type": "integer"}}},
    ]})
    request_id: NonEmptyString
    task_id: NonEmptyString
    expected_task_revision: PositiveInt
    plan_id: NonEmptyString | None
    plan_version: PositiveInt | None
    report: JsonObject
    memory_candidates: Annotated[list[ReportMemoryCandidate], Field(max_length=20)] = Field(default_factory=list)

    @model_validator(mode="after")
    def paired_plan(self) -> Self:
        if (self.plan_id is None) != (self.plan_version is None):
            raise ValueError("plan identity must be entirely present or null")
        return self


class ResultGet(InputModel):
    task_id: NonEmptyString
    record_ref: NonEmptyString
    cursor: NonEmptyString = Field(default=None)
    limit_bytes: Annotated[int, Field(ge=1, le=1048576)] = Field(default=None)


class MemoryUpdate(InputModel):
    model_config = ConfigDict(json_schema_extra={
        "if": {"properties": {"operation": {"const": "store"}}},
        "then": {"required": ["draft"]},
        "else": {"required": ["memory_ref"], "properties": {"draft": False}},
    })
    request_id: NonEmptyString
    task_id: NonEmptyString
    expected_task_revision: PositiveInt
    operation: Literal["store", "invalidate"]
    memory_ref: NonEmptyString = Field(default=None)
    draft: MemoryDraft = Field(default=None)
    source_refs: Annotated[UniqueIDs, Field(min_length=1)]
    reason: NonEmptyString

    @model_validator(mode="after")
    def operation_fields(self) -> Self:
        if self.operation == "store" and "draft" not in self.model_fields_set:
            raise ValueError("store requires a draft")
        if self.operation == "invalidate" and ("draft" in self.model_fields_set or "memory_ref" not in self.model_fields_set):
            raise ValueError("invalidate requires a reference and forbids a draft")
        return self


class WorkerAssignment(InputModel):
    label: NonEmptyString
    objective: NonEmptyString
    step_ids: Annotated[UniqueIDs, Field(min_length=1, max_length=100)]
    completion_criteria: Annotated[list[NonEmptyString], Field(min_length=1, max_length=20)]
    source_refs: UniqueIDs = Field(default_factory=list)


class WorkerCall(InputModel):
    step_id: NonEmptyString
    call_id: NonEmptyString
    capability_id: NonEmptyString = Field(default=None)
    parameters: JsonObject
    bindings: list[BindingSelection] = Field(default_factory=list)


class WorkerResult(InputModel):
    summary: NonEmptyString
    evidence_refs: UniqueIDs
    limitations: list[NonEmptyString]


WORKER_FIELDS = {
    "create": ({"request_id", "expected_task_revision", "assignment"}, {"ttl_seconds"}),
    "get": ({"delegation_token"}, set()),
    "execute": ({"delegation_token", "call"}, {"timeout_ms"}),
    "search": ({"delegation_token", "query"}, {"instance_ids", "limit"}),
    "read": ({"delegation_token", "record_ref"}, {"cursor", "limit_bytes"}),
    "complete": ({"request_id", "delegation_token", "result"}, set()),
    "revoke": ({"request_id", "expected_task_revision", "delegation_id"}, set()),
}


class WorkerRequest(InputModel):
    model_config = ConfigDict(json_schema_extra={"oneOf": [
        {"properties": {"operation": {"const": operation},
            **{field: False for field in set().union(*(a | b for a, b in WORKER_FIELDS.values())) - required - optional}},
         "required": sorted(required)}
        for operation, (required, optional) in WORKER_FIELDS.items()
    ]})
    operation: Literal["create", "get", "execute", "search", "read", "complete", "revoke"]
    task_id: NonEmptyString
    request_id: NonEmptyString = Field(default=None)
    expected_task_revision: PositiveInt = Field(default=None)
    assignment: WorkerAssignment = Field(default=None)
    ttl_seconds: Annotated[int, Field(ge=1, le=86400)] = Field(default=None)
    delegation_id: NonEmptyString = Field(default=None)
    delegation_token: NonEmptyString = Field(default=None)
    call: WorkerCall = Field(default=None)
    timeout_ms: Annotated[int, Field(ge=1, le=60000)] = Field(default=None)
    query: NonEmptyString = Field(default=None)
    instance_ids: Annotated[UniqueIDs, Field(min_length=1)] = Field(default=None)
    limit: Annotated[int, Field(ge=1, le=50)] = Field(default=None)
    record_ref: NonEmptyString = Field(default=None)
    cursor: NonEmptyString = Field(default=None)
    limit_bytes: Annotated[int, Field(ge=1, le=1048576)] = Field(default=None)
    result: WorkerResult = Field(default=None)

    @model_validator(mode="after")
    def operation_fields(self) -> Self:
        required, optional = WORKER_FIELDS[self.operation]
        fields = self.model_fields_set - {"operation", "task_id"}
        if not required <= fields or fields - required - optional:
            raise ValueError("fields must match the worker operation")
        return self


INPUT_MODELS: dict[str, type[InputModel]] = {
    "daeri_task_create": TaskCreate,
    "daeri_task_update": TaskUpdate,
    "daeri_task_get": TaskGet,
    "daeri_task_resume": TaskResume,
    "daeri_task_cancel": TaskCancel,
    "daeri_memory_search": MemorySearch,
    "daeri_resource_list": ResourceList,
    "daeri_plan_submit": PlanSubmit,
    "daeri_plan_get": PlanGet,
    "daeri_approval_request": ApprovalRequest,
    "daeri_capability_get": CapabilityGet,
    "daeri_capability_execute": CapabilityExecute,
    "daeri_result_submit": ResultSubmit,
    "daeri_result_get": ResultGet,
    "daeri_memory_update": MemoryUpdate,
    "daeri_worker": WorkerRequest,
}
