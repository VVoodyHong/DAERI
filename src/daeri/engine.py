"""Task, approval and execution coordination; hosts retain the reasoning loop."""

import copy
import hashlib
import json
import os
import sqlite3
from time import monotonic
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import ValidationError
from jsonschema import Draft202012Validator

from .contracts import INPUT_MODELS
from .security import redact
from .storage import Store, StoreError, process_alive, retention_deadline
from .credentials import Credentials
from .memory import MemoryService
from .resources import ResourceRegistry
from .planning import evidence_matches_step, validate_plan
from .policy import (as_pointer, disclosure, effective_resource_policy, leaves,
                     pointer_get, pointer_path, pointer_set, project)

TERMINAL = {"succeeded", "partial", "failed", "cancelled"}


def identifier(prefix):
    return f"{prefix}_{uuid4().hex}"


def now():
    return datetime.now(UTC).isoformat(timespec="microseconds")


def response(data, status="ok", code=None, message=None):
    action = "fix_input"
    if code in {"REQUEST_ID_CONFLICT", "CALL_ID_CONFLICT", "ACCESS_DENIED", "STORE_UNAVAILABLE", "APPROVAL_UNAVAILABLE",
                "CONTEXT_CHANGED", "CONNECTION_NOT_READY", "AUTHENTICATION_REQUIRED", "UNSUPPORTED_CONSTRAINT",
                "RECORD_EXPIRED", "RECORD_UNAVAILABLE", "PROFILE_DELETING", "PROFILE_DELETED", "WORKER_UNAVAILABLE"}:
        action = "stop"
    elif code in {"NOT_FOUND", "REVISION_CONFLICT", "OWNER_CONFLICT", "CURSOR_INVALID", "TASK_STATE_INVALID",
                  "CONTRACT_CHANGED", "RESULT_TRUNCATED", "MEMORY_SEARCH_INCOMPLETE", "MEMORY_SEARCH_UNAVAILABLE",
                  "MEMORY_INDEX_INCOMPLETE", "MEMORY_CONFLICT", "WORKER_CONFLICT"}:
        action = "refresh"
    elif code in {"APPROVAL_REQUIRED", "APPROVAL_EXPIRED", "APPROVAL_REVOKED", "SCOPE_VIOLATION", "BUDGET_EXCEEDED"}:
        action = "request_approval"
    elif code in {"EXTERNAL_OUTCOME_UNKNOWN", "RESULT_PERSIST_FAILED"}:
        action = "reconcile"
    elif code == "INTENT_PERSIST_FAILED":
        action = "retry_same_request"
    return {"status": status, "data": data, "error": None if code is None else {
        "code": code, "message": message or "현재 요청을 처리할 수 없습니다.",
        "next_action": action}}


def require(condition, code, message):
    if not condition:
        raise StoreError(code, message)


