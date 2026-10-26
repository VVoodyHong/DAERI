"""Scoped subagent gateway; the host runs reasoning and the orchestrator owns its ledger."""

import copy
import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta

from daeri.application.runtime import identifier, now, require, response
from daeri.core.security import redact
from daeri.infrastructure.storage import process_alive


def invalidate(task):
    for grant in task.get("delegations", []):
        if grant["state"] == "active":
            grant["state"] = "revoked"


def _live(runtime, task, grant, *, completed=False):
    require(grant and grant["state"] in ({"active", "completed"} if completed else {"active"})
        and (not grant.get("expires_at") or datetime.fromisoformat(grant["expires_at"]) > datetime.now(UTC))
        and task["state"] in {"running", "verifying"}
        and task.get("owner_id") == grant["owner_id"]
        and process_alive(int(grant["owner_id"].split(":", 1)[0]))
        and task["active_plan_ref"] == grant["plan_ref"],
        "SUBAGENT_UNAVAILABLE", "하위 에이전트의 현재 위임·기한·업무 상태를 확인하세요.")
    require(any(item.get("approval_id") == grant["approval_id"] and runtime._approval_valid(task, item)
        for item in task["approvals"]), "SUBAGENT_UNAVAILABLE", "하위 에이전트의 실제 사용자 승인이 유효하지 않습니다.")
    runtime._current_profile(task)
    return grant


def _authenticate(runtime, task, token, *, completed=False):
    digest = hashlib.sha256(token.encode()).hexdigest()
    grant = next((item for item in task.get("delegations", [])
        if secrets.compare_digest(item["token_hash"], digest)), None)
    require(grant, "ACCESS_DENIED", "이 업무에 유효한 하위 에이전트 토큰이 필요합니다.")
    return _live(runtime, task, grant, completed=completed)


def check_execution(runtime, task, args):
    grant = next((item for item in task.get("delegations", [])
        if item["delegation_id"] == args["_delegation_id"]), None)
    _live(runtime, task, grant)
    require(args["step_id"] in grant["step_ids"], "SCOPE_VIOLATION", "하위 에이전트에게 배정한 단계가 아닙니다.")
    return grant


def _assignment(runtime, task, grant):
    return runtime.store.read_record(task["task_id"], grant["assignment_ref"])["payload"]


def _instances(runtime, task, grant):
    plan = runtime._current_plan(task)
    return sorted({step["instance_id"] for step in plan["draft"]["steps"] if step["step_id"] in grant["step_ids"]})


def _allowed_refs(runtime, task, grant):
    return set(_assignment(runtime, task, grant).get("source_refs", [])) | set(grant.get("memory_refs", [])) | {
        call["result_ref"] for call in task["calls"]
        if call.get("delegation_id") == grant["delegation_id"] and call.get("result_ref")}


def _check_refs(runtime, task, grant, refs):
    require(set(refs) <= _allowed_refs(runtime, task, grant), "ACCESS_DENIED", "하위 에이전트에게 제공한 근거 참조만 사용할 수 있습니다.")
    for ref in refs:
        runtime._load_evidence(task, ref)


def _view(runtime, task, grant):
    plan = runtime._current_plan(task)
    steps = [copy.deepcopy(step) for step in plan["draft"]["steps"] if step["step_id"] in grant["step_ids"]]
    # Resolve the current registered read interfaces for this assignment.
    contracts = {}
    profile = runtime._current_profile(task)
    for step in steps:
        descriptor = runtime.registry.get(profile, step["instance_id"], step["capability_id"])
        contracts[(step["instance_id"], step["capability_id"])] = descriptor
        for member in descriptor.get("capability_refs", []):
            key = (step["instance_id"], member["capability_id"])
            contracts[key] = runtime.registry.get(profile, *key)
    result = {"task_id": task["task_id"], "task_revision": task["task_revision"],
        "delegation_id": grant["delegation_id"], "state": grant["state"], "expires_at": grant["expires_at"],
        "plan_ref": grant["plan_ref"], "assignment": _assignment(runtime, task, grant),
        "steps": steps, "contracts": list(contracts.values()), "result_ref": grant.get("result_ref"),
        "calls": [{key: call[key] for key in ("call_id", "step_id", "call_state", "result_ref", "error_code")}
            for call in task["calls"] if call.get("delegation_id") == grant["delegation_id"]]}
    if grant.get("result_ref"):
        result["result"] = runtime._load_evidence(task, grant["result_ref"])["payload"]
    return result


