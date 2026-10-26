"""Validate plans against registered capabilities, rather than model assurances."""

import copy
import json
from itertools import zip_longest

from jsonschema import Draft202012Validator
from pydantic import ValidationError

from daeri.core.contracts import PlanDraft
from daeri.core.policy import (
    MEMORY_CATEGORIES,
    effective_profile_memory_policy,
    effective_resource_policy,
    validate_resource_policy,
    within_selectors,
)
from daeri.core.errors import StoreError


def check(condition, message):
    if not condition:
        raise StoreError("PLAN_INVALID", message)


def _intersect_fields(*groups):
    result = [{'segments': []}]
    for group in groups:
        common = {}
        for left in result:
            for right in group:
                segments = []
                for a, b in zip_longest(left['segments'], right['segments']):
                    if a is None or b is None or a == b:
                        segments.append(a if b is None else b)
                    elif a == {'each': True} and 'index' in b:
                        segments.append(b)
                    elif b == {'each': True} and 'index' in a:
                        segments.append(a)
                    else:
                        break
                else:
                    common[json.dumps(segments, sort_keys=True)] = {'segments': segments}
        result = list(common.values())
    return copy.deepcopy(result)


def resolve_memory_defaults(profile, draft):
    """Include automatic memory in the immutable plan before requesting approval."""
    draft = copy.deepcopy(draft)
    resources = {resource['instance_id']: resource for resource in profile.get('resources', [])}
    for rule in draft['output']['resource_rules']:
        resource = resources.get(rule['instance_id'])
        ceiling = effective_resource_policy(resource) if resource else {}
        if 'retained_fields' not in rule:
            rule['retained_fields'] = _intersect_fields(rule['allowed_model_fields'], ceiling.get('retained_fields', []))
        if 'memory' in rule:
            continue
        upper = ceiling.get('memory', {})
        profile_memory = effective_profile_memory_policy(profile)
        enabled = (rule['instance_id'] in profile_memory.get('instance_ids', [])
                   and profile_memory.get('model_allowed') is True and profile_memory.get('retain_allowed') is True
                   and upper.get('allow_model_recall') is True and upper.get('retention', 'none') != 'none')
        categories = [category for category in MEMORY_CATEGORIES
                      if category in upper.get('categories', []) and category in profile_memory.get('categories', [])]
        fields = _intersect_fields(rule['allowed_model_fields'], rule['retained_fields'],
                                   upper.get('source_fields', [])) if enabled else []
        contexts = copy.deepcopy(upper.get('context_sources', [])) if enabled else []
        if enabled and categories and (fields or contexts):
            rule['memory'] = {'categories': categories, 'source_fields': fields, 'context_sources': contexts,
                'retention': 'long_term' if upper['retention'] == 'long_term'
                             and profile_memory.get('long_term_allowed') is True else 'source_bound',
                'allow_model_recall': True}
        else:
            rule['memory'] = {'categories': [], 'source_fields': [], 'context_sources': [],
                              'retention': 'none', 'allow_model_recall': False}
    return draft


def resolve_plan_defaults(profile, draft, registry):
    """Resolve connection policy and runtime details once, before plan approval."""
    try:
        model = PlanDraft.model_validate(draft)
    except ValidationError:
        raise StoreError("PLAN_INVALID", "계획의 필드·자료형·완료 조건을 확인하세요.") from None
    # Preserve omission of optional fields whose presence has a separate meaning.
    result = model.model_dump(exclude_unset=True)
    result.setdefault("budget", {})
    for key, value in model.budget.model_dump().items():
        result["budget"].setdefault(key, value)
    result.setdefault("memory_refs", [])
    result.setdefault("memory_search_refs", [])
    result.setdefault("memory_limitations", [])
    result.setdefault("carried_evidence_refs", [])
    result["output"].setdefault("destination", "conversation")
    resources = {item["instance_id"]: item for item in profile.get("resources", [])}
    for step in result["steps"]:
        check(step["instance_id"] in resources, "profile에 등록한 연결을 선택하세요.")
        step.setdefault("purpose", "collect")
        step.setdefault("depends_on", [])
        step.setdefault("capability_id", "*")
        step.setdefault("scope", {})
        descriptor = registry.get(profile, step["instance_id"], step["capability_id"])
        step.setdefault("contract_ref", descriptor["contract_ref"])
    for criterion in result["completion_criteria"]:
        criterion.setdefault("required", True)
    if "resource_rules" not in result["output"] or not result["output"]["resource_rules"]:
        result["output"]["resource_rules"] = [
            {"instance_id": instance, **effective_resource_policy(resource)}
            for instance, resource in resources.items()]
    return resolve_memory_defaults(profile, result)