class Engine:
    def __init__(self, data_dir, *, registry=None, memory=None, credentials=None):
        self.store = Store(data_dir)
        self.credentials = credentials or Credentials()
        self.registry = registry or ResourceRegistry(self.store, self.credentials.get)
        self.memory = memory or MemoryService(self.store)
        self.owner = f"{os.getpid()}:{uuid4().hex}"
        self.cursors = {}
        self._needs_cleanup = False
        self._stopped_calls = {}

    def close(self):
        self.store.close()

    def call(self, tool, arguments):
        result = self._call(tool, arguments)
        if tool == "daeri_result_submit" and result.get("status") == "ok":
            from .automatic_memory import finish_report
            result["data"]["memory"] = finish_report(self, result["data"])
        return result

    def _call(self, tool, arguments):
        args = None
        try:
            require(tool in INPUT_MODELS, "NOT_FOUND", "지원하지 않는 도구입니다.")
            require(isinstance(arguments, dict), "INVALID_INPUT", "JSON 객체를 전달하세요.")
            encoded = json.dumps(arguments, ensure_ascii=False, allow_nan=False)
            require(len(encoded.encode()) <= (2097152 if tool == "daeri_result_submit" else 65536), "INVALID_INPUT", "입력이 허용 크기를 초과했습니다.")
            args = INPUT_MODELS[tool].model_validate(arguments).model_dump(exclude_unset=True)
            self._flush_stopped_calls(args.get("task_id"))
            if tool == "daeri_worker":
                from .workers import run
                return run(self, args)
            if tool == "daeri_capability_execute":
                return self._execute(args)
            if tool == "daeri_memory_search":
                return self._memory_search(args, None)
            with self.store.transaction():
                fingerprint = hashlib.sha256(json.dumps(redact(args), sort_keys=True,
                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()
                scope = tool + ":" + args.get("task_id", "local")
                key = args.get("request_id")
                if key:
                    cached = self.store.request_get(scope, key)
                    if cached:
                        require(cached["fingerprint"] == fingerprint, "REQUEST_ID_CONFLICT",
                                "같은 요청 ID에 다른 입력이 전달됐습니다.")
                        return self._replay(tool, args, cached["response"])
                task = self._task(args["task_id"]) if "task_id" in args else None
                if task and "expected_task_revision" in args:
                    require(task["task_revision"] == args["expected_task_revision"],
                            "REVISION_CONFLICT", "최신 업무 개정으로 다시 요청하세요.")
                result = getattr(self, "_" + tool.removeprefix("daeri_"))(args, task)
                if tool == "daeri_plan_get":
                    for ref in result["data"]["plan"]["draft"]["memory_refs"]:
                        self.memory.get(task["profile_id"], ref)
                if key:
                    self.store.request_put(scope, key, fingerprint, self._cache(tool, result))
            if tool == "daeri_memory_update" and args["operation"] == "store":
                if result["data"]["index_state"] == "pending":
                    self.memory._index(task["profile_id"], result["data"]["memory_ref"])
                from .knowledge import replay
                return replay(self, args, result)
            return result
        except ValidationError:
            return response(None, "error", "INVALID_INPUT", "도구의 필드·자료형·필수값을 확인하세요.")
        except StoreError as error:
            if tool != "daeri_worker" and args and error.code == "PLAN_INVALID" and args.get("request_id") and args.get("task_id"):
                return self._plan_failure(tool, args, scope, fingerprint, error)
            return response(None, "error", error.code, error.message)
        except (ValueError, TypeError, RecursionError, UnicodeError):
            return response(None, "error", "INVALID_INPUT", "유효한 JSON 입력을 전달하세요.")
        except (OSError, sqlite3.Error):
            return response(None, "error", "STORE_UNAVAILABLE", "로컬 기록을 읽거나 보존할 수 없습니다.")
        finally:
            if self._needs_cleanup:
                self._needs_cleanup = False
                try:
                    self.store.cleanup()
                except StoreError:
                    pass  # Logical expiry still blocks reads; startup retries physical cleanup.

    def _plan_failure(self, tool, args, scope, fingerprint, error):
        # The rejected mutation has rolled back. Publish only its actual check.
        try:
            with self.store.transaction():
                existing = self.store.request_get(scope, args["request_id"])
                if existing:
                    require(existing["fingerprint"] == fingerprint, "REQUEST_ID_CONFLICT", "요청 ID가 이미 사용됐습니다.")
                    return self._replay(tool, args, existing["response"])
                self._task(args["task_id"])
                check = self.store.write_record(args["task_id"], "local_check", {
                    "check_kind": "plan", "outcome": "failed", "error_code": error.code,
                    "message": error.message})
                result = response({"local_check_ref": check}, "error", error.code, error.message)
                self.store.request_put(scope, args["request_id"], fingerprint, result)
                return result
        except StoreError as failure:
            return response(None, "error", failure.code, failure.message)

    def _task(self, task_id):
        task = self.store.get_task(task_id)
        if task["profile_id"]:
            self.store.get_profile(task["profile_id"])
        return task

    def _flush_stopped_calls(self, task_id):
        failures = {key: value for key, value in self._stopped_calls.copy().items() if key[0] == task_id}
        if not failures:
            return
        with self.store.transaction():
            task = self.store.get_task(task_id)
            changed = False
            for (_, call_id), (elapsed, observed_at) in failures.items():
                call = next(item for item in task["calls"] if item["call_id"] == call_id)
                if (call["call_state"] in {"pending", "unknown"} and not call.get("result_ref")
                        and call.get("error_code") != "RESULT_PERSIST_FAILED"):
                    if elapsed is None:
                        if call["dispatched"]:
                            continue
                        task["reserved"]["tool_calls"] -= 1
                    else:
                        task["usage"]["runtime_ms"] += elapsed - call["runtime_ms"]
                    task["reserved"]["runtime_ms"] = max(0, task["reserved"]["runtime_ms"] - call["reserved_runtime_ms"])
                    call.update(call_state="blocked" if elapsed is None else "unknown",
                        error_code="STORE_UNAVAILABLE" if elapsed is None else "RESULT_PERSIST_FAILED", runtime_ms=elapsed or 0,
                        observed_at=observed_at, reserved_runtime_ms=0)
                    changed = True
            if changed:
                self._save(task)
        for key in failures:
            self._stopped_calls.pop(key, None)

    def _bind(self, selection):
        require(not selection.get("department_id") and not selection.get("environment_id"),
                "INVALID_INPUT", "등록된 부처·환경 분류가 없습니다.")
        context = dict.fromkeys(("profile_id", "profile_revision", "profile_snapshot_ref",
                                 "department_id", "environment_id"))
        if not selection.get("profile_id"):
            return context, None
        profile = self.store.get_profile(selection["profile_id"])
        context.update(profile_id=profile["profile_id"], profile_revision=profile["revision"],
                       profile_snapshot_ref=identifier("snapshot"))
        return context, copy.deepcopy(profile)

    def _owner_status(self, task):
        owner = task.get("owner_id")
        if not owner:
            return "unassigned"
        if owner == self.owner:
            return "self"
        try:
            if not process_alive(int(owner.split(":", 1)[0])):
                return "recovery_required"
        except (ValueError, OverflowError, OSError):
            pass  # Uncertain ownership cannot justify takeover.
        return "other"

    def _own(self, task):
        require(self._owner_status(task) == "self", "OWNER_CONFLICT",
                "현재 실행 소유자에서 처리하거나 업무 재개로 소유권을 확인하세요.")

    def _save(self, task):
        revision = task["task_revision"]
        task["task_revision"] += 1
        task["last_activity_at"] = now()
        self.store.update_task(task, revision)

    def _receipt(self, task):
        result = {key: copy.deepcopy(task[key]) for key in (
            "task_id", "task_revision", "request_ref", "state", "context")}
        waiting = copy.deepcopy(task.get("waiting_reason"))
        if waiting and "record_ref" in waiting:
            waiting["message"] = self.store.read_record(task["task_id"], waiting.pop("record_ref"))["payload"]["question"]
        result["waiting_reason"] = waiting
        return result

    def _snapshot(self, task):
        return {**self._receipt(task), "active_plan_ref": task["active_plan_ref"],
                "last_report_ref": task.get("last_report_ref"), "active_calls": self._active_calls(task),
                "uncertain_effects": self._uncertain(task),
                "execution_owner_status": self._owner_status(task),
                "workers": [{key: item.get(key) for key in ("delegation_id", "state", "step_ids", "plan_ref",
                    "expires_at", "assignment_ref", "result_ref")} for item in task.get("delegations", [])]}

    def _active_calls(self, task):
        keys = ("call_id", "plan_id", "plan_version", "step_id", "call_state", "external_effect")
        return [{key: item[key] for key in keys} for item in task.get("calls", []) if item["call_state"] in {"pending", "unknown"}]

    def _uncertain(self, task):
        return [item["call_id"] for item in task.get("calls", []) if item["external_effect"] == "unknown"]

    def _invalidate(self, task):
        from .workers import invalidate
        invalidate(task)
        for approval in task["approvals"]:
            if approval["state"] in {"awaiting_response", "approved"}:
                approval["state"] = "invalidated"
                approval.pop("approval_id", None)

    def _cache(self, tool, result):
        # Persist references instead of turning body text into permanent cache data.
        data = result["data"]
        if tool == "daeri_approval_request":
            return {"approval_request_id": data["approval_request_id"]}
        if tool in {"daeri_task_create", "daeri_task_update"}:
            return {"task_id": data["task_id"], "clarification_ref": data.get("clarification_ref")}
        if tool == "daeri_task_resume":
            return {"task_id": data["task_id"]}
        if tool == "daeri_result_submit":
            return {"report_ref": data["report_ref"]}
        return copy.deepcopy(result)

    def _replay(self, tool, args, cached):
        if tool == "daeri_memory_update":
            from .knowledge import replay
            return replay(self, args, cached)
        if cached.get("status") == "error":
            if cached.get("data", {}).get("local_check_ref"):
                self.store.read_record(args["task_id"], cached["data"]["local_check_ref"])
            return copy.deepcopy(cached)
        if tool == "daeri_approval_request":
            task = self._task(args["task_id"])
            approval = next(a for a in task["approvals"]
                            if a["approval_request_id"] == cached["approval_request_id"])
            self.store.read_record(task["task_id"], self._plan_header(task, approval["plan_id"])["body_ref"])
            return self._approval_reply(task, approval)
        if tool in {"daeri_task_create", "daeri_task_update"}:
            task = self._task(cached["task_id"])
            data = self._receipt(task)
            if cached.get("clarification_ref"):
                self.store.read_record(task["task_id"], cached["clarification_ref"])
                data["clarification_ref"] = cached["clarification_ref"]
            return response(data)
        if tool == "daeri_plan_submit":
            task = self._task(args["task_id"])
            self.store.read_record(task["task_id"], self._plan_header(task, cached["data"]["plan_id"])["body_ref"])
        if tool == "daeri_task_resume":
            return self._resume_reply(self._task(cached["task_id"]))
        if tool == "daeri_result_submit":
            task = self._task(args["task_id"])
            payload = self._load_evidence(task, cached["report_ref"], require_current_plan=False)["payload"]
            return response({"task_id": task["task_id"], "task_revision": task["task_revision"],
                "report_ref": cached["report_ref"], "task_state": task["state"], "delivery_status": "not_confirmed",
                **{key: payload[key] for key in ("validation", "table_validations", "artifact_refs", "artifact_validations")}})
        return copy.deepcopy(cached)

    def _task_create(self, args, unused):
        context, policy = self._bind(args.get("context", {}))
        task = {"task_id": identifier("task"), "profile_id": context["profile_id"],
                "task_revision": 0, "state": "planning" if policy else "awaiting_input",
                "context": context, "profile_snapshot": policy, "owner_id": self.owner,
                "request_ref": "", "context_revision": 1, "active_plan_ref": None,
                "plans": [], "approvals": [], "calls": [], "last_report_ref": None,
                "usage": {"tool_calls": 0, "external_requests": 0, "runtime_ms": 0, "resource_limits": []},
                "reserved": {"tool_calls": 0, "external_requests": 0, "runtime_ms": 0, "resource_limits": []},
                "waiting_reason": None, "created_at": now(), "last_activity_at": now()}
        if not policy:
            task["waiting_reason"] = {"kind": "input", "message": "사용할 profile을 선택하세요."}
        self.store.insert_task(task)
        task["request_ref"] = self.store.write_record(task["task_id"], "request", redact({
            "request_text": args["request_text"], "desired_output": args.get("desired_output")}))
        self._save(task)
        return response(self._receipt(task))

    def _task_update(self, args, task):
        require(task["state"] not in TERMINAL, "TASK_STATE_INVALID", "종료 업무는 변경할 수 없습니다.")
        if args.get("action") == "pause":
            if task["state"] != "paused":
                task["paused_waiting"] = task["waiting_reason"]
                task["state"] = "paused"
                task["waiting_reason"] = {"kind": "pause", "message": "사용자 요청으로 일시중지했습니다."}
                task["owner_id"] = None
                self._invalidate(task)
                self._save(task)
            return response(self._receipt(task))
        self._own(task)
        patch = args["patch"]
        if "question" in patch:
            require(task["state"] in {"planning", "awaiting_input", "running", "reconciling", "verifying"},
                    "TASK_STATE_INVALID", "현재 상태에서는 질문을 추가할 수 없습니다.")
            ref = self.store.write_record(task["task_id"], "clarification", redact(patch))
            task.setdefault("clarification_times", {})[ref] = now()
            if task["state"] != "awaiting_input":
                task["return_state"] = task["state"]
            task["waiting_reason"] = {"kind": "input", "record_ref": ref}
            task["state"] = "awaiting_input"
        else:
            require(task["state"] in {"planning", "awaiting_input"}, "TASK_STATE_INVALID",
                    "보완은 계획 작성 또는 입력 대기 중에만 가능합니다.")
            if "context" in patch:
                require(not any(a.get("ever_approved") for a in task["approvals"]),
                        "TASK_STATE_INVALID", "승인 이력이 있는 업무의 profile은 변경할 수 없습니다.")
                context, policy = self._bind(patch["context"])
                task.update(context=context, profile_id=context["profile_id"], profile_snapshot=policy,
                            active_plan_ref=None)
                self._invalidate(task)
            ref = self.store.write_record(task["task_id"], "clarification", redact(patch))
            task.setdefault("clarification_times", {})[ref] = now()
            task["context_revision"] += 1
            task["waiting_reason"] = None
            task["state"] = "planning" if task["profile_id"] else "awaiting_input"
            if not task["profile_id"]:
                task["waiting_reason"] = {"kind": "input", "message": "사용할 profile을 선택하세요."}
            elif task.get("return_state") in {"running", "reconciling", "verifying"} and self._active_approved(task):
                task["state"] = task.pop("return_state")
        self._save(task)
        return response({**self._receipt(task), "clarification_ref": ref})

    def _page(self, kind, target, signature, args, values):
        offset, limit = 0, args.get("limit", 20)
        if args.get("cursor"):
            cursor = self.cursors.get(args["cursor"])
            require(isinstance(cursor, tuple) and cursor[:3] == (kind, target, signature), "CURSOR_INVALID", "목록을 처음부터 다시 조회하세요.")
            offset = cursor[3]
        items = values(offset, limit + 1)
        more = len(items) > limit
        cursor = None
        if more:
            if len(self.cursors) >= 256:
                self.cursors.clear()
            cursor = identifier("cursor")
            self.cursors[cursor] = (kind, target, signature, offset + limit)
        return items[:limit], cursor, more

    def _task_get(self, args, task):
        if task:
            records = [item for item in self.store.list_records(task["task_id"])
                       if item["record_kind"] not in {"memory_pending", "memory_done"}]
            page, cursor, more = self._page("records", task["task_id"],
                (task["task_revision"], len(records)), args, lambda offset, limit: records[offset:offset+limit])
            return response({"task_id": task["task_id"], "task_revision": task["task_revision"],
                             "task": self._snapshot(task), "records": page, "next_cursor": cursor, "has_more": more})
        profile = self.store.get_profile(args["profile_id"])
        signature = self.store.task_page_signature(profile["profile_id"])
        page, cursor, more = self._page("tasks", profile["profile_id"], signature, args,
            lambda offset, limit: self.store.list_tasks(profile["profile_id"], limit, offset=offset))
        return response({"profile_id": profile["profile_id"], "tasks": [{
            "task_id": item["task_id"], "title": self._title(item),
            "state": item["state"], "last_activity_at": item["last_activity_at"]} for item in page],
            "next_cursor": cursor, "has_more": more})

    def _title(self, task):
        try:
            body = self.store.read_record(task["task_id"], task["request_ref"])["payload"]
            return " ".join(body["request_text"].split())[:80]
        except StoreError as error:
            if error.code not in {"RECORD_EXPIRED", "RECORD_UNAVAILABLE"}:
                raise
            return "업무 " + task["task_id"].removeprefix("task_")[:8]

    def _task_resume(self, args, task):
        owner_status = self._owner_status(task)
        require(owner_status != "other", "OWNER_CONFLICT", "다른 실행 소유자가 업무를 처리하고 있습니다.")
        recovered_calls = False
        for call in task.get("calls", []):
            if call["call_state"] != "pending":
                continue
            executor_lost = False
            if call.get("execution_owner_id"):
                try:
                    executor_lost = not process_alive(int(call["execution_owner_id"].split(":", 1)[0]))
                except (ValueError, OverflowError, OSError):
                    pass
            if owner_status == "recovery_required" or executor_lost:
                recovered_calls = True
                for grant in task.get("delegations", []):
                    if grant["delegation_id"] == call.get("delegation_id") and grant["state"] == "active":
                        grant["state"] = "revoked"
                if call["dispatched"]:
                    # A dead process cannot prove whether its dispatched read completed.
                    # Retain its unsettled runtime reservation instead of assuming zero.
                    call.update(call_state="unknown", error_code="EXTERNAL_OUTCOME_UNKNOWN")
                else:
                    call.update(call_state="blocked", error_code="OWNER_CONFLICT")
                    task["reserved"]["tool_calls"] -= 1
                    task["reserved"]["runtime_ms"] -= call["reserved_runtime_ms"]
                    call["reserved_runtime_ms"] = 0
        if task["state"] not in TERMINAL:
            paused = task["state"] == "paused"
            changed_owner = task["owner_id"] != self.owner
            task["owner_id"] = self.owner
            if paused:
                self._invalidate(task)
                task.pop("return_state", None)
            waiting = task.pop("paused_waiting", None) if paused else task["waiting_reason"]
            previous_state = task["state"]
            if waiting and waiting["kind"] == "input":
                task["state"], task["waiting_reason"] = "awaiting_input", waiting
            elif not task["profile_id"]:
                task["state"], task["waiting_reason"] = "awaiting_input", {"kind": "input", "message": "profile을 선택하세요."}
            elif task["active_plan_ref"]:
                header = self._plan_header(task, task["active_plan_ref"]["plan_id"])
                body = self.store.read_record(task["task_id"], header["body_ref"])["payload"]
                try:
                    self._validate_searches(task, body["draft"], header["source_plan_ref"])
                except StoreError as error:
                    if error.code != "PLAN_INVALID":
                        raise
                    self._invalidate(task)
                    task["state"], task["waiting_reason"] = "planning", {"kind": "scope", "message": "계획 전제가 변경되어 재조회·수정 계획이 필요합니다."}
                else:
                    if not paused and self._active_approved(task):
                        task["state"], task["waiting_reason"] = "running", None
                    else:
                        task["state"], task["waiting_reason"] = "awaiting_approval", {"kind": "approval", "message": "계획을 다시 확인하고 승인하세요."}
            else:
                task["state"], task["waiting_reason"] = "planning", None
            if paused or changed_owner or task["state"] != previous_state or recovered_calls:
                if changed_owner:
                    from .workers import invalidate
                    invalidate(task)
                self._save(task)
        elif task.get("owner_id") != self.owner or recovered_calls:
            task["owner_id"] = self.owner
            self._save(task)
        return self._resume_reply(task)

    def _resume_reply(self, task):
        receipt = self._receipt(task)
        unresolved = [call["call_id"] for call in self._active_calls(task)]
        return response({key: receipt[key] for key in ("task_id", "task_revision", "state", "waiting_reason")} |
                        {"resumed": task["state"] == "running" and self._active_approved(task) and not unresolved,
                         "active_plan_ref": task["active_plan_ref"], "unresolved_call_ids": unresolved},
                        "ok" if task["state"] in TERMINAL | {"running"} and not unresolved else "pending")

    def _task_cancel(self, args, task):
        require(task["state"] not in TERMINAL - {"cancelled"}, "TASK_STATE_INVALID", "다른 종료 상태를 취소로 바꿀 수 없습니다.")
        if task["state"] != "cancelled":
            if args.get("reason"):
                self.store.write_record(task["task_id"], "clarification", redact({"cancel_reason": args["reason"]}))
            self._invalidate(task)
            self._finish(task, "cancelled")
            task["owner_id"] = None
            self._save(task)
        return response({"task_id": task["task_id"], "task_revision": task["task_revision"],
                         "state": "cancelled", "active_calls": self._active_calls(task), "uncertain_effects": self._uncertain(task)})

    def _resource_list(self, args, task):
        require(task["profile_id"], "CONTEXT_REQUIRED", "profile을 먼저 선택하세요.")
        from .results import ResultService
        profile = self._current_profile(task)
        items = self.registry.list(profile)
        if args.get("types"):
            items = [item for item in items if item["type"] in args["types"]]
        signature = hashlib.sha256(json.dumps([items, args.get("types")], sort_keys=True).encode()).hexdigest()
        page, cursor, more = self._page("resources", task["task_id"], signature, args,
                                      lambda offset, limit: items[offset:offset+limit])
        return response({"task_id": task["task_id"], "task_revision": task["task_revision"],
                         "instances": page, "output_formats": ResultService(self.store).formats(),
                         "next_cursor": cursor, "has_more": more})

    def _current_profile(self, task):
        require(task["profile_id"], "CONTEXT_REQUIRED", "profile을 먼저 선택하세요.")
        profile = self.store.get_profile(task["profile_id"])
        require(profile["revision"] == task["context"]["profile_revision"], "CONTEXT_CHANGED",
                "고정한 profile 구성이 변경됐습니다. 기존 승인을 새 대상에 적용할 수 없습니다.")
        return profile

    def _capability_get(self, args, task):
        descriptor = self.registry.get(self._current_profile(task), args["instance_id"], args["capability_id"])
        return response({"task_id": task["task_id"], "task_revision": task["task_revision"], "descriptor": descriptor})

    def _memory_search(self, args, task):
        from .automatic_memory import retry_profile
        retry_profile(self, self._task(args["task_id"])["profile_id"])
        with self.store.transaction():
            task = self._task(args["task_id"])
            profile = self._current_profile(task)
            revision = task["task_revision"]
        found = self.memory.search(profile["profile_id"], redact(args["query"]),
                                   limit=min(args.get("limit", 10), 50), instance_ids=args.get("instance_ids"))
        with self.store.transaction():
            task = self._task(args["task_id"])
            profile = self._current_profile(task)
            require(task["task_revision"] == revision, "REVISION_CONFLICT", "검색 중 변경된 업무의 현재 맥락을 다시 확인하세요.")
            found["items"] = [self.memory.get(profile["profile_id"], item["memory_ref"], instance_ids=args.get("instance_ids"))
                              for item in found["items"]]
            payload = {"context": task["context"], "context_revision": task["context_revision"],
                       "source_plan_ref": task["active_plan_ref"], "query": redact(args["query"]),
                       "memory_refs": [item["memory_ref"] for item in found["items"]],
                       "limitations": found["limitations"], "retrieval_state": found["retrieval_state"]}
            ref = self.store.write_record(task["task_id"], "memory_search", payload)
            complete = not found["limitations"] and found["retrieval_state"] != "unavailable"
            status = "ok" if complete else "error" if found["retrieval_state"] == "unavailable" else "partial"
            code = None if status == "ok" else "MEMORY_SEARCH_UNAVAILABLE" if status == "error" else "MEMORY_SEARCH_INCOMPLETE"
            return response({"task_id": task["task_id"], "task_revision": task["task_revision"],
                             "memory_search_ref": ref, "core_context": redact(profile["core_context"]), **found},
                             status, code, "현재 검색 상태와 제한을 계획에 반영하세요." if code else None)

    def _plan_header(self, task, plan_id):
        header = next((p for p in task["plans"] if p["plan_id"] == plan_id), None)
        require(header is not None, "NOT_FOUND", "업무에 속한 계획을 찾을 수 없습니다.")
        return header

    def _validate_searches(self, task, draft, source_plan_ref):
        profile = self.store.get_profile(task["profile_id"])
        require(profile["revision"] == task["context"]["profile_revision"], "PLAN_INVALID", "profile 구성이 변경되어 새 계획이 필요합니다.")
        retrieved, limitations = set(), []
        for ref in draft["memory_search_refs"]:
            try:
                record = self.store.read_record(task["task_id"], ref)
            except StoreError:
                raise StoreError("PLAN_INVALID", "현재 업무의 유효한 기억 검색 기록이 필요합니다.") from None
            payload = record["payload"]
            require(record["record_kind"] == "memory_search" and
                    payload["context"] == task["context"] and
                    payload["context_revision"] == task["context_revision"] and
                    payload["source_plan_ref"] == source_plan_ref,
                    "PLAN_INVALID", "요청·profile·계획 맥락에 맞게 기억을 다시 조회하세요.")
            retrieved.update(payload.get("memory_refs", []))
            limitations.extend(payload.get("limitations", []))
        require(set(draft["memory_refs"]) <= retrieved, "PLAN_INVALID", "실제 검색 결과에 포함된 기억만 선택하세요.")
        for ref in draft["memory_refs"]:
            try:
                self.memory.get(task["profile_id"], ref)
            except StoreError:
                raise StoreError("PLAN_INVALID", "선택 기억의 기한·개정·현재 권한이 바뀌었습니다.") from None
        return list(dict.fromkeys(limitations))

    def _budget(self, header, task=None):
        budget = header["budget"]
        zero = {"tool_calls": 0, "external_requests": 0, "runtime_ms": 0, "resource_limits": []}
        usage = (task or {}).get("usage", zero)
        reserved = (task or {}).get("reserved", zero)
        def remaining(limit, kind):
            return None if limit is None or usage[kind] is None or reserved[kind] is None else max(0, limit - usage[kind] - reserved[kind])
        return {"previous_budget": header["previous_budget"], "consumed": copy.deepcopy(usage), "reserved": copy.deepcopy(reserved),
                "remaining": {"remaining_tool_calls": remaining(budget["max_tool_calls"], "tool_calls"),
                              "remaining_external_requests": remaining(budget["max_external_requests"], "external_requests"),
                              "remaining_runtime_ms": remaining(budget["max_runtime_ms"], "runtime_ms"), "resource_limits": []}}

    def _plan_submit(self, args, task):
        self._own(task)
        active = task["active_plan_ref"]
        require(task["state"] in ({"planning", "awaiting_approval", "running", "reconciling", "verifying"} if active else {"planning"}),
                "TASK_STATE_INVALID", "현재 상태에서 계획을 제출할 수 없습니다.")
        require(task["profile_id"], "PLAN_INVALID", "profile을 먼저 선택하세요.")
        draft = redact(copy.deepcopy(args["draft"]))
        draft.setdefault("carried_evidence_refs", [])
        require(draft.get("previous_plan_id") == (active["plan_id"] if active else None),
                "PLAN_INVALID", "대체할 현재 계획 ID를 정확히 지정하세요.")
        from .results import ResultService
        limitations = self._validate_searches(task, draft, active)
        draft["memory_limitations"] = list(dict.fromkeys([*draft["memory_limitations"], *limitations]))
        instances = set()
        for ref in draft["memory_refs"]:
            instances.update(self.memory.get(task["profile_id"], ref)["instance_ids"])
        for ref in draft["carried_evidence_refs"]:
            evidence = self._load_evidence(task, ref, mode="export", require_current_plan=False)
            if evidence.get("instance_id"):
                instances.add(evidence["instance_id"])
        from .planning import resolve_memory_defaults
        profile = self._current_profile(task)
        draft = resolve_memory_defaults(profile, draft)
        contracts = validate_plan(profile, draft, self.registry,
                                  ResultService(self.store).formats(), instances)
        for limit, amount in (("max_tool_calls", "tool_calls"), ("max_runtime_ms", "runtime_ms"), ("max_external_requests", "external_requests")):
            if draft["budget"][limit] is not None:
                consumed, reserved = task["usage"][amount], task["reserved"][amount]
                require(consumed is not None and reserved is not None and draft["budget"][limit] >= consumed + reserved,
                        "PLAN_INVALID", "새 전체 한도는 누적 소비·미정산 예약보다 작거나 미확정량을 0으로 바꿀 수 없습니다.")
        version = len(task["plans"]) + 1
        policy = task["profile_snapshot"]
        header = {"plan_id": identifier("plan"), "plan_version": version,
                  "context": copy.deepcopy(task["context"]), "source_plan_ref": active,
                  "contract_refs": contracts, "budget": draft["budget"],
                  "previous_budget": self._plan_header(task, active["plan_id"])["budget"] if active else None,
                  "retention_context": {"task_body_days": policy["retention_days"],
                                        "long_term_memory_days": policy["long_term_memory_days"]}}
        header["body_ref"] = self.store.write_record(task["task_id"], "plan", {"draft": draft})
        header["render_ref"] = header["body_ref"]
        task["plans"].append(header)
        task["active_plan_ref"] = {"plan_id": header["plan_id"], "plan_version": version}
        task["state"], task["waiting_reason"] = "awaiting_approval", {"kind": "approval", "message": "저장한 계획과 허용 범위를 확인하세요."}
        self._invalidate(task)
        self._save(task)
        return response({"task_id": task["task_id"], "task_revision": task["task_revision"],
                         **{key: header[key] for key in ("plan_id", "plan_version", "contract_refs", "render_ref", "retention_context")},
                         "budget_context": self._budget(header, task)})

    def _plan_get(self, args, task):
        header = self._plan_header(task, args["plan_id"])
        body = self.store.read_record(task["task_id"], header["body_ref"])["payload"]
        approval = next((a for a in reversed(task["approvals"]) if a["plan_id"] == header["plan_id"]), None)
        saved = {**{key: header[key] for key in ("plan_id", "plan_version", "context", "contract_refs", "render_ref", "retention_context")},
                         "draft": body["draft"], "is_active": task["active_plan_ref"] == {"plan_id": header["plan_id"], "plan_version": header["plan_version"]},
                         "approval_request": self._outcome(task, approval) if approval else None,
                         "budget_context": self._budget(header, task)}
        return response({"task_id": task["task_id"], "task_revision": task["task_revision"], "plan": saved})

    def _active_approved(self, task):
        return any(self._approval_valid(task, approval) for approval in task["approvals"])

    def _approval_valid(self, task, approval):
        if (approval["state"] != "approved" or task["state"] in TERMINAL | {"paused"}
                or approval["plan_id"] != (task["active_plan_ref"] or {}).get("plan_id")):
            return False
        header = self._plan_header(task, approval["plan_id"])
        body = self.store.read_record(task["task_id"], header["body_ref"])["payload"]
        try:
            self._validate_searches(task, body["draft"], header["source_plan_ref"])
        except StoreError as error:
            if error.code != "PLAN_INVALID":
                raise
            return False
        return True

    def _outcome(self, task, approval):
        outcome = {key: approval[key] for key in ("approval_request_id", "state", "decision")}
        outcome["feedback"] = self.store.read_record(task["task_id"], approval["feedback_ref"])["payload"]["user_response"] if approval.get("feedback_ref") else None
        valid = self._approval_valid(task, approval)
        if approval["state"] == "approved" and not valid:
            outcome["state"] = "invalidated"
        if valid:
            outcome["approval_id"] = approval["approval_id"]
        return outcome

    def _approval_reply(self, task, approval):
        return response({"task_id": task["task_id"], "task_revision": task["task_revision"],
                         **self._outcome(task, approval)},
                        "pending" if approval["state"] == "awaiting_response" else "ok")

    def _approval_request(self, args, task):
        if args["stage"] == "request":
            self._own(task)
        require(task["state"] == "awaiting_approval", "TASK_STATE_INVALID", "승인 대기 업무에서만 응답할 수 있습니다.")
        target = {"plan_id": args["plan_id"], "plan_version": args["plan_version"]}
        require(target == task["active_plan_ref"], "APPROVAL_UNAVAILABLE", "현재 계획과 승인 대상이 다릅니다.")
        header = self._plan_header(task, args["plan_id"])
        body = self.store.read_record(task["task_id"], header["body_ref"])["payload"]
        if args["stage"] == "request":
            require(not any(a["state"] == "awaiting_response" for a in task["approvals"]),
                    "APPROVAL_UNAVAILABLE", "이미 응답을 기다리는 승인 요청이 있습니다.")
            approval = {"approval_request_id": identifier("approval_request"), **target,
                        "state": "awaiting_response", "decision": None, "feedback_ref": None}
            task["approvals"].append(approval)
        else:
            approval = next((a for a in task["approvals"] if a["approval_request_id"] == args["approval_request_id"]), None)
            require(approval and approval["state"] == "awaiting_response" and
                    approval["plan_id"] == args["plan_id"] and approval["plan_version"] == args["plan_version"],
                    "APPROVAL_UNAVAILABLE", "유효한 대기 요청과 실제 응답을 연결할 수 없습니다.")
            if args.get("user_message_ref"):
                scope = "user_message:" + args["user_message_ref"]
                fingerprint = hashlib.sha256(json.dumps({"target": target, "task_id": task["task_id"],
                    "request_id": approval["approval_request_id"], "reply": redact(args["user_response"]),
                    "decision": args["decision"]}, sort_keys=True).encode()).hexdigest()
                existing = self.store.request_get(scope, "message")
                require(not existing or existing["fingerprint"] == fingerprint, "APPROVAL_UNAVAILABLE", "사용자 메시지 참조를 다른 답변·계획에 재사용할 수 없습니다.")
                self.store.request_put(scope, "message", fingerprint, {}, profile_id=task['profile_id'])
            if args["decision"] == "approve":
                self._validate_searches(task, body["draft"], header["source_plan_ref"])
            approval["feedback_ref"] = self.store.write_record(task["task_id"], "approval", redact({
                "decision": args["decision"], "user_response": args["user_response"]}))
            approval["decision"] = args["decision"]
            if args["decision"] == "approve":
                approval.update(state="approved", approval_id=identifier("approval"), ever_approved=True)
                task["state"], task["waiting_reason"] = "running", None
            elif args["decision"] in {"request_changes", "decline"}:
                approval["state"] = "rejected"
                task["state"], task["waiting_reason"] = "planning", None
            elif args["decision"] == "cancel":
                approval["state"] = "cancelled"
        self._save(task)
        return self._approval_reply(task, approval)

    def _current_plan(self, task):
        require(task["active_plan_ref"], "APPROVAL_REQUIRED", "먼저 계획을 저장하고 승인받으세요.")
        return self._plan_get({"plan_id": task["active_plan_ref"]["plan_id"]}, task)["data"]["plan"]

    def _resource_rule(self, plan, instance_id):
        rule = next((item for item in plan["draft"]["output"]["resource_rules"] if item["instance_id"] == instance_id), None)
        require(rule is not None, "ACCESS_DENIED", "해당 리소스의 자료 제공 규칙이 없습니다.")
        return rule

    def _resource_config(self, task, instance_id):
        return next(item for item in self._current_profile(task)["resources"] if item["instance_id"] == instance_id)

    def _bindings(self, task, plan, step, args):
        definitions = {item["binding_id"]: item for item in step["input_bindings"]}
        selections = args.get("bindings", [])
        require(len({item["binding_id"] for item in selections}) == len(selections) and
                {item["binding_id"] for item in selections} == set(definitions), "BINDING_INVALID", "계획의 모든 바인딩을 정확히 선택하세요.")
        parameters, values, sources = copy.deepcopy(args["parameters"]), {}, {}
        missing = object()
        for selected in selections:
            binding = definitions[selected["binding_id"]]
            call = next((item for item in task["calls"] if item["call_id"] == selected["source_call_id"]), None)
            require(call and call["step_id"] == binding["source_step_id"] and call.get("result_ref"),
                    "BINDING_INVALID", "실제 선행 호출의 보존 결과가 필요합니다.")
            require(binding["source_step_id"] not in sources or sources[binding["source_step_id"]] == call["call_id"],
                    "BINDING_INVALID", "같은 선행 단계는 동일한 출처 호출을 사용하세요.")
            sources[binding["source_step_id"]] = call["call_id"]
            evidence = self._load_evidence(task, call["result_ref"], mode="model")
            source_step = next(item for item in plan["draft"]["steps"] if item["step_id"] == binding["source_step_id"])
            require(evidence_matches_step(evidence, source_step), "BINDING_INVALID",
                    "승계한 출처의 대상·기능·계약·범위가 현재 선행 단계와 다릅니다.")
            try:
                value = pointer_get(evidence["result"], binding["source_pointer"])
                path = pointer_path(evidence["result"], binding["source_pointer"])
            except StoreError:
                raise StoreError("BINDING_INVALID", "가림·누락된 값을 실제 부재로 추정하거나 바인딩할 수 없습니다.") from None
            require(all(as_pointer(leaf) in evidence["allowed_paths"] for leaf in leaves(value, path)),
                    "BINDING_INVALID", "바인딩 필드의 현재 모델 제공 권한이 없습니다.")
            values[binding["binding_id"]] = value
            if binding["target_pointer"] is not None:
                try:
                    provided = pointer_get(parameters, binding["target_pointer"])
                except StoreError:
                    provided = missing
                require(provided is missing or json.dumps(provided, sort_keys=True) == json.dumps(value, sort_keys=True),
                        "BINDING_INVALID", "모델이 전달한 바인딩 값이 보존 원본과 다릅니다.")
                pointer_set(parameters, binding["target_pointer"], value)
        for condition in step.get("conditions", []):
            value = values[condition["binding_id"]]
            operator = condition["operator"]
            permitted = value is not missing if operator == "exists" else (
                type(value) is type(condition["value"]) and value == condition["value"]) if operator == "eq" else any(
                type(value) is type(item) and value == item for item in condition["value"])
            require(permitted, "CONDITION_NOT_MET", "승인한 분기 조건이 충족되지 않았습니다.")
        for dependency in step["depends_on"]:
            if dependency in sources:
                continue
            source_step = next(item for item in plan["draft"]["steps"] if item["step_id"] == dependency)
            candidates = (item for item in task["calls"] if item["step_id"] == dependency
                and item["call_state"] == "completed" and item.get("result_ref")
                and (item["plan_id"] == plan["plan_id"] or item["result_ref"] in plan["draft"]["carried_evidence_refs"]))
            require(any(evidence_matches_step(self._load_evidence(task, item["result_ref"]), source_step)
                        for item in candidates), "BINDING_INVALID", "선행 단계의 정상 완료 근거가 필요합니다.")
        return parameters

    def _execution_allowed(self, task, plan, args):
        from .workers import check_execution
        if args.get("_delegation_id"):
            check_execution(self, task, args)
        else:
            self._own(task)
            require(not any(item["state"] == "active" and item["plan_ref"] == task["active_plan_ref"]
                and datetime.fromisoformat(item["expires_at"]) > datetime.now(UTC)
                and args["step_id"] in item["step_ids"] for item in task.get("delegations", [])),
                "WORKER_CONFLICT", "이 단계는 활성 작업자에게 위임되어 있습니다.")
        require(task["state"] in {"running", "verifying"}, "TASK_STATE_INVALID", "현재 업무 상태에서는 새 호출을 시작할 수 없습니다.")
        require(not any(call.get("error_code") == "RESULT_PERSIST_FAILED" and call["call_state"] == "unknown"
            for call in task["calls"]), "RESULT_PERSIST_FAILED", "보존에 실패한 원 호출을 확인하고 업무 결과를 정리하세요.")
        require({"plan_id": args["plan_id"], "plan_version": args["plan_version"]} == task["active_plan_ref"],
                "APPROVAL_REQUIRED", "현재 계획과 호출 대상이 일치하지 않습니다.")
        require(any(approval.get("approval_id") == args["approval_id"] and self._approval_valid(task, approval)
                    for approval in task["approvals"]), "APPROVAL_REQUIRED", "현재 유효한 실제 사용자 승인이 필요합니다.")
        require(not args.get("reconcile_of_call_id") and not args.get("reconcile_task_id"),
                "UNSUPPORTED_CONSTRAINT", "현재 등록한 읽기 기능은 원격 변경 확인을 지원하지 않습니다.")
        step = next((item for item in plan["draft"]["steps"] if item["step_id"] == args["step_id"]), None)
        require(step and all(step[key] == args[key] for key in ("instance_id", "capability_id", "contract_ref")),
                "SCOPE_VIOLATION", "승인한 단계·대상·기능·계약과 다릅니다.")
        return step

    def _execute(self, args):
        fingerprint = hashlib.sha256(json.dumps(redact(args), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        scope, key = "call:" + args["task_id"], args["call_id"]
        with self.store.transaction():
            task = self._task(args["task_id"])
            if args.get("_delegation_id"):
                from .workers import check_execution
                check_execution(self, task, args)
            cached = self.store.request_get(scope, key)
            if cached:
                require(cached["fingerprint"] == fingerprint, "CALL_ID_CONFLICT", "같은 호출 ID에 다른 입력이 전달됐습니다.")
                call = next(item for item in task["calls"] if item["call_id"] == key)
                return self._call_reply(task, call)
            if not args.get("_delegation_id"):
                require(task["task_revision"] == args["expected_task_revision"], "REVISION_CONFLICT", "업무를 다시 읽고 현재 개정으로 요청하세요.")
            require(not any(item["step_id"] == args["step_id"] and item["call_state"] in {"pending", "unknown"}
                for item in task["calls"]), "WORKER_CONFLICT", "같은 단계의 진행 중 호출을 먼저 확인하세요.")
            plan = self._current_plan(task)
            step = self._execution_allowed(task, plan, args)
            parameters = self._bindings(task, plan, step, args)
            invocation = self.registry.normalize(self._current_profile(task), step["instance_id"], step["capability_id"], parameters, step["scope"])
            output_schema = self.registry.get(self._current_profile(task), step["instance_id"], step["capability_id"])["output_schema"]
            require(invocation.contract_ref == step["contract_ref"], "CONTRACT_CHANGED", "실행할 계약이 승인 당시와 다릅니다.")
            header = self._plan_header(task, plan["plan_id"])
            budget = self._budget(header, task)["remaining"]
            require(budget["remaining_tool_calls"] is not None and budget["remaining_tool_calls"] >= 1 and
                    budget["remaining_runtime_ms"] is not None and budget["remaining_runtime_ms"] > 0,
                    "BUDGET_EXCEEDED", "업무의 누적 호출·시간 한도가 소진됐습니다.")
            timeout_ms = min(budget["remaining_runtime_ms"], args.get("_timeout_ms", 60000))
            call = {key: args[key] for key in ("call_id", "plan_id", "plan_version", "step_id", "instance_id", "capability_id", "contract_ref")}
            call.update(call_state="pending", external_effect="none", result_ref=None, error_code=None,
                        dispatched=False, runtime_ms=0, reserved_runtime_ms=timeout_ms,
                        query_fingerprint=invocation.fingerprint, observed_at=now())
            if args.get("_delegation_id"):
                call["delegation_id"] = args["_delegation_id"]
                call["execution_owner_id"] = self.owner
            call["intent_ref"] = self.store.write_record(task["task_id"], "call_result", {
                "call_id": key, "intent": redact(invocation.prepared_request), "result": None})
            task["calls"].append(call)
            task["reserved"]["tool_calls"] += 1
            task["reserved"]["runtime_ms"] += timeout_ms
            self.store.request_put(scope, key, fingerprint, {"call_id": key})
            self._save(task)
        # The durable intent commits before dispatch. Pause/cancel remain available during I/O.
        try:
            with self.store.transaction():
                task = self._task(args["task_id"])
                call = next(item for item in task["calls"] if item["call_id"] == key)
                try:
                    self._execution_allowed(task, self._current_plan(task), args)
                except StoreError as error:
                    call.update(call_state="blocked", error_code=error.code)
                    task["reserved"]["tool_calls"] -= 1
                    task["reserved"]["runtime_ms"] -= timeout_ms
                    self._save(task)
                    return self._call_reply(task, call)
                call["dispatched"] = True
                task["reserved"]["tool_calls"] -= 1
                task["usage"]["tool_calls"] += 1
                task["usage"]["external_requests"] = None
                self._save(task)
        except (StoreError, OSError, sqlite3.Error):
            # This executor stopped before I/O; the committed intent must not stay in flight.
            self._stopped_calls[(args["task_id"], key)] = (None, now())
            try:
                self._flush_stopped_calls(args["task_id"])
            except (StoreError, OSError, sqlite3.Error):
                pass
            raise
        started = monotonic()
        outcome, failure = None, None
        try:
            outcome = self.registry.execute(invocation, timeout=timeout_ms / 1000)
        except StoreError as error:
            failure = error
        except Exception:
            failure = StoreError("EXTERNAL_OUTCOME_UNKNOWN", "호출 결과를 확인할 수 없습니다.")
        elapsed = max(0, int((monotonic() - started) * 1000))
        try:
            with self.store.transaction():
                task = self._task(args["task_id"])
                call = next(item for item in task["calls"] if item["call_id"] == key)
                task["reserved"]["runtime_ms"] = max(0, task["reserved"]["runtime_ms"] - timeout_ms)
                task["usage"]["runtime_ms"] += elapsed
                call.update(runtime_ms=elapsed, observed_at=now(), reserved_runtime_ms=0)
                if outcome is not None:
                    try:
                        self._current_profile(task)
                    except StoreError as error:
                        failure = error
                if failure:
                    call["error_code"] = failure.code
                    call["call_state"] = "unknown" if failure.code == "EXTERNAL_OUTCOME_UNKNOWN" else "completed" if outcome is not None or failure.code == "RESULT_VALIDATION_FAILED" else "blocked" if failure.code in {"CONTEXT_CHANGED", "AUTHENTICATION_REQUIRED", "CONNECTION_NOT_READY"} else "failed"
                    call["result_ref"] = self.store.write_record(task["task_id"], "call_result", {
                        "call_id": key, "result": None, "error_code": failure.code, "source_complete": False,
                        **{field: call[field] for field in ("plan_id", "plan_version", "step_id", "instance_id", "capability_id", "contract_ref", "observed_at")}})
                else:
                    try:
                        body = self._completed_body(task, plan, step, call, outcome, invocation, output_schema)
                    except (StoreError, ValueError, TypeError):
                        call["error_code"] = "RESULT_VALIDATION_FAILED"
                        body = {"result": None, "source_complete": False, "warnings": ["원격 읽기는 완료했지만 출력 제공 검사를 통과하지 못했습니다."],
                            **{field: call[field] for field in ("call_id", "plan_id", "plan_version", "step_id", "instance_id", "capability_id", "contract_ref", "observed_at")}}
                    call["result_ref"] = self.store.write_record(task["task_id"], "call_result", body)
                    call["call_state"] = "completed"
                if task.get("finished_at"):
                    self._reduce_call(task, call)
                # A late result records facts but never releases a pause, cancellation or new plan.
                self._save(task)
        except (StoreError, OSError, sqlite3.Error):
            # Keep only confirmed timing metadata until the store can record the stopped call.
            self._stopped_calls[(args["task_id"], key)] = (elapsed, now())
            try:
                self._flush_stopped_calls(args["task_id"])
                task = self._task(args["task_id"])
                call = next(item for item in task["calls"] if item["call_id"] == key)
            except (StoreError, OSError, sqlite3.Error):
                return response(None, "error", "RESULT_PERSIST_FAILED", "조회 후 결과를 보존하지 못했습니다. 저장소 복구 뒤 원 호출을 다시 확인하세요.")
        return self._call_reply(task, call)

    def _completed_body(self, task, plan, step, call, outcome, invocation, output_schema):
        rule = self._resource_rule(plan, step["instance_id"])
        ceiling = effective_resource_policy(next(item for item in task["profile_snapshot"]["resources"]
                                                 if item["instance_id"] == step["instance_id"]))
        raw = outcome["result"]
        views = {mode: disclosure(raw, rule, ceiling, mode) for mode in ("allowed_model_fields", "export_fields", "retained_fields")}
        visible = set().union(*(paths for _, _, paths in views.values()))
        masked = set().union(*(paths - usable for _, usable, paths in views.values()))
        def selector(path):
            return {"segments": [{"index": part} if type(part) is int else {"key": part} for part in path]}
        return {"result": project(raw, [selector(path) for path in visible], masking=[{"field": selector(path), "action": "redact"} for path in masked]), "source_complete": outcome.get("source_complete"),
            "query_fingerprint": invocation.fingerprint, "model_paths": [list(path) for path in views["allowed_model_fields"][1]],
            "export_paths": [list(path) for path in views["export_fields"][1]], "retained_paths": [list(path) for path in views["retained_fields"][1]],
            "model_visible_paths": [list(path) for path in views["allowed_model_fields"][2]],
            "export_visible_paths": [list(path) for path in views["export_fields"][2]],
            "retained_visible_paths": [list(path) for path in views["retained_fields"][2]],
            "output_schema": output_schema, "warnings": outcome.get("warnings", []),
            "value_types": outcome.get("value_types", {}), "units": outcome.get("units", {}),
            **{field: call[field] for field in ("call_id", "plan_id", "plan_version", "step_id", "instance_id", "capability_id", "contract_ref", "observed_at")}}

    def _call_reply(self, task, call):
        inline, warnings, evidence_refs, truncated = None, [], [], False
        if call.get("result_ref"):
            evidence = self._load_evidence(task, call["result_ref"], mode="model", require_current_plan=False)
            if evidence.get("result") is not None:
                try:
                    Draft202012Validator(evidence["output_schema"]).validate(evidence["result"])
                    inline = evidence["result"]
                except Exception:
                    warnings.append("제공 정책 적용 후 출력 스키마를 충족하지 않아 본문을 직접 반환하지 않습니다.")
                evidence_refs = [call["result_ref"]]
                truncated = evidence.get("source_complete") is False
            warnings.extend(evidence.get("warnings", []))
        if truncated:
            warnings.append("원격 조회의 일부가 누락되거나 잘렸습니다.")
        header = self._plan_header(task, (task["active_plan_ref"] or call)["plan_id"])
        code = call.get("error_code") or ("RESULT_TRUNCATED" if truncated else None)
        status = "partial" if truncated and code == "RESULT_TRUNCATED" else "error" if code else "pending" if call["call_state"] == "pending" else "ok"
        return response({"task_id": task["task_id"], "task_revision": task["task_revision"],
            **{field: call[field] for field in ("call_id", "plan_id", "plan_version", "step_id", "call_state", "external_effect", "result_ref")},
            "inline_result": inline, "usage": {"logical_calls": int(call["dispatched"]), "external_requests": None if call["dispatched"] else 0,
                "runtime_ms": call["runtime_ms"], "metrics": []}, "budget_remaining": self._budget(header, task)["remaining"],
            "evidence_refs": evidence_refs, "warnings": warnings,
            "retry_safety": {"automatic_retry_allowed": False, "effect_known": True,
                             "reason": "같은 호출 ID는 보존 상태만 반환하며 자동 재실행하지 않습니다."}, "reconciliation": None},
            status, code, "보존된 호출 상태와 오류를 확인하세요." if code else None)

    def _load_evidence(self, task, ref, mode="model", *, require_current_plan=True, _lineage=(), _checked=None):
        checked = set() if _checked is None else _checked
        require(ref not in _lineage and len(_lineage) < 32 and len(checked) + len(_lineage) <= 256,
                "RESULT_VALIDATION_FAILED", "파생 자료의 출처가 순환하거나 검사 한도를 초과했습니다.")
        if ref.startswith("memory_"):
            plan = self._current_plan(task)
            require(ref in plan["draft"]["memory_refs"], "ACCESS_DENIED", "현재 계획에 선택한 기억만 인용할 수 있습니다.")
            item = self.memory.get(task["profile_id"], ref)
            require(mode == "model" or self.memory.can_export(task["profile_id"], ref,
                    resource_rules=plan["draft"]["output"]["resource_rules"]),
                    "ACCESS_DENIED", "이 기억의 내보내기를 허용하지 않았습니다.")
            paths = [as_pointer(path) for path in leaves(item)]
            return {"record_kind": "memory", "payload": item, "expires_at": item["expires_at"],
                    "evidence_refs": [],
                    "provided_paths": paths, "allowed_paths": paths}
        record = self.store.read_record(task["task_id"], ref)
        payload, kind = record["payload"], record["record_kind"]
        require(kind not in {"memory_pending", "memory_done"}, "ACCESS_DENIED", "기억 후처리는 내부 기록입니다.")
        days = (task.get("profile_snapshot") or {}).get("retention_days")
        expiry = record["expires_at"]
        if expiry is None and days is not None:
            expiry = retention_deadline(days).isoformat()
        metadata = {"record_kind": kind, "payload": copy.deepcopy(payload), "expires_at": expiry,
                    "observed_at": task.get("created_at", task["last_activity_at"]), "evidence_refs": []}
        if kind == "call_result":
            require("intent" not in payload, "ACCESS_DENIED", "실행 의도는 원격 완료 근거가 아닙니다.")
            metadata.update({key: payload[key] for key in ("instance_id", "plan_id", "plan_version", "step_id", "capability_id", "contract_ref", "observed_at")})
            metadata.update(source_complete=payload.get("source_complete"), warnings=payload.get("warnings", []),
                            query_fingerprint=payload.get("query_fingerprint"), output_schema=payload.get("output_schema", {"type": "object"}))
            if payload.get("result") is None:
                return {**metadata, "result": None, "allowed_paths": [], "provided_paths": [], "retained_paths": []}
            profile = self._current_profile(task)
            resource = next((item for item in profile["resources"] if item["instance_id"] == payload["instance_id"]), None)
            require(resource is not None, "ACCESS_DENIED", "현재 profile의 리소스가 아닙니다.")
            original = self._plan_get({"plan_id": payload["plan_id"]}, task)["data"]["plan"]
            source_step = next((step for step in original["draft"]["steps"] if step["step_id"] == payload["step_id"]), {})
            metadata["scope"] = copy.deepcopy(source_step.get("scope"))
            require(payload["plan_version"] == original["plan_version"] and evidence_matches_step(metadata, source_step),
                    "RESULT_VALIDATION_FAILED", "원 호출의 계획·대상·기능 계약을 확인할 수 없습니다.")
            active = self._current_plan(task)
            rule = self._resource_rule(original, payload["instance_id"])
            if require_current_plan and payload["plan_id"] != active["plan_id"]:
                require(ref in active["draft"]["carried_evidence_refs"], "ACCESS_DENIED", "이전 계획의 결과를 명시적으로 승계하세요.")
                rule = self._resource_rule(active, payload["instance_id"])
            field = "allowed_model_fields" if mode == "model" else "export_fields" if mode == "export" else "retained_fields"
            result, allowed, visible = disclosure(payload["result"], rule, effective_resource_policy(resource), field)
            prefix = {"allowed_model_fields": "model", "export_fields": "export", "retained_fields": "retained"}[field]
            original_paths = {tuple(path) for path in payload.get(prefix + "_paths", [])}
            allowed &= original_paths
            visible &= {tuple(path) for path in payload.get(prefix + "_visible_paths", payload.get(prefix + "_paths", []))}
            selectors = [{"segments": [{"index": part} if type(part) is int else {"key": part} for part in path]} for path in visible]
            result = project(result, selectors)
            value_types = {key: value for key, value in payload.get("value_types", {}).items() if key in {as_pointer(path) for path in allowed}}
            units = {key: value for key, value in payload.get("units", {}).items() if key in {as_pointer(path) for path in allowed}}
            for path in allowed:
                schema = payload.get("output_schema", {})
                for segment in path:
                    schema = schema.get("items", {}) if type(segment) is int else schema.get("properties", {}).get(segment, {})
                pointer = as_pointer(path)
                if schema.get("format") == "decimal" or schema.get("x-daeri-value-type") == "decimal":
                    value_types[pointer] = "decimal"
                elif schema.get("type") == "boolean":
                    value_types[pointer] = "boolean"
                elif schema.get("type") == "string":
                    value_types[pointer] = "text"
                if schema.get("x-daeri-unit"):
                    units[pointer] = schema["x-daeri-unit"]
            metadata.update(result=result, allowed_paths=[as_pointer(path) for path in allowed],
                provided_paths=[as_pointer(tuple(path)) for path in payload.get("model_visible_paths", payload.get("model_paths", []))],
                retained_paths=[as_pointer(tuple(path)) for path in payload.get("retained_paths", [])],
                value_types=value_types, units=units)
            metadata["payload"] = {**payload, "result": result}
        elif kind in {"request", "clarification", "local_check"}:
            metadata["observed_at"] = task.get("clarification_times", {}).get(ref, metadata["observed_at"])
            metadata.update(source_complete=True, allowed_paths=[as_pointer(path) for path in leaves(payload)],
                            provided_paths=[as_pointer(path) for path in leaves(payload)],
                            retained_paths=[as_pointer(path) for path in leaves(payload)])
        elif kind == "worker_result":
            self._current_profile(task)
            refs = set(payload["evidence_refs"])
            proof = payload.get("_source_paths", {})
            require(set(proof) == refs, "ACCESS_DENIED", "작업자 결과의 출처 제공 범위를 확인할 수 없습니다.")
            for source_ref in sorted(refs):
                source = self._load_evidence(task, source_ref, mode=mode,
                    require_current_plan=require_current_plan, _lineage=(*_lineage, ref), _checked=checked)
                require(set(proof[source_ref]) <= set(source["allowed_paths"]),
                    "ACCESS_DENIED", "작업자 결과에 사용한 원문 필드의 현재 제공 범위를 확인하세요.")
                checked.add(source_ref)
            metadata["payload"].pop("_source_paths", None)
            metadata.update(evidence_refs=sorted(refs), source_complete=None)
        elif kind in {"report", "table", "artifact"}:
            # Deny archived derived data after any profile reconfiguration until reviewed.
            self._current_profile(task)
            refs = set(payload.get("evidence_refs", []))
            for finding in payload.get("findings", []):
                refs.update(finding.get("evidence_refs", []))
            for check in payload.get("checks", []):
                refs.update(check.get("evidence_refs", []))
            for validation in payload.get("table_validations", []):
                refs.update(validation.get("source_refs", []))
            for source in payload.get("sources", []):
                refs.update(source.get("record_refs", []))
            if kind == "artifact":
                for artifact in task.get("artifact_ids", {}).values():
                    if artifact["artifact_ref"] == ref:
                        refs.update(artifact.get("source_refs", []))
            lineage = (*_lineage, ref)
            for source_ref in sorted(refs):
                require(source_ref not in lineage, "RESULT_VALIDATION_FAILED", "파생 자료 출처가 순환합니다.")
                if source_ref not in checked:
                    self._load_evidence(task, source_ref, mode=mode, require_current_plan=require_current_plan,
                                        _lineage=lineage, _checked=checked)
                    checked.add(source_ref)
            metadata["evidence_refs"] = sorted(refs)
            metadata["source_complete"] = None
        return metadata

    def _finish(self, task, state):
        if task.get("finished_at"):
            return
        from .workers import invalidate
        invalidate(task)
        task["state"], task["waiting_reason"], task["finished_at"] = state, None, now()
        days = (task.get("profile_snapshot") or {}).get("retention_days")
        deadline = retention_deadline(days, datetime.fromisoformat(task["finished_at"]))
        expiry = deadline.isoformat() if deadline is not None else None
        self.store.set_task_expiry(task["task_id"], expiry)
        for call in task.get("calls", []):
            self._reduce_call(task, call)
        self._needs_cleanup = True

    def _reduce_call(self, task, call):
        if call.get("intent_ref"):
            self.store.restrict_record(task["task_id"], call["intent_ref"], {"call_id": call["call_id"], "result": None, "intent": "removed after termination"})
        if call.get("result_ref"):
            record = self.store.read_record(task["task_id"], call["result_ref"])
            body = record["payload"]
            if body.get("result") is not None:
                selectors = [{"segments": [{"index": part} if type(part) is int else {"key": part} for part in path]} for path in body.get("retained_visible_paths", body.get("retained_paths", []))]
                body["result"] = project(body["result"], selectors)
                self.store.restrict_record(task["task_id"], call["result_ref"], body)
        self._needs_cleanup = True

    def _result_submit(self, args, task):
        from .results import ResultService
        self._own(task)
        require(task["state"] != "paused", "TASK_STATE_INVALID", "일시중지한 업무는 먼저 재개하세요.")
        active = task["active_plan_ref"]
        require((args["plan_id"] is None and active is None) or
                active == {"plan_id": args["plan_id"], "plan_version": args["plan_version"]},
                "RESULT_VALIDATION_FAILED", "현재 계획을 참조해 보고를 제출하세요.")
        plan = self._current_plan(task) if active else None
        proposed = args["report"].get("proposed_task_state")
        require(set(args["report"].get("uncertain_effects", [])) == set(self._uncertain(task)),
                "RESULT_VALIDATION_FAILED", "실제 미확인 효과를 보고에 정확히 포함하세요.")
        output_approved = bool(plan) and (self._active_approved(task) if task["state"] not in TERMINAL else
            any(approval.get("ever_approved") and approval["plan_id"] == plan["plan_id"] for approval in task["approvals"]))
        if args["report"].get("tables") or args["report"].get("artifacts"):
            require(output_approved, "APPROVAL_REQUIRED", "새 표·산출물 생성에는 해당 계획의 실제 승인이 필요합니다.")
        if proposed == "succeeded":
            require(output_approved and not self._active_calls(task),
                    "RESULT_VALIDATION_FAILED", "성공 보고에는 승인 이력과 해소된 호출 상태가 필요합니다.")
        if proposed == "cancelled":
            require(task["state"] == "cancelled", "TASK_STATE_INVALID", "업무 취소 도구로 먼저 취소하세요.")
        data = ResultService(self.store).submit(task, plan, args["report"],
            load_evidence=lambda ref, mode="model": self._load_evidence(task, ref, mode))
        task["last_report_ref"] = data["report_ref"]
        if task["state"] not in TERMINAL:
            self._finish(task, data["task_state"])
        for ref in data["artifact_refs"]:
            task.setdefault("artifact_permissions", {})[ref] = {"plan_id": args["plan_id"]}
        from .automatic_memory import enqueue
        enqueue(self, task, plan, args, data["report_ref"])
        self._save(task)
        return response({"task_id": task["task_id"], "task_revision": task["task_revision"], **data})

    def _result_get(self, args, task):
        record = self.store.read_record(task["task_id"], args["record_ref"])
        require(record["record_kind"] != "plan", "INVALID_INPUT", "계획은 계획 조회 도구로 읽으세요.")
        evidence = self._load_evidence(task, args["record_ref"],
            mode="export" if record["record_kind"] == "artifact" else "model", require_current_plan=False)
        payload = evidence.get("result", evidence["payload"])
        kind, managed_file = record["record_kind"], None
        if kind == "artifact":
            require(args["record_ref"] in task.get("artifact_permissions", {}), "ACCESS_DENIED", "업무에서 제공한 산출물만 조회할 수 있습니다.")
            managed_file = self.store.artifact_file(task["task_id"], args["record_ref"])
            payload = None
        elif kind == "memory_search":
            payload = {key: record["payload"].get(key) for key in ("context", "source_plan_ref", "memory_refs", "retrieval_state", "limitations")}
        limit = args.get("limit_bytes", 65536)
        payload, cursor, more = self._payload_page(task, args, payload, limit)
        truncated = kind == "call_result" and evidence.get("source_complete") is False and evidence.get("result") is not None
        return response({"task_id": task["task_id"], "task_revision": task["task_revision"], "record_kind": kind,
            "payload": payload, "next_cursor": cursor, "has_more": more, "redactions": [],
            "evidence_refs": evidence.get("evidence_refs", []), "integrity": "intact",
            "coverage": {"source_complete": evidence.get("source_complete"), "returned_scope": "현재 허용된 보존 본문",
                         "missing_scope": ["원격 조회의 일부가 누락되거나 잘렸습니다."] if truncated else []},
            "managed_file": managed_file}, "partial" if truncated else "ok", "RESULT_TRUNCATED" if truncated else None,
            "원본 조회의 잘림을 포함한 보존 자료입니다." if truncated else None)

    def _payload_page(self, task, args, payload, limit):
        signature = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        position = 0
        if args.get("cursor"):
            cursor = self.cursors.get(args["cursor"])
            require(isinstance(cursor, dict) and cursor.get("kind") == "result" and
                    (cursor["task_id"], cursor["record_ref"], cursor["signature"]) == (task["task_id"], args["record_ref"], signature),
                    "CURSOR_INVALID", "조회 대상이나 제공 조건이 변경됐습니다.")
            position = cursor["position"]
        if payload is None or len(json.dumps(payload, ensure_ascii=False).encode()) <= limit:
            require(position == 0, "CURSOR_INVALID", "현재 본문에는 후속 페이지가 없습니다.")
            return payload, None, False
        page = copy.deepcopy(payload)
        if isinstance(payload, str):
            text = payload[position:].encode()[:max(1, limit - 2)].decode("utf-8", errors="ignore")
            while len(json.dumps(text, ensure_ascii=False).encode()) > limit:
                text = text[:-1]
            require(bool(text), "INVALID_INPUT", "반환 바이트 한도를 늘리세요.")
            page, following, more = text, position + len(text), position + len(text) < len(payload)
        else:
            arrays = []
            def find(node, path):
                if isinstance(node, list):
                    arrays.append((len(json.dumps(node).encode()), path, node))
                elif isinstance(node, dict):
                    for key, value in node.items():
                        find(value, path + [key])
            find(payload, [])
            require(bool(arrays), "RESULT_TRUNCATED", "큰 단일 JSON 항목에는 더 큰 바이트 한도가 필요합니다.")
            _, path, items = max(arrays, key=lambda item: item[0])
            parent = page
            for key in path[:-1]:
                parent = parent[key]
            chunk, following = [], position
            for item in items[position:]:
                candidate = chunk + [item]
                if path:
                    parent[path[-1]] = candidate
                else:
                    page = candidate
                if len(json.dumps(page, ensure_ascii=False).encode()) > limit:
                    if path:
                        parent[path[-1]] = chunk
                    else:
                        page = chunk
                    break
                chunk = candidate
                following += 1
            require(following > position, "RESULT_TRUNCATED", "큰 단일 JSON 항목에는 더 큰 바이트 한도가 필요합니다.")
            more = following < len(items)
        next_cursor = None
        if more:
            if len(self.cursors) >= 256:
                self.cursors.clear()
            next_cursor = identifier("cursor")
            self.cursors[next_cursor] = {"kind": "result", "task_id": task["task_id"], "record_ref": args["record_ref"],
                                        "signature": signature, "position": following}
        return page, next_cursor, more

    def _memory_update(self, args, task):
        from .knowledge import update
        return update(self, args, task)