def _summary(task, grant):
    return {"task_id": task["task_id"], "task_revision": task["task_revision"],
        **{key: grant.get(key) for key in ("delegation_id", "state", "expires_at", "plan_ref", "result_ref")}}


def _create(runtime, args, task):
    runtime._own(task)
    require(task["state"] in {"running", "verifying"} and runtime._active_approved(task),
        "APPROVAL_REQUIRED", "현재 업무 계획의 실제 사용자 승인이 필요합니다.")
    plan = runtime._current_plan(task)
    selected = set(args["assignment"]["step_ids"])
    steps = {step["step_id"]: step for step in plan["draft"]["steps"]}
    require(selected <= steps.keys(), "SCOPE_VIOLATION", "현재 계획에 포함된 단계만 위임하세요.")
    for ref in args["assignment"].get("source_refs", []):
        evidence = runtime._load_evidence(task, ref)
        require(evidence["record_kind"] in {"call_result", "memory"}, "ACCESS_DENIED", "하위 에이전트의 입력은 조회·기억 근거로 구성하세요.")
    token = secrets.token_urlsafe(32)
    grant = {"delegation_id": identifier("delegation"), "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "owner_id": task["owner_id"], "plan_ref": copy.deepcopy(task["active_plan_ref"]),
        "approval_id": next(item["approval_id"] for item in reversed(task["approvals"]) if runtime._approval_valid(task, item)),
        "step_ids": sorted(selected), "state": "active", "created_at": now(),
        "expires_at": ((datetime.now(UTC) + timedelta(seconds=args["ttl_seconds"])).isoformat()
                       if args.get("ttl_seconds") else None),
        "assignment_ref": runtime.store.write_record(task["task_id"], "subagent_assignment", redact(args["assignment"]))}
    task.setdefault("delegations", []).append(grant)
    runtime._save(task)
    return response({**_view(runtime, task, grant), "delegation_token": token})


