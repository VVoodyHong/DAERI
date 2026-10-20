"""Validate plans against registered capabilities, rather than model assurances."""

from jsonschema import Draft202012Validator
from pydantic import ValidationError

from .contracts import PlanDraft
from .policy import pointer_tokens, validate_resource_policy, within_selectors
from .storage import StoreError


def check(condition, message):
    if not condition:
        raise StoreError("PLAN_INVALID", message)


def validate_plan(profile, draft, registry, formats, evidence_instances=()):
    # The MCP boundary normally performs this check; keep direct callers safe too.
    try:
        draft = PlanDraft.model_validate(draft).model_dump(exclude_unset=True)
    except ValidationError:
        raise StoreError("PLAN_INVALID", "계획의 중첩 필드·자료형·필수값을 확인하세요.") from None
    steps, contracts, descriptors = {}, [], {}
    required = {"step_id", "description", "purpose", "depends_on", "instance_id", "capability_id", "contract_ref", "scope", "input_bindings"}
    for step in draft["steps"]:
        check(required <= set(step) <= required | {"conditions", "reconciliation"}, "실행 단계의 필드를 확인하세요.")
        check(all(isinstance(step[key], str) and step[key].strip() for key in
                  ("step_id", "description", "purpose", "instance_id", "capability_id", "contract_ref")), "단계 식별자와 설명이 필요합니다.")
        check(step["step_id"] not in steps, "계획 단계 ID는 고유해야 합니다.")
        check(isinstance(step["depends_on"], list) and all(isinstance(item, str) for item in step["depends_on"]) and
              len(set(step["depends_on"])) == len(step["depends_on"]), "선행 단계 목록이 올바르지 않습니다.")
        descriptor = registry.get(profile, step["instance_id"], step["capability_id"])
        check(step["contract_ref"] == descriptor["contract_ref"], "현재 등록 계약으로 계획을 갱신하세요.")
        check(step["purpose"] in descriptor["supported_purposes"], "기능이 해당 실행 목적을 지원하지 않습니다.")
        check(not descriptor["effects"]["changes_state"] and step["purpose"] in {"collect", "verify"}, "첫 버전은 등록된 읽기·검증 기능만 지원합니다.")
        check("reconciliation" not in step, "현재 등록 기능은 외부 변경 확인을 지원하지 않습니다.")
        try:
            Draft202012Validator(descriptor["scope_schema"]).validate(step["scope"])
        except Exception:
            raise StoreError("PLAN_INVALID", "등록 기능의 범위 스키마를 확인하세요.") from None
        if draft["budget"]["max_external_requests"] is not None:
            check(descriptor["budget_support"]["external_requests_enforceable"], "하위 MCP의 실제 요청 수는 강제할 수 없어 null 한도가 필요합니다.")
        check(isinstance(step["input_bindings"], list) and isinstance(step.get("conditions", []), list), "바인딩과 조건은 배열이어야 합니다.")
        binding_ids, targets = set(), []
        for binding in step["input_bindings"]:
            check(isinstance(binding, dict) and set(binding) == {"binding_id", "source_step_id", "source_pointer", "target_pointer"}, "바인딩 필드를 확인하세요.")
            check(isinstance(binding["binding_id"], str) and binding["binding_id"] and binding["binding_id"] not in binding_ids, "바인딩 ID는 고유해야 합니다.")
            binding_ids.add(binding["binding_id"])
            pointer_tokens(binding["source_pointer"])
            if binding["target_pointer"] is not None:
                tokens = tuple(pointer_tokens(binding["target_pointer"]))
                check(bool(tokens) and not any(tokens[:len(other)] == other or other[:len(tokens)] == tokens for other in targets), "바인딩 대상이 중복되거나 겹칩니다.")
                targets.append(tokens)
        for condition in step.get("conditions", []):
            check(isinstance(condition, dict) and {"binding_id", "operator"} <= set(condition) <= {"binding_id", "operator", "value"}, "조건 필드를 확인하세요.")
            check(condition["binding_id"] in binding_ids and condition["operator"] in {"exists", "eq", "in"}, "등록 바인딩과 지원 비교 연산을 선택하세요.")
            check((condition["operator"] == "exists") == ("value" not in condition), "exists 조건에는 값을 넣지 않습니다.")
            value = condition.get("value")
            check(not isinstance(value, dict) and (not isinstance(value, list) or condition["operator"] == "in"), "비교 조건에는 실행 식이나 객체를 넣을 수 없습니다.")
            if condition["operator"] == "in":
                check(isinstance(value, list) and value and all(not isinstance(item, (dict, list)) for item in value), "in 조건은 비어 있지 않은 스칼라 배열이 필요합니다.")
        steps[step["step_id"]] = step
        descriptors[step["step_id"]] = descriptor
        contracts.append(descriptor["contract_ref"])

    visited, visiting = set(), set()
    def visit(step_id):
        check(step_id in steps and step_id not in visiting, "계획의 선행 단계가 없거나 순환합니다.")
        if step_id in visited:
            return
        visiting.add(step_id)
        for dependency in steps[step_id]["depends_on"]:
            visit(dependency)
        visiting.remove(step_id)
        visited.add(step_id)
    for step_id in steps:
        visit(step_id)
        for binding in steps[step_id]["input_bindings"]:
            source = binding["source_step_id"]
            check(source in steps and source in steps[step_id]["depends_on"], "바인딩은 명시한 선행 단계를 참조해야 합니다.")
    for criterion in draft["completion_criteria"]:
        check(set(criterion["step_ids"]) <= set(steps), "완료 조건이 없는 단계를 참조합니다.")
    check(not draft["budget"]["resource_limits"], "현재 기능은 별도 metric 한도 강제를 지원하지 않습니다.")

    resources = {item["instance_id"]: item for item in profile.get("resources", [])}
    rules = draft["output"]["resource_rules"]
    used = {step["instance_id"] for step in steps.values()} | set(evidence_instances)
    check(len({rule.get("instance_id") for rule in rules}) == len(rules), "리소스 제공 규칙이 중복됩니다.")
    check(used <= {rule.get("instance_id") for rule in rules}, "사용한 리소스마다 제공·보관·기억 범위를 명시하세요.")
    for rule in rules:
        check(set(rule) == {"instance_id", "allowed_model_fields", "export_fields", "retained_fields", "memory", "masking"}, "리소스 제공 규칙의 필드를 확인하세요.")
        check(rule["instance_id"] in resources, "profile 밖 리소스의 규칙은 사용할 수 없습니다.")
        try:
            validate_resource_policy({key: value for key, value in rule.items() if key != "instance_id"})
        except StoreError:
            raise StoreError("PLAN_INVALID", "리소스의 제공·마스킹·기억 규칙이 올바르지 않습니다.") from None
        ceiling = resources[rule["instance_id"]].get("policy", {})
        for field in ("allowed_model_fields", "export_fields", "retained_fields"):
            check(within_selectors(rule[field], ceiling.get(field, [])), "profile의 자료 제공·보관 상한을 초과했습니다.")
        memory = rule["memory"]
        upper = ceiling.get("memory", {})
        check(set(memory["categories"]) <= set(upper.get("categories", [])) and
              set(memory["context_sources"]) <= set(upper.get("context_sources", [])) and
              within_selectors(memory["source_fields"], upper.get("source_fields", [])), "profile의 기억 수집 범위를 초과했습니다.")
        ranks = {"none": 0, "source_bound": 1, "long_term": 2}
        check(ranks[memory["retention"]] <= ranks[upper.get("retention", "none")] and
              (not memory["allow_model_recall"] or upper.get("allow_model_recall") is True), "profile의 기억 보존·재제공 상한을 초과했습니다.")

    table_ids = set()
    for table in draft["output"]["table_rules"]:
        fields = {"table_id", "sources", "join_mode", "coverage", "allowed_operations", "max_rows", "ratio_scale"}
        check(set(table) == fields and isinstance(table["table_id"], str) and table["table_id"] not in table_ids, "표 규칙의 필드와 고유 ID를 확인하세요.")
        table_ids.add(table["table_id"])
        check(table["join_mode"] in {"none", "inner", "full"} and table["coverage"] in {"complete", "selected"}, "표 결합·전체성 규칙이 올바르지 않습니다.")
        check(type(table["max_rows"]) is int and 1 <= table["max_rows"] <= 10000, "표는 최대 10,000행까지 검증합니다.")
        check(isinstance(table["allowed_operations"], list) and table["allowed_operations"] and
              len(set(table["allowed_operations"])) == len(table["allowed_operations"]) and
              set(table["allowed_operations"]) <= {"copy", "equal", "sum", "difference", "ratio", "missing"}, "지원하는 표 연산을 선택하세요.")
        check((type(table["ratio_scale"]) is int and 0 <= table["ratio_scale"] <= 18) if "ratio" in table["allowed_operations"] else table["ratio_scale"] is None, "비율의 반올림 자릿수를 확인하세요.")
        check(isinstance(table["sources"], list) and table["sources"] and len({source.get("source_id") for source in table["sources"]}) == len(table["sources"]), "표 원본 ID는 하나 이상이며 고유해야 합니다.")
        for source in table["sources"]:
            check(set(source) == {"source_id", "step_id", "rows_pointer", "key_pointers"} and source["step_id"] in steps, "표 원본은 승인한 자료 확보 단계를 참조해야 합니다.")
            pointer_tokens(source["rows_pointer"])
            check(isinstance(source["key_pointers"], list) and len(set(source["key_pointers"])) == len(source["key_pointers"]), "결합키 포인터를 확인하세요.")
            for key in source["key_pointers"]:
                pointer_tokens(key)

    available, artifact_ids = {item["format_id"]: item for item in formats}, set()
    for artifact in draft["output"]["artifact_rules"]:
        check(set(artifact) == {"artifact_id", "format_id", "format_contract_ref", "input_kind", "options", "max_bytes"}, "산출물 규칙의 필드를 확인하세요.")
        check(isinstance(artifact["artifact_id"], str) and artifact["artifact_id"] not in artifact_ids, "산출물 ID는 고유해야 합니다.")
        artifact_ids.add(artifact["artifact_id"])
        descriptor = available.get(artifact["format_id"])
        check(descriptor and descriptor["available"] and descriptor["contract_ref"] == artifact["format_contract_ref"] and artifact["input_kind"] in descriptor["input_kinds"], "현재 등록한 파일 형식·입력 종류·계약을 선택하세요.")
        check(type(artifact["max_bytes"]) is int and 0 < artifact["max_bytes"] <= 10 * 1024 * 1024, "파일 크기 상한은 10MiB 이내로 지정하세요.")
        try:
            Draft202012Validator(descriptor["options_schema"]).validate(artifact["options"])
        except Exception:
            raise StoreError("PLAN_INVALID", "파일 생성 옵션이 올바르지 않습니다.") from None
    return sorted(set(contracts))