def evidence_matches_step(record, step):
    """A reused step ID cannot change the target or the approved observation scope."""
    return (record.get("record_kind") == "call_result"
            and all(record.get(key) == step.get(key) for key in
                    ("step_id", "instance_id", "capability_id", "contract_ref"))
            and "scope" in record and "scope" in step
            and json.dumps(record["scope"], sort_keys=True, allow_nan=False) ==
                json.dumps(step["scope"], sort_keys=True, allow_nan=False))


def validate_plan(profile, draft, registry, evidence_instances=()):
    draft = resolve_plan_defaults(profile, draft, registry)
    steps, contracts = {}, []
    for step in draft["steps"]:
        check(step["step_id"] not in steps, "계획 단계 ID는 고유해야 합니다.")
        descriptor = registry.get(profile, step["instance_id"], step["capability_id"])
        check(step["contract_ref"] == descriptor["contract_ref"], "현재 등록 계약으로 계획을 갱신하세요.")
        check(step["purpose"] in descriptor["supported_purposes"], "기능이 해당 실행 목적을 지원하지 않습니다.")
        check(not descriptor["effects"]["changes_state"] and step["purpose"] in {"collect", "verify"}, "첫 버전은 등록된 읽기·검증 기능만 지원합니다.")
        try:
            Draft202012Validator(descriptor["scope_schema"]).validate(step["scope"])
        except Exception:
            raise StoreError("PLAN_INVALID", "등록 기능의 범위 스키마를 확인하세요.") from None
        if draft["budget"]["max_external_requests"] is not None:
            check(descriptor["budget_support"]["external_requests_enforceable"], "하위 MCP의 실제 요청 수는 강제할 수 없어 null 한도가 필요합니다.")
        steps[step["step_id"]] = step
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
    for criterion in draft["completion_criteria"]:
        check(set(criterion["step_ids"]) <= set(steps), "완료 조건이 없는 단계를 참조합니다.")
    check(not draft["budget"]["resource_limits"], "현재 기능은 별도 metric 한도 강제를 지원하지 않습니다.")

    resources = {item["instance_id"]: item for item in profile.get("resources", [])}
    rules = draft["output"]["resource_rules"]
    used = {step["instance_id"] for step in steps.values()} | set(evidence_instances)
    check(len({rule.get("instance_id") for rule in rules}) == len(rules), "리소스 제공 규칙이 중복됩니다.")
    check(used <= {rule.get("instance_id") for rule in rules}, "사용한 리소스마다 제공·보관·기억 범위를 명시하세요.")
    for rule in rules:
        check(rule["instance_id"] in resources, "profile 밖 리소스의 규칙은 사용할 수 없습니다.")
        try:
            validate_resource_policy({key: value for key, value in rule.items() if key != "instance_id"})
        except StoreError:
            raise StoreError("PLAN_INVALID", "리소스의 제공·마스킹·기억 규칙이 올바르지 않습니다.") from None
        ceiling = effective_resource_policy(resources[rule["instance_id"]])
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
        if memory["retention"] != "none":
            profile_memory = effective_profile_memory_policy(profile)
            check(rule["instance_id"] in profile_memory.get("instance_ids", []) and
                  set(memory["categories"]) <= set(profile_memory.get("categories", [])) and
                  profile_memory.get("retain_allowed") is True and
                  (not memory["allow_model_recall"] or profile_memory.get("model_allowed") is True) and
                  (memory["retention"] != "long_term" or profile_memory.get("long_term_allowed") is True),
                  "profile의 기억 대상·범주·보존·재제공 상한을 초과했습니다.")

    return sorted(set(contracts))