def _mutate(runtime, args):
    with runtime.store.transaction():
        task = runtime._task(args["task_id"])
        operation = args["operation"]
        scope = "subagent:" + task["task_id"]
        fingerprint = hashlib.sha256(json.dumps(args, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        key = args.get("request_id")
        cached = runtime.store.request_get(scope, key) if key else None
        if operation == "complete":
            grant = _authenticate(runtime, task, args["delegation_token"], completed=True)
        else:
            runtime._own(task)
        if cached:
            require(cached["fingerprint"] == fingerprint, "REQUEST_ID_CONFLICT", "같은 요청 ID의 하위 에이전트 입력이 다릅니다.")
            saved = cached["response"]
            grant = next(item for item in task["delegations"] if item["delegation_id"] == saved["delegation_id"])
            if operation == "create":
                _live(runtime, task, grant)
            if operation == "revoke":
                return response(_summary(task, grant))
            return response({**_view(runtime, task, grant),
                **({"delegation_token": saved["delegation_token"]} if "delegation_token" in saved else {})})
        if operation != "complete" and "expected_task_revision" in args:
            require(task["task_revision"] == args["expected_task_revision"], "REVISION_CONFLICT", "현재 업무 개정으로 하위 에이전트를 변경하세요.")
        if operation == "create":
            result = _create(runtime, args, task)
        else:
            if operation == "revoke":
                grant = next((item for item in task.get("delegations", []) if item["delegation_id"] == args["delegation_id"]), None)
                require(grant, "NOT_FOUND", "업무의 하위 에이전트를 찾을 수 없습니다.")
                grant["state"] = "revoked"
            else:
                if grant["state"] == "completed":
                    saved_result = runtime._load_evidence(task, grant["result_ref"])["payload"]
                    require(all(saved_result.get(key) == redact(value) for key, value in args["result"].items()),
                        "SUBAGENT_CONFLICT", "이미 완료한 하위 에이전트의 결과와 다릅니다.")
                    return response(_view(runtime, task, grant))
                require(not any(call.get("delegation_id") == grant["delegation_id"] and call["call_state"] in {"pending", "unknown"}
                    for call in task["calls"]), "SUBAGENT_CONFLICT", "진행 중이거나 미확인인 호출을 먼저 확인하세요.")
                _check_refs(runtime, task, grant, args["result"]["evidence_refs"])
                evidence = {ref: runtime._load_evidence(task, ref) for ref in args["result"]["evidence_refs"]}
                expiries = [item.get("expires_at") for item in evidence.values()]
                result = {**redact(args["result"]), "_source_paths": {
                    ref: item["allowed_paths"] for ref, item in evidence.items()}}
                grant["result_ref"] = runtime.store.write_record(task["task_id"], "subagent_result", result,
                    expires_at=min((value for value in expiries if value), default=None))
                grant["state"] = "completed"
            runtime._save(task)
            result = response(_view(runtime, task, grant) if operation == "complete" else _summary(task, grant))
        data = result["data"]
        if key:
            runtime.store.request_put(scope, key, fingerprint,
                {field: data[field] for field in ("delegation_id", "delegation_token") if field in data}, profile_id=task["profile_id"])
        return result


def run(runtime, args):
    operation = args["operation"]
    if operation in {"create", "complete", "revoke"}:
        return _mutate(runtime, args)
    with runtime.store.transaction():
        task = runtime._task(args["task_id"])
        grant = _authenticate(runtime, task, args["delegation_token"], completed=operation in {"get", "read"})
        if operation == "get":
            return response(_view(runtime, task, grant))
        if operation == "read":
            _check_refs(runtime, task, grant, [args["record_ref"]])
            # Memory bodies are returned by search; file pagination belongs to records.
            require(not args["record_ref"].startswith("memory_"), "INVALID_INPUT", "기억 본문은 하위 에이전트 검색에서 조회하세요.")
            return runtime._result_get({key: value for key, value in args.items()
                if key in {"task_id", "record_ref", "cursor", "limit_bytes"}}, task)
        if operation == "execute":
            call = args["call"]
            require(call["step_id"] in grant["step_ids"], "SCOPE_VIOLATION", "위임한 단계의 호출이 필요합니다.")
            plan = runtime._current_plan(task)
            step = next(item for item in plan["draft"]["steps"] if item["step_id"] == call["step_id"])
            capability = call.get("capability_id", step["capability_id"])
            require(capability != "*", "INVALID_INPUT", "읽기 묶음에서 실행할 실제 기능을 선택하세요.")
            execute = {"task_id": task["task_id"], **grant["plan_ref"], "approval_id": grant["approval_id"],
                "_delegation_id": grant["delegation_id"], "_timeout_ms": args.get("timeout_ms", 10000),
                **call, "capability_id": capability, **{key: step[key] for key in ("instance_id", "contract_ref")}}
        else:
            allowed = _instances(runtime, task, grant)
            selected = args.get("instance_ids", allowed)
            require(set(selected) <= set(allowed), "SCOPE_VIOLATION", "배정한 리소스의 기억만 검색하세요.")
            context = copy.deepcopy(task["context"])
            plan_ref = copy.deepcopy(task["active_plan_ref"])
            profile_id = task["profile_id"]
    if operation == "execute":
        result = runtime._execute(execute)
        # Preserve late observations before checking whether the subagent may receive them.
        with runtime.store.transaction():
            _authenticate(runtime, runtime._task(args["task_id"]), args["delegation_token"])
        return result
    found = runtime.memory.search(profile_id, redact(args["query"]), limit=args.get("limit", 10), instance_ids=selected)
    with runtime.store.transaction():
        task = runtime._task(args["task_id"])
        grant = _authenticate(runtime, task, args["delegation_token"])
        profile = runtime._current_profile(task)
        require(task["context"] == context and task["active_plan_ref"] == plan_ref,
            "SUBAGENT_UNAVAILABLE", "검색 중 하위 에이전트의 업무 맥락이 변경됐습니다.")
        found["items"] = [runtime.memory.get(profile_id, item["memory_ref"], instance_ids=selected) for item in found["items"]]
        found_refs = [item["memory_ref"] for item in found["items"]]
        if not set(found_refs) <= set(grant.get("memory_refs", [])):
            grant["memory_refs"] = sorted(set(grant.get("memory_refs", [])) | set(found_refs))
            runtime._save(task)
        ref = runtime.store.write_record(task["task_id"], "memory_search", {
            "context": context, "context_revision": task["context_revision"], "source_plan_ref": plan_ref,
            "query": redact(args["query"]), "memory_refs": found_refs,
            "limitations": found["limitations"], "retrieval_state": found["retrieval_state"],
            "delegation_id": grant["delegation_id"]})
        status = "ok" if not found["limitations"] else "error" if found["retrieval_state"] == "unavailable" else "partial"
        return response({"task_id": task["task_id"], "task_revision": task["task_revision"],
            "memory_search_ref": ref, "core_context": redact(profile["core_context"]), **found}, status,
            None if status == "ok" else "MEMORY_SEARCH_UNAVAILABLE" if status == "error" else "MEMORY_SEARCH_INCOMPLETE")
