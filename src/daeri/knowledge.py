"""Resolve approved memory candidates to actual source fields before persistence."""

import hashlib
import json
from datetime import UTC, datetime, timedelta

from .policy import as_pointer, pointer_path, selected_paths
from .security import redact
from .storage import StoreError


TERMINAL = {"succeeded", "partial", "failed", "cancelled"}


def require(condition, code, message):
    if not condition:
        raise StoreError(code, message)


def instant(value):
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(UTC)
    except (TypeError, ValueError):
        raise StoreError("RESULT_VALIDATION_FAILED", "출처의 확인·보존 시각을 해소할 수 없습니다.") from None


def paths(record, name, body):
    raw = record.get(name)
    require(isinstance(raw, (list, tuple)), "RESULT_VALIDATION_FAILED", "실제 제공·보관 필드의 등록 기록이 필요합니다.")
    result = set()
    for value in raw:
        pointer = value if isinstance(value, str) else as_pointer(value)
        result.add(pointer_path(body, pointer))
    return result


def selector(path):
    return {"segments": [{"index": part} if type(part) is int else {"key": part} for part in path]}


def response(data):
    pending = data["index_state"] == "pending"
    return {"status": "partial" if pending else "ok", "data": data,
            "error": {"code": "MEMORY_INDEX_INCOMPLETE", "message": "기억 정본은 보존했지만 일부 검색 색인을 준비하지 못했습니다.",
                      "next_action": "refresh"} if pending else None}


def source_leaves(engine, task, references):
    seen, visiting, found = set(), set(), {}

    def visit(reference, depth=0):
        require(depth <= 32 and len(seen | visiting) <= 256, "RESULT_VALIDATION_FAILED", "기억 출처의 재귀·건수 한도를 초과했습니다.")
        require(isinstance(reference, str) and reference and reference not in visiting,
                "RESULT_VALIDATION_FAILED", "출처 참조에 순환이 있거나 유효하지 않습니다.")
        if reference in seen:
            return
        visiting.add(reference)
        record = engine._load_evidence(task, reference, mode="model")
        kind, payload = record.get("record_kind"), record.get("payload")
        if kind in {"request", "clarification", "local_check", "call_result"}:
            require(kind != "clarification" or (isinstance(payload, dict) and "question" not in payload),
                    "RESULT_VALIDATION_FAILED", "에이전트의 질문을 실제 사용자 확인으로 사용할 수 없습니다.")
            found[reference] = record
        else:
            require(kind in {"report", "table", "artifact"} and isinstance(payload, dict),
                    "RESULT_VALIDATION_FAILED", "실제 말단 관찰·사용자 확인으로 이어지는 출처가 필요합니다.")
            links = [*record.get("evidence_refs", []), *payload.get("evidence_refs", [])]
            if kind == "table":
                for source in payload.get("sources", []):
                    links.extend(source.get("record_refs", []))
            elif kind == "report":
                tables = {value["table_id"]: value for value in payload.get("table_validations", [])}
                for finding in payload.get("findings", []):
                    require(finding.get("kind") in {"observation", "derived"}, "RESULT_VALIDATION_FAILED",
                            "보고의 가설·과거 기억을 새 확인 근거로 사용할 수 없습니다.")
                    citations = list(finding.get("evidence_refs", []))
                    for table_id in finding.get("table_ids", []):
                        require(table_id in tables, "RESULT_VALIDATION_FAILED", "보고에 인용한 검증표의 출처가 없습니다.")
                        citations.extend(tables[table_id].get("source_refs", []))
                    require(bool(citations), "RESULT_VALIDATION_FAILED", "출처 없는 보고 문장은 새 확인 근거가 아닙니다.")
                    links.extend(citations)
                for check in payload.get("checks", []):
                    links.extend(check.get("evidence_refs", []))
                    for table_id in check.get("table_ids", []):
                        require(table_id in tables, "RESULT_VALIDATION_FAILED", "검사의 검증표 출처가 없습니다.")
                        links.extend(tables[table_id].get("source_refs", []))
            require(bool(links), "RESULT_VALIDATION_FAILED", "원출처로 이어지지 않는 문서·파일은 기억 근거가 아닙니다.")
            for link in dict.fromkeys(links):
                visit(link, depth + 1)
        visiting.remove(reference)
        seen.add(reference)

    for reference in references:
        visit(reference)
    require(bool(found), "RESULT_VALIDATION_FAILED", "유효한 말단 출처가 하나 이상 필요합니다.")
    return found


