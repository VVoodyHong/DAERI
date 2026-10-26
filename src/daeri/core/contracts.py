"""Strict inputs for the implemented local task and approval tools."""

from typing import Annotated, Literal, Self

from pydantic.json_schema import SkipJsonSchema

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


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class RequestInput(InputModel):
    # Stored callers may replay old keys; the model-facing schema uses runtime bookkeeping.
    request_id: SkipJsonSchema[NonEmptyString] = Field(default=None)


class TaskMutation(RequestInput):
    expected_task_revision: SkipJsonSchema[PositiveInt] = Field(default=None)


# Unvalidated None defaults allow omission while supplied JSON null stays invalid.
class ContextSelection(InputModel):
    profile_id: NonEmptyString = Field(default=None)
    department_id: NonEmptyString = Field(default=None)
    environment_id: NonEmptyString = Field(default=None)


class DesiredOutput(InputModel):
    format: NonEmptyString = Field(default=None)
    description: NonEmptyString = Field(default=None)


class TaskCreate(RequestInput):
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


class TaskUpdate(TaskMutation):
    model_config = ConfigDict(json_schema_extra={
        "if": {"required": ["patch"]},
        "then": {"properties": {"action": False}},
        "else": {"required": ["action"]},
    })

    task_id: NonEmptyString
    patch: TaskPatch = Field(default=None)
    action: Literal["pause"] = Field(default=None)

    @model_validator(mode="after")
    def check_change_branch(self) -> Self:
        if len(self.model_fields_set & {"patch", "action"}) != 1:
            raise ValueError("exactly one of patch and action is required")
        return self


class TaskGet(InputModel):
    model_config = ConfigDict(json_schema_extra={
        "if": {"required": ["task_id"]},
        "then": {"properties": {"profile_id": False}},
        "else": {"required": ["profile_id"]},
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


class TaskResume(TaskMutation):
    task_id: NonEmptyString


class TaskCancel(TaskMutation):
    task_id: NonEmptyString
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
    max_tool_calls: NonNegativeInt | None = None
    max_runtime_ms: NonNegativeInt | None = None
    max_external_requests: SkipJsonSchema[NonNegativeInt | None] = None
    resource_limits: SkipJsonSchema[list[ResourceLimit]] = Field(default_factory=list)

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
    from daeri.core.policy import validate_selectors
    from daeri.core.errors import StoreError

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
    destination: NonEmptyString = "conversation"
    resource_rules: SkipJsonSchema[list[ResourceOutputRule]] = Field(default_factory=list)


class PlanStep(InputModel):
    step_id: NonEmptyString
    description: NonEmptyString
    purpose: Literal["collect", "verify"] = "collect"
    depends_on: UniqueIDs = Field(default_factory=list)
    instance_id: NonEmptyString
    capability_id: NonEmptyString = "*"
    contract_ref: SkipJsonSchema[NonEmptyString] = Field(default=None)
    scope: JsonObject = Field(default_factory=dict)


class CompletionCriterion(InputModel):
    criterion_id: NonEmptyString
    description: NonEmptyString
    required: bool = True
    step_ids: UniqueIDs


class PlanDraft(InputModel):
    purpose: NonEmptyString
    steps: list[PlanStep]
    budget: PlanBudget = Field(default_factory=PlanBudget)
    output: PlanOutput
    memory_search_refs: UniqueIDs = Field(default_factory=list)
    memory_refs: UniqueIDs = Field(default_factory=list)
    memory_limitations: list[NonEmptyString] = Field(default_factory=list)
    completion_criteria: Annotated[list[CompletionCriterion], Field(min_length=1)]
    previous_plan_id: SkipJsonSchema[NonEmptyString] = Field(default=None)
    carried_evidence_refs: UniqueIDs = Field(default_factory=list)

    @model_validator(mode="after")
    def check_completion_ids(self) -> Self:
        unique_ids([criterion.criterion_id for criterion in self.completion_criteria])
        return self


class PlanSubmit(TaskMutation):
    task_id: NonEmptyString
    draft: PlanDraft


class PlanGet(InputModel):
    task_id: NonEmptyString
    plan_id: NonEmptyString


class ApprovalRequest(TaskMutation):
    model_config = ConfigDict(json_schema_extra={"required": ["task_id", "plan_id", "decision", "user_response"]})
    task_id: NonEmptyString
    plan_id: NonEmptyString
    plan_version: SkipJsonSchema[PositiveInt] = Field(default=None)
    stage: SkipJsonSchema[Literal["request", "respond"]] = "respond"
    approval_request_id: SkipJsonSchema[NonEmptyString] = Field(default=None)
    decision: Literal["approve", "request_changes", "decline", "cancel"] | None = None
    user_response: NonEmptyString = Field(default=None)
    user_message_ref: NonEmptyString = Field(default=None)

    @model_validator(mode="after")
    def check_stage_fields(self) -> Self:
        response_fields = {"approval_request_id", "decision", "user_response", "user_message_ref"}
        if self.stage == "request" and self.model_fields_set & response_fields:
            raise ValueError("request stage cannot contain response fields")
        required_fields = {"decision", "user_response"}
        if self.stage == "respond" and not required_fields <= self.model_fields_set:
            raise ValueError("respond stage requires the decision and actual reply")
        return self


class CapabilityGet(InputModel):
    task_id: NonEmptyString
    instance_id: NonEmptyString
    capability_id: NonEmptyString


class CapabilityExecute(TaskMutation):
    task_id: NonEmptyString
    step_id: NonEmptyString
    instance_id: NonEmptyString = Field(default=None)
    capability_id: NonEmptyString = Field(default=None)
    plan_id: SkipJsonSchema[NonEmptyString] = Field(default=None)
    plan_version: SkipJsonSchema[PositiveInt] = Field(default=None)
    call_id: SkipJsonSchema[NonEmptyString] = Field(default=None)
    approval_id: SkipJsonSchema[NonEmptyString] = Field(default=None)
    contract_ref: SkipJsonSchema[NonEmptyString] = Field(default=None)
    parameters: JsonObject


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


class ResultSubmit(TaskMutation):
    task_id: NonEmptyString
    plan_id: SkipJsonSchema[NonEmptyString | None] = None
    plan_version: SkipJsonSchema[PositiveInt | None] = None
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


class MemoryUpdate(TaskMutation):
    model_config = ConfigDict(json_schema_extra={
        "if": {"properties": {"operation": {"const": "store"}}},
        "then": {"required": ["draft"]},
        "else": {"required": ["memory_ref"], "properties": {"draft": False}},
    })
    task_id: NonEmptyString
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


class SubagentAssignment(InputModel):
    label: NonEmptyString
    objective: NonEmptyString
    step_ids: Annotated[UniqueIDs, Field(min_length=1, max_length=100)]
    completion_criteria: Annotated[list[NonEmptyString], Field(min_length=1, max_length=20)]
    source_refs: UniqueIDs = Field(default_factory=list)


class SubagentCall(InputModel):
    step_id: NonEmptyString
    call_id: SkipJsonSchema[NonEmptyString] = Field(default=None)
    capability_id: NonEmptyString = Field(default=None)
    parameters: JsonObject


class SubagentResult(InputModel):
    summary: NonEmptyString
    evidence_refs: UniqueIDs
    limitations: list[NonEmptyString]


SUBAGENT_FIELDS = {
    "create": ({"assignment"}, {"request_id", "expected_task_revision", "ttl_seconds"}),
    "get": ({"delegation_token"}, set()),
    "execute": ({"delegation_token", "call"}, {"timeout_ms"}),
    "search": ({"delegation_token", "query"}, {"instance_ids", "limit"}),
    "read": ({"delegation_token", "record_ref"}, {"cursor", "limit_bytes"}),
    "complete": ({"delegation_token", "result"}, {"request_id"}),
    "revoke": ({"delegation_id"}, {"request_id", "expected_task_revision"}),
}


def subagent_operation_schema():
    internal = {"request_id", "expected_task_revision", "ttl_seconds"}
    fields = set().union(*(required | optional for required, optional in SUBAGENT_FIELDS.values())) - internal
    schema = False
    for operation, (required, optional) in reversed(SUBAGENT_FIELDS.items()):
        schema = {
            "if": {"properties": {"operation": {"const": operation}}},
            "then": {"required": sorted(required),
                     "properties": {field: False for field in sorted(fields - required - optional)}},
            "else": schema,
        }
    return schema


class SubagentRequest(InputModel):
    model_config = ConfigDict(json_schema_extra=subagent_operation_schema())
    operation: Literal["create", "get", "execute", "search", "read", "complete", "revoke"]
    task_id: NonEmptyString
    request_id: SkipJsonSchema[NonEmptyString] = Field(default=None)
    expected_task_revision: SkipJsonSchema[PositiveInt] = Field(default=None)
    assignment: SubagentAssignment = Field(default=None)
    ttl_seconds: SkipJsonSchema[Annotated[int, Field(ge=1, le=86400)]] = Field(default=None)
    delegation_id: NonEmptyString = Field(default=None)
    delegation_token: NonEmptyString = Field(default=None)
    call: SubagentCall = Field(default=None)
    timeout_ms: Annotated[int, Field(ge=1, le=60000)] = Field(default=None)
    query: NonEmptyString = Field(default=None)
    instance_ids: Annotated[UniqueIDs, Field(min_length=1)] = Field(default=None)
    limit: Annotated[int, Field(ge=1, le=50)] = Field(default=None)
    record_ref: NonEmptyString = Field(default=None)
    cursor: NonEmptyString = Field(default=None)
    limit_bytes: Annotated[int, Field(ge=1, le=1048576)] = Field(default=None)
    result: SubagentResult = Field(default=None)

    @model_validator(mode="after")
    def operation_fields(self) -> Self:
        required, optional = SUBAGENT_FIELDS[self.operation]
        fields = self.model_fields_set - {"operation", "task_id"}
        if not required <= fields or fields - required - optional:
            raise ValueError("fields must match the subagent operation")
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
    "daeri_subagent": SubagentRequest,
}