def update(engine, args, task):
    engine._own(task)
    require(task["state"] != "paused", "TASK_STATE_INVALID", "일시중지한 업무에서는 기억을 갱신할 수 없습니다.")
    profile = engine._current_profile(task)
    storing = args["operation"] == "store"
    draft = args.get("draft")
    with engine.store.transaction():
        plan = None
        if storing:
            require(task["state"] in TERMINAL and task.get("last_report_ref"), "TASK_STATE_INVALID",
                    "보고를 보존한 종료 업무에서만 새 지식을 저장할 수 있습니다.")
            report = engine.store.read_record(task["task_id"], task["last_report_ref"])
            require(report["record_kind"] == "report", "RESULT_VALIDATION_FAILED", "실제로 보존한 보고 기록이 필요합니다.")
            target = task.get("active_plan_ref")
            require(target is not None and any(
                approval.get("ever_approved") is True and all(approval.get(key) == target[key] for key in ("plan_id", "plan_version"))
                for approval in task.get("approvals", [])), "APPROVAL_UNAVAILABLE", "현재 계획의 실제 승인 이력이 필요합니다.")
            plan = engine._plan_get({"task_id": task["task_id"], "plan_id": target["plan_id"]}, task)["data"]["plan"]
            require(redact(draft) == draft, "RESULT_VALIDATION_FAILED", "비밀 형태의 값이 포함된 후보는 기억에 저장할 수 없습니다.")

        leaves = source_leaves(engine, task, args["source_refs"])
        resources = {resource["instance_id"]: resource for resource in profile.get("resources", [])}
        candidate_ids = set(draft["instance_ids"]) if storing else set()
        if storing:
            require(candidate_ids <= set(resources), "ACCESS_DENIED", "profile에 등록한 실제 대상만 기억에 연결할 수 있습니다.")
        rules = {rule["instance_id"]: rule for rule in plan["draft"]["output"]["resource_rules"]} if plan else {}
        resolved, retentions, bindings = [], [], {}
        expiry_row = engine.store._db.execute("SELECT expires_at FROM tasks WHERE task_id=?", (task["task_id"],)).fetchone()[0]
        finished = task.get("finished_at") or task.get("first_finished_at")
        common_expiry = instant(expiry_row) if expiry_row is not None else None
        days = (task.get("profile_snapshot") or {}).get("retention_days", profile.get("retention_days"))
        if storing and common_expiry is None and days is not None:
            common_expiry = instant(finished) + timedelta(days=days)
        for reference, record in leaves.items():
            kind = record["record_kind"]
            body = record.get("result") if kind == "call_result" else record["payload"]
            if body is None and kind == "call_result" and isinstance(record["payload"], dict):
                body = record["payload"].get("result")
            provided = paths(record, "provided_paths", body)
            allowed = paths(record, "allowed_paths", body)
            require(provided and provided <= allowed, "ACCESS_DENIED", "출처에서 실제 제공한 모든 필드의 현재 열람 허용이 필요합니다.")
            if storing:
                retained = paths(record, "retained_paths", body)
                require(provided <= retained, "ACCESS_DENIED", "제공한 원출처 전체를 현재 적법하게 보관해야 합니다.")
            if kind in {"request", "clarification"}:
                instances = candidate_ids if storing else set()
            else:
                require(record.get("instance_id") in resources, "RESULT_VALIDATION_FAILED", "관찰·검사는 실제 리소스 대상에 연결돼야 합니다.")
                instances = {record["instance_id"]}
            source_bindings, long_term = {}, True
            for instance in instances:
                resource = resources[instance]
                target_hash = hashlib.sha256(json.dumps(resource["target"], sort_keys=True,
                    separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
                source_bindings[instance] = bindings[instance] = target_hash
                if not storing:
                    continue
                require(instance in candidate_ids and instance in rules, "ACCESS_DENIED", "모든 기여 리소스에 승인한 기억 규칙이 필요합니다.")
                for policy in (rules[instance], resource.get("policy", {})):
                    memory = policy.get("memory", {})
                    require(draft["category"] in memory.get("categories", []) and memory.get("allow_model_recall") is True
                            and memory.get("retention") in {"source_bound", "long_term"}, "ACCESS_DENIED",
                            "profile·계획의 기억 범주·재제공·보관 허용을 충족해야 합니다.")
                    long_term = long_term and memory["retention"] == "long_term"
                    if kind in {"request", "clarification"}:
                        require(kind in memory.get("context_sources", []), "ACCESS_DENIED", "실제 사용자 확인 자료의 기억 사용을 별도로 허용해야 합니다.")
                    else:
                        granted = selected_paths(body, memory.get("source_fields", []))
                        granted &= selected_paths(body, policy.get("allowed_model_fields", []))
                        granted &= selected_paths(body, policy.get("retained_fields", []))
                        require(provided <= granted, "ACCESS_DENIED", "전체 기여 필드가 승인·profile 기억 출처 범위 안에 있어야 합니다.")
                retentions.append("long_term" if long_term else "source_bound")
            verified_at = record.get("observed_at") or record.get("verified_at") or finished
            # Invalidation never starts a new retention period; a current accessible correction suffices.
            if verified_at is None and not storing:
                verified_at = task.get("last_activity_at") or datetime.now(UTC).isoformat()
            verified_at = instant(verified_at).isoformat()
            finite_expiries = [instant(record["expires_at"])] if record["expires_at"] is not None else []
            if common_expiry is not None:
                finite_expiries.append(common_expiry)
            source_expiry = min(finite_expiries, default=None)
            origin_task = record.get("task_id", task["task_id"])
            if origin_task != task["task_id"] and storing:
                require(reference in plan["draft"].get("carried_evidence_refs", []), "ACCESS_DENIED", "이전 업무의 원출처는 명시적으로 승계해야 합니다.")
            export_allowed = False
            if storing:
                try:
                    exported = engine._load_evidence(task, reference, mode="export")
                    export_body = exported.get("result") if kind == "call_result" else exported["payload"]
                    export_allowed = provided <= paths(exported, "allowed_paths", export_body)
                except StoreError as error:
                    if error.code not in {"ACCESS_DENIED", "RESULT_VALIDATION_FAILED", "RECORD_EXPIRED"}:
                        raise
            resolved.append({"task_id": origin_task, "record_ref": reference,
                "fields": [selector(path) for path in sorted(provided, key=as_pointer)],
                "instance_ids": sorted(instances), "target_bindings": source_bindings,
                "verified_at": verified_at, "expires_at": source_expiry.isoformat() if source_expiry is not None else None,
                "available": True, "model_allowed": True, "retain_allowed": storing, "long_term_allowed": long_term,
                "export_allowed": export_allowed})
        if storing:
            require(set(bindings) == candidate_ids, "RESULT_VALIDATION_FAILED", "후보에 표시한 모든 리소스의 실제 기여 근거가 필요합니다.")
            retain_long_term = bool(retentions) and all(value == "long_term" for value in retentions) and profile.get("memory_policy", {}).get("long_term_allowed") is True
            policy = {"categories": [draft["category"]], "instance_ids": sorted(candidate_ids), "target_bindings": bindings,
                "model_allowed": True, "retain_allowed": True, "long_term_allowed": retain_long_term,
                "retention": "long_term" if retain_long_term else "source_bound", "expires_at": common_expiry.isoformat() if common_expiry is not None else None,
                "retention_days": (task.get("profile_snapshot") or {}).get("long_term_memory_days", profile["long_term_memory_days"])}
            item = engine.memory.save(profile["profile_id"], draft, source_records=resolved, policy=policy, memory_ref=args.get("memory_ref"))
            data = {"memory_ref": item["memory_ref"], "memory_state": "active", "expires_at": item["expires_at"], "index_state": item["index_state"]}
        else:
            _, current = engine.memory._row(profile["profile_id"], args["memory_ref"], check_policy=False, allow_expired=True)
            target_bindings = current.get("_target_bindings", {})
            if target_bindings:
                require(all(set(source["target_bindings"]) <= set(target_bindings)
                    and all(target_bindings[instance] == target for instance, target in source["target_bindings"].items())
                    for source in resolved), "ACCESS_DENIED", "다른 실제 대상의 관찰을 기억의 반증으로 사용할 수 없습니다.")
            data = engine.memory.invalidate(profile["profile_id"], args["memory_ref"], reason=args["reason"], source_records=resolved)
        update_ref = engine.store.write_record(task["task_id"], "memory_update", {"operation": args["operation"],
            **data, "source_refs": args["source_refs"], "reason_hash": hashlib.sha256(args["reason"].encode()).hexdigest()})
        engine._save(task)
        return response({"task_id": task["task_id"], "task_revision": task["task_revision"], "update_ref": update_ref, **data})


def replay(engine, args, cached):
    task = engine.store.get_task(args["task_id"])
    profile = engine._current_profile(task)
    data = dict(cached.get("data", cached))
    with engine.store.transaction():
        row = engine.store._db.execute("""SELECT v.memory_ref,v.expires_at,v.index_state,v.body,h.current_ref,h.invalidated
            FROM memory_versions v JOIN memory_heads h USING(memory_id) WHERE v.profile_id=? AND v.memory_ref=?""",
            (profile["profile_id"], data["memory_ref"])).fetchone()
        require(row is not None, "NOT_FOUND", "원래 갱신한 기억 개정을 찾을 수 없습니다.")
        active = (row["current_ref"] == row["memory_ref"] and not row["invalidated"] and row["body"] is not None
                  and (row["expires_at"] is None or instant(row["expires_at"]) > datetime.now(UTC)))
        data.update(task_revision=task["task_revision"], memory_state="active" if active else "invalidated",
                    expires_at=row["expires_at"] if active else None,
                    index_state=row["index_state"] if active else "ready")
    return response(data)
