import copy
import csv
import io
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from daeri.results import ResultService
from daeri.storage import Store, StoreError


@pytest.fixture
def results(tmp_path):
    store = Store(tmp_path)
    profile = store.put_profile("results-profile")
    task = {"task_id": "task-results", "profile_id": profile["profile_id"],
            "task_revision": 5, "state": "running", "created_at": datetime.now(UTC).isoformat(),
            "profile_snapshot": {"retention_days": 30}, "artifact_ids": {}}
    store.insert_task(task)
    service = ResultService(store)
    yield service, task
    store.close()


def evidence(service, task, step="collect-a", rows=None, complete=True, retention_days=20):
    rows = rows or [{"id": "x", "amount": "10.00"}, {"id": "y", "amount": "20.00"}]
    result = {"rows": rows}
    expiry = None if retention_days is None else (datetime.now(UTC) + timedelta(days=retention_days)).isoformat()
    payload = {"result": result, "step_id": step, "instance_id": step,
               "capability_id": "read", "contract_ref": "contract-" + step, "scope": {}, "source_complete": complete,
               "observed_at": datetime.now(UTC).isoformat(), "plan_id": "plan-one", "plan_version": 1}
    ref = service.store.write_record(task["task_id"], "call_result", payload, expiry)
    resolved = {**payload, "payload": payload, "record_kind": "call_result", "expires_at": expiry,
                "allowed_paths": [f"/rows/{i}/{key}" for i, row in enumerate(rows) for key in row],
                "provided_paths": [f"/rows/{i}/{key}" for i, row in enumerate(rows) for key in row],
                "value_types": {f"/rows/{i}/amount": "decimal" for i in range(len(rows))},
                "units": {f"/rows/{i}/amount": "count" for i in range(len(rows))}, "evidence_refs": []}
    return ref, resolved


def setup_plan(service, task, *, coverage="complete", join="inner", artifact=False, source_days=(20, 20)):
    first, a = evidence(service, task, retention_days=source_days[0])
    second, b = evidence(service, task, "collect-b", [{"id": "x", "amount": "8"}, {"id": "y", "amount": "16"}],
                         retention_days=source_days[1])
    records = {first: a, second: b}
    rule = {"table_id": "comparison", "sources": [
        {"source_id": "a", "step_id": "collect-a", "rows_pointer": "/rows", "key_pointers": ["/id"]},
        {"source_id": "b", "step_id": "collect-b", "rows_pointer": "/rows", "key_pointers": ["/id"]}],
        "join_mode": join, "coverage": coverage, "allowed_operations": ["copy", "difference", "ratio", "missing"],
        "max_rows": 10, "ratio_scale": 2}
    plan = {"plan_id": "plan-one", "plan_version": 1, "draft": {
        "steps": [{"step_id": step, "instance_id": step, "capability_id": "read", "contract_ref": "contract-" + step, "scope": {}} for step in ("collect-a", "collect-b")],
        "completion_criteria": [{"criterion_id": "compared", "description": "모든 행 비교", "required": True,
                                  "step_ids": ["collect-a", "collect-b"]}],
        "carried_evidence_refs": [], "memory_refs": [], "output": {
            "table_rules": [rule], "artifact_rules": [], "resource_rules": []}}}
    if artifact:
        descriptor = next(item for item in service.formats() if item["format_id"] == "csv")
        plan["draft"]["output"]["artifact_rules"] = [{"artifact_id": "csv-output", "format_id": "csv",
            "format_contract_ref": descriptor["contract_ref"], "input_kind": "table", "options": {}, "max_bytes": 4096}]
    table = {"table_id": "comparison", "sources": [{"source_id": "a", "record_refs": [first]}, {"source_id": "b", "record_refs": [second]}],
             "columns": [{"column_id": "id", "label": "식별자", "value_type": "text"},
                         {"column_id": "difference", "label": "차이", "value_type": "decimal"}],
             "rows": [{"key": [key], "cells": [
                 {"column_id": "id", "value": key, "operation": "copy", "inputs": [{"source_id": "a", "record_ref": first, "pointer": f"/rows/{index}/id"}]},
                 {"column_id": "difference", "value": difference, "operation": "difference", "inputs": [
                     {"source_id": "a", "record_ref": first, "pointer": f"/rows/{index}/amount"},
                     {"source_id": "b", "record_ref": second, "pointer": f"/rows/{index}/amount"}]}]}
                 for index, (key, difference) in enumerate((("x", "2"), ("y", "4")))]}
    report = {"summary": "실제 조회 결과를 비교했다", "proposed_task_state": "succeeded",
              "findings": [{"kind": "derived", "text": "조회 결과의 차이를 계산했다", "evidence_refs": [], "table_ids": ["comparison"]}],
              "tables": [table], "checks": [{"criterion_id": "compared", "status": "met", "method": "검증표의 행·출처 대조", "evidence_refs": [], "table_ids": ["comparison"]}],
              "limitations": [], "uncertain_effects": [], "artifacts": ([{"kind": "derived", "artifact_id": "csv-output", "table_id": "comparison", "label": "비교 결과"}] if artifact else []), "next_actions": []}

    def load(ref, mode="model"):
        service.store.read_record(task["task_id"], ref)
        return records[ref]
    return plan, report, records, load


def test_multisource_table_is_recomputed_before_csv_and_atomic_report_storage(results):
    service, task = results
    plan, report, records, load = setup_plan(service, task, artifact=True)
    result = service.submit(task, plan, report, load_evidence=load)
    assert result["task_state"] == "succeeded"
    assert result["validation"]["completion"] == "met"
    assert result["delivery_status"] == "not_confirmed"
    validation = result["table_validations"][0]
    assert validation["row_count"] == 2 and validation["coverage"] == "complete"
    artifact = service.store.artifact_file(task["task_id"], result["artifact_refs"][0])
    rows = list(csv.reader(io.StringIO(Path(artifact["local_path"]).read_text())))
    assert rows == [["식별자", "차이"], ["x", "2"], ["y", "4"]]
    assert artifact["expires_at"] <= min(record["expires_at"] for record in records.values())
    stored = service.store.read_record(task["task_id"], result["report_ref"])["payload"]
    assert stored["summary"] == report["summary"]


@pytest.mark.parametrize("change", ["wrong_value", "other_row", "missing_key", "duplicate_key", "missing_row", "unapproved_operation", "hidden_key", "incomplete"])
def test_bad_table_never_publishes_any_report_table_or_artifact(results, change):
    service, task = results
    plan, report, records, load = setup_plan(service, task, artifact=True)
    if change == "wrong_value":
        report["tables"][0]["rows"][0]["cells"][1]["value"] = "3"
    elif change == "other_row":
        report["tables"][0]["rows"][0]["cells"][1]["inputs"][0]["pointer"] = "/rows/1/amount"
    elif change == "missing_key":
        report["tables"][0]["rows"][0]["key"] = [None]
    elif change == "duplicate_key":
        report["tables"][0]["rows"][1]["key"] = ["x"]
    elif change == "missing_row":
        report["tables"][0]["rows"].pop()
    elif change == "unapproved_operation":
        plan["draft"]["output"]["table_rules"][0]["allowed_operations"] = ["copy"]
    elif change == "hidden_key":
        next(iter(records.values()))["allowed_paths"].remove("/rows/0/id")
    elif change == "incomplete":
        next(iter(records.values()))["source_complete"] = False
    before = service.store.list_records(task["task_id"])
    with pytest.raises(StoreError, match="RESULT_VALIDATION_FAILED"):
        service.submit(task, plan, report, load_evidence=load)
    assert service.store.list_records(task["task_id"]) == before


def test_completion_cannot_use_an_unrelated_step_or_hypothesis(results):
    service, task = results
    plan, report, records, load = setup_plan(service, task)
    report["checks"][0].update(table_ids=[], evidence_refs=[next(iter(records))])
    with pytest.raises(StoreError, match="RESULT_VALIDATION_FAILED"):
        service.submit(task, plan, report, load_evidence=load)


@pytest.mark.parametrize("operator,value", [("eq", "0"), ("in", ["0", "1"])])
def test_required_branch_can_be_excluded_only_with_observed_false_condition(results, operator, value):
    service, task = results
    plan, report, records, load = setup_plan(service, task)
    second = plan["draft"]["steps"][1]
    second.update(input_bindings=[{"binding_id": "amount", "source_step_id": "collect-a",
        "source_pointer": "/rows/0/amount", "target_pointer": None}],
        conditions=[{"binding_id": "amount", "operator": operator, "value": value}])
    plan["draft"]["completion_criteria"][0]["step_ids"] = ["collect-b"]
    report.update(tables=[], findings=[])
    report["checks"][0].update(status="not_applicable", table_ids=[], evidence_refs=[next(iter(records))],
        reason="승인 분기 조건이 원본 관찰에서 false임을 확인")
    result = service.submit(task, plan, report, load_evidence=load)
    assert result["task_state"] == "succeeded"
    assert result["validation"]["completion"] == "met"


@pytest.mark.parametrize("change", ["true", "hidden", "missing", "other_plan", "wrong_source", "unconditional_step", "exists"])
def test_branch_exclusion_does_not_treat_hidden_missing_or_unproven_conditions_as_false(results, change):
    service, task = results
    plan, report, records, load = setup_plan(service, task)
    second = plan["draft"]["steps"][1]
    second.update(input_bindings=[{"binding_id": "amount", "source_step_id": "collect-a",
        "source_pointer": "/rows/0/amount", "target_pointer": None}],
        conditions=[{"binding_id": "amount", "operator": "eq", "value": "0"}])
    plan["draft"]["completion_criteria"][0]["step_ids"] = ["collect-b"]
    report.update(tables=[], findings=[])
    reference = next(iter(records))
    report["checks"][0].update(status="not_applicable", table_ids=[], evidence_refs=[reference], reason="분기 미적용")
    record = records[reference]
    if change == "true":
        second["conditions"][0]["value"] = "10.00"
    elif change == "hidden":
        record["allowed_paths"].remove("/rows/0/amount")
    elif change == "missing":
        del record["result"]["rows"][0]["amount"]
    elif change == "other_plan":
        record["plan_version"] = 2
    elif change == "wrong_source":
        record["instance_id"] = "unrelated"
    elif change == "unconditional_step":
        plan["draft"]["completion_criteria"][0]["step_ids"].append("collect-a")
    else:
        second["conditions"] = [{"binding_id": "amount", "operator": "exists"}]
    with pytest.raises(StoreError):
        service.submit(task, plan, report, load_evidence=load)


def test_exportable_memory_uses_its_verified_proof_without_reopening_other_task_sources(results):
    service, task = results
    memory = {"record_kind": "memory", "payload": {"statement": "verified operating procedure",
        "sources": [{"task_id": "earlier-task", "record_ref": "earlier-record", "fields": []}]},
        "provided_paths": ["/statement"], "allowed_paths": ["/statement"], "evidence_refs": ["earlier-record"]}
    def load(ref, mode="model"):
        assert ref == "memory-current", "An independent memory cannot reopen another task's source body"
        return memory
    assert service._report_exports(["memory-current"], load) == ["memory-current"]


@pytest.mark.parametrize("kind", ["report", "generated"])
@pytest.mark.parametrize("memory_days", [None, 2])
@pytest.mark.parametrize("task_days", [None, 30])
def test_memory_citations_and_files_use_earliest_configured_retention(results, kind, memory_days, task_days):
    service, task = results
    task_expiry = None if task_days is None else (datetime.now(UTC) + timedelta(days=task_days)).isoformat()
    task["profile_snapshot"]["retention_days"] = task_days
    if task_expiry is not None:
        service.store.set_task_expiry(task["task_id"], task_expiry)
    memory_expiry = None if memory_days is None else (datetime.now(UTC) + timedelta(days=memory_days)).isoformat()
    reference = "memory-current"
    memory = {"record_kind": "memory", "payload": {"statement": "verified procedure"},
              "provided_paths": ["/statement"], "allowed_paths": ["/statement"],
              "evidence_refs": [], "expires_at": memory_expiry}
    plan = {"plan_id": "plan-memory", "plan_version": 1, "draft": {
        "steps": [], "memory_refs": [reference],
        "completion_criteria": [{"criterion_id": "explained", "required": True, "step_ids": []}],
        "output": {"table_rules": [], "artifact_rules": [{"artifact_id": "knowledge-file", "format_id": "json",
            "format_contract_ref": "format_json_v1", "input_kind": "report" if kind == "report" else "json",
            "options": {}, "max_bytes": 10000}]}}}
    artifact = {"kind": kind, "artifact_id": "knowledge-file", "label": "procedure"}
    if kind == "generated":
        artifact["source_ref"] = reference
    report = {"summary": "verified procedure", "proposed_task_state": "succeeded",
        "findings": [{"kind": "memory", "text": "verified procedure", "evidence_refs": [reference]}],
        "tables": [], "checks": [{"criterion_id": "explained", "status": "met", "method": "기억 설명",
                                  "evidence_refs": [reference]}],
        "limitations": [], "uncertain_effects": [], "artifacts": [artifact], "next_actions": []}
    result = service.submit(task, plan, report, load_evidence=lambda ref, mode="model": memory)
    expected = min((value for value in (task_expiry, memory_expiry) if value is not None), default=None)
    assert service.store.read_record(task["task_id"], result["report_ref"])["expires_at"] == expected
    assert service.store.artifact_file(task["task_id"], result["artifact_refs"][0])["expires_at"] == expected
    service.store.cleanup(now=datetime.now(UTC) + timedelta(days=3650))
    if expected is None:
        assert service.store.read_record(task["task_id"], result["report_ref"])["expires_at"] is None
        assert service.store.artifact_file(task["task_id"], result["artifact_refs"][0])["expires_at"] is None
    else:
        with pytest.raises(StoreError, match="RECORD_EXPIRED"):
            service.store.read_record(task["task_id"], result["report_ref"])
        with pytest.raises(StoreError, match="RECORD_EXPIRED"):
            service.store.artifact_file(task["task_id"], result["artifact_refs"][0])


@pytest.mark.parametrize("source_days, memory_days", [((20, 20), None), ((None, None), 2)])
def test_memory_and_observations_keep_earliest_table_report_csv_retention(results, source_days, memory_days):
    service, task = results
    task["profile_snapshot"]["retention_days"] = None
    plan, report, records, load = setup_plan(service, task, artifact=True, source_days=source_days)
    reference = "memory-current"
    plan["draft"]["memory_refs"] = [reference]
    report["findings"].append({"kind": "memory", "text": "verified procedure", "evidence_refs": [reference]})
    memory_expiry = None if memory_days is None else (datetime.now(UTC) + timedelta(days=memory_days)).isoformat()
    memory = {"record_kind": "memory", "payload": {"statement": "verified procedure"}, "expires_at": memory_expiry}
    result = service.submit(task, plan, report, load_evidence=lambda ref, mode="model": memory if ref == reference else load(ref, mode))
    expected = min(value for value in [memory_expiry, *(record["expires_at"] for record in records.values())] if value is not None)
    table = result["table_validations"][0]
    assert table["expires_at"] == expected
    assert service.store.read_record(task["task_id"], table["table_ref"])["expires_at"] == expected
    assert service.store.read_record(task["task_id"], result["report_ref"])["expires_at"] == expected
    assert service.store.artifact_file(task["task_id"], result["artifact_refs"][0])["expires_at"] == expected


@pytest.mark.parametrize("invalid", ["missing", "", False, [], "2026-01-01T00:00:00"])
def test_evidence_requires_an_explicit_null_or_valid_expiration(results, invalid):
    service, task = results
    plan, report, records, load = setup_plan(service, task)
    report["tables"] = []
    reference = next(iter(records))
    if invalid == "missing":
        records[reference].pop("expires_at")
    else:
        records[reference]["expires_at"] = invalid
    report["findings"] = [{"kind": "observation", "text": "current observation", "evidence_refs": [reference]}]
    before = service.store.list_records(task["task_id"])
    with pytest.raises(StoreError, match="RESULT_VALIDATION_FAILED"):
        service.submit(task, plan, report, load_evidence=load)
    assert service.store.list_records(task["task_id"]) == before


@pytest.mark.parametrize("source_days, task_days", [
    ((None, None), None), ((None, 3), None), ((7, 3), None),
    ((None, None), 2), ((None, 3), 7),
])
def test_table_report_and_csv_use_only_the_earliest_finite_retention(results, source_days, task_days):
    service, task = results
    task["profile_snapshot"]["retention_days"] = task_days
    task_expiry = None if task_days is None else (datetime.now(UTC) + timedelta(days=task_days)).isoformat()
    if task_expiry is not None:
        service.store.set_task_expiry(task["task_id"], task_expiry)
    plan, report, records, load = setup_plan(service, task, artifact=True, source_days=source_days)
    expected = min((value for value in [task_expiry, *(record["expires_at"] for record in records.values())]
                    if value is not None), default=None)
    result = service.submit(task, plan, report, load_evidence=load)
    table = result["table_validations"][0]
    assert table["expires_at"] == expected
    assert service.store.read_record(task["task_id"], table["table_ref"])["expires_at"] == expected
    assert service.store.read_record(task["task_id"], result["report_ref"])["expires_at"] == expected
    assert service.store.artifact_file(task["task_id"], result["artifact_refs"][0])["expires_at"] == expected


@pytest.mark.parametrize("kind", ["existing", "derived"])
def test_reusing_an_existing_file_never_extends_its_finite_retention(results, kind):
    service, task = results
    plan, report, records, load = setup_plan(service, task, artifact=True, source_days=(60, 60))
    original = service.submit(task, plan, report, load_evidence=load)
    file_ref = original["artifact_refs"][0]
    original_expiry = service.store.artifact_file(task["task_id"], file_ref)["expires_at"]
    task["profile_snapshot"]["retention_days"] = None
    if kind == "existing":
        report["artifacts"] = [{"kind": "existing", "artifact_id": "csv-output", "artifact_ref": file_ref, "label": "기존 결과"}]
    def with_artifact(reference, mode="model"):
        if reference == file_ref:
            return {**service.store.read_record(task["task_id"], reference), "evidence_refs": list(records)}
        return load(reference, mode)
    repeated = service.submit(task, plan, report, load_evidence=with_artifact)
    assert repeated["artifact_refs"] == [file_ref]
    assert service.store.artifact_file(task["task_id"], file_ref)["expires_at"] == original_expiry
    assert service.store.read_record(task["task_id"], repeated["report_ref"])["expires_at"] == original_expiry


def test_generated_file_only_source_caps_the_partial_report_retention(results):
    service, task = results
    task["profile_snapshot"]["retention_days"] = None
    plan, report, records, load = setup_plan(service, task, source_days=(2, None))
    reference = next(iter(records))
    plan["draft"]["completion_criteria"] = []
    report.update(proposed_task_state="partial", findings=[], tables=[], checks=[])
    plan["draft"]["output"]["artifact_rules"] = [{"artifact_id": "json-output", "format_id": "json",
        "format_contract_ref": "format_json_v1", "input_kind": "json", "options": {}, "max_bytes": 10000}]
    report["artifacts"] = [{"kind": "generated", "artifact_id": "json-output", "source_ref": reference, "label": "result"}]
    result = service.submit(task, plan, report, load_evidence=load)
    expected = records[reference]["expires_at"]
    assert service.store.artifact_file(task["task_id"], result["artifact_refs"][0])["expires_at"] == expected
    assert service.store.read_record(task["task_id"], result["report_ref"])["expires_at"] == expected


@pytest.mark.parametrize("kind", ["report", "generated"])
def test_unlimited_observations_can_generate_and_read_json_files(results, kind):
    service, task = results
    task["profile_snapshot"]["retention_days"] = None
    plan, report, records, load = setup_plan(service, task, source_days=(None, None))
    reference = next(iter(records))
    report["tables"] = []
    report["findings"] = [{"kind": "observation", "text": "current observation", "evidence_refs": [reference]}]
    report["checks"][0].update(table_ids=[], evidence_refs=list(records))
    plan["draft"]["output"]["artifact_rules"] = [{"artifact_id": "json-output", "format_id": "json",
        "format_contract_ref": "format_json_v1", "input_kind": "report" if kind == "report" else "json",
        "options": {}, "max_bytes": 10000}]
    artifact = {"kind": kind, "artifact_id": "json-output", "label": "result"}
    if kind == "generated":
        artifact["source_ref"] = reference
    report["artifacts"] = [artifact]
    result = service.submit(task, plan, report, load_evidence=load)
    assert service.store.read_record(task["task_id"], result["report_ref"])["expires_at"] is None
    assert service.store.artifact_file(task["task_id"], result["artifact_refs"][0])["expires_at"] is None


def test_ratio_rounds_half_even_and_rejects_zero_division(results):
    service, task = results
    plan, report, records, load = setup_plan(service, task)
    cell = report["tables"][0]["rows"][0]["cells"][1]
    cell.update(operation="ratio", value="1.25", scale=2)
    assert service.submit(task, plan, report, load_evidence=load)["validation"]["completion"] == "met"
    list(records.values())[1]["result"]["rows"][0]["amount"] = "0"
    with pytest.raises(StoreError, match="RESULT_VALIDATION_FAILED"):
        service.submit(task, plan, report, load_evidence=load)


def test_oversized_or_changed_artifact_is_not_published(results):
    service, task = results
    plan, report, records, load = setup_plan(service, task, artifact=True)
    plan["draft"]["output"]["artifact_rules"][0]["max_bytes"] = 1
    before = service.store.list_records(task["task_id"])
    with pytest.raises(StoreError, match="RESULT_VALIDATION_FAILED"):
        service.submit(task, plan, report, load_evidence=load)
    assert service.store.list_records(task["task_id"]) == before


def test_csv_neutralizes_formula_text_without_changing_preserved_table(results):
    service, task = results
    plan, report, records, load = setup_plan(service, task, artifact=True)
    for record in records.values():
        record["result"]["rows"][0]["id"] = "=DANGEROUS()"
    report["tables"][0]["rows"][0]["key"] = ["=DANGEROUS()"]
    report["tables"][0]["rows"][0]["cells"][0]["value"] = "=DANGEROUS()"
    result = service.submit(task, plan, report, load_evidence=load)
    artifact = service.store.artifact_file(task["task_id"], result["artifact_refs"][0])
    assert "'=DANGEROUS()" in Path(artifact["local_path"]).read_text()
    table = service.store.read_record(task["task_id"], result["table_validations"][0]["table_ref"])["payload"]
    assert table["rows"][0]["cells"][0]["value"] == "=DANGEROUS()"


def test_source_key_types_must_match_instead_of_silently_making_an_empty_join(results):
    service, task = results
    plan, report, records, load = setup_plan(service, task)
    for index, row in enumerate(list(records.values())[1]["result"]["rows"]):
        row["id"] = index + 1
    report["tables"][0]["rows"] = []
    with pytest.raises(StoreError, match="RESULT_VALIDATION_FAILED"):
        service.submit(task, plan, report, load_evidence=load)


def test_partial_source_cannot_meet_a_direct_completion_check(results):
    service, task = results
    plan, report, records, load = setup_plan(service, task)
    report["tables"], report["findings"] = [], []
    report["checks"][0].update(table_ids=[], evidence_refs=list(records))
    next(iter(records.values()))["source_complete"] = False
    with pytest.raises(StoreError, match="RESULT_VALIDATION_FAILED"):
        service.submit(task, plan, report, load_evidence=load)


def test_same_artifact_id_reuses_identical_bytes_and_rejects_overwrite(results):
    service, task = results
    plan, report, records, load = setup_plan(service, task, artifact=True)
    first = service.submit(task, plan, report, load_evidence=load)
    assert service.submit(task, plan, report, load_evidence=load)["artifact_refs"] == first["artifact_refs"]
    report["tables"][0]["columns"][0]["label"] = "다른 파일 내용"
    with pytest.raises(StoreError, match="RESULT_VALIDATION_FAILED"):
        service.submit(task, plan, report, load_evidence=load)


def test_full_join_null_for_missing_row_requires_complete_source(results):
    service, task = results
    plan, report, records, load = setup_plan(service, task, join="full")
    second = list(records.values())[1]
    second["result"]["rows"].pop()
    second["allowed_paths"] = ["/rows/0/id", "/rows/0/amount"]
    missing = report["tables"][0]["rows"][1]["cells"][1]
    missing.update(operation="missing", inputs=[], value=None, missing_source_id="b")
    result = service.submit(task, plan, report, load_evidence=load)
    assert result["table_validations"][0]["coverage"] == "complete"
    second["source_complete"] = False
    with pytest.raises(StoreError, match="RESULT_VALIDATION_FAILED"):
        service.submit(task, plan, report, load_evidence=load)


def test_decimal_scale_units_and_type_are_preserved(results):
    service, task = results
    plan, report, records, load = setup_plan(service, task)
    first, second = list(records.values())
    first["result"]["rows"][0]["amount"], second["result"]["rows"][0]["amount"] = "1.005", "1"
    cell = report["tables"][0]["rows"][0]["cells"][1]
    cell.update(operation="ratio", value="1.00", scale=2)
    result = service.submit(task, plan, report, load_evidence=load)
    table = service.store.read_record(task["task_id"], result["table_validations"][0]["table_ref"])["payload"]
    assert table["rows"][0]["cells"][1]["value"] == "1"
    cell.update(operation="difference", value="0.005")
    cell.pop("scale")
    second["units"]["/rows/0/amount"] = "seconds"
    with pytest.raises(StoreError, match="RESULT_VALIDATION_FAILED"):
        service.submit(task, plan, report, load_evidence=load)


@pytest.mark.parametrize("bad", ["1e3", "+1", "01", "1.1234567890123456789", "9" * 39])
def test_decimal_input_does_not_accept_lossy_or_noncanonical_values(results, bad):
    service, task = results
    plan, report, records, load = setup_plan(service, task)
    next(iter(records.values()))["result"]["rows"][0]["amount"] = bad
    with pytest.raises(StoreError, match="RESULT_VALIDATION_FAILED"):
        service.submit(task, plan, report, load_evidence=load)


def test_untyped_numeric_string_is_not_assumed_to_be_a_number(results):
    service, task = results
    plan, report, records, load = setup_plan(service, task)
    next(iter(records.values()))["value_types"] = {}
    with pytest.raises(StoreError, match="RESULT_VALIDATION_FAILED"):
        service.submit(task, plan, report, load_evidence=load)


@pytest.mark.parametrize("fmt", ["text", "json"])
def test_report_file_is_generated_from_the_validated_report(results, fmt):
    service, task = results
    plan, report, records, load = setup_plan(service, task)
    descriptor = next(item for item in service.formats() if item["format_id"] == fmt)
    plan["draft"]["output"]["artifact_rules"] = [{"artifact_id": "report-file", "format_id": fmt,
        "format_contract_ref": descriptor["contract_ref"], "input_kind": "report", "options": {}, "max_bytes": 10000}]
    report["artifacts"] = [{"kind": "report", "artifact_id": "report-file", "label": "결과 파일"}]
    result = service.submit(task, plan, report, load_evidence=load)
    artifact = service.store.artifact_file(task["task_id"], result["artifact_refs"][0])
    assert report["summary"] in Path(artifact["local_path"]).read_text()


@pytest.mark.parametrize("change", ["table_scalar", "row_missing_cells", "bad_finding", "artifact_missing_kind", "float_cell"])
def test_malformed_nested_result_is_rejected_as_validation_failure(results, change):
    service, task = results
    plan, report, records, load = setup_plan(service, task)
    if change == "table_scalar":
        report["tables"] = [1]
    elif change == "row_missing_cells":
        report["tables"][0]["rows"] = [{"key": ["x"]}]
    elif change == "bad_finding":
        report["findings"] = [None]
    elif change == "artifact_missing_kind":
        report["artifacts"] = [{"artifact_id": "csv-output"}]
    elif change == "float_cell":
        report["tables"][0]["rows"][0]["cells"][1]["value"] = 2.0
    with pytest.raises(StoreError, match="RESULT_VALIDATION_FAILED"):
        service.submit(task, plan, report, load_evidence=load)


def test_plan_without_resources_can_report_analysis_of_registered_request(results):
    service, task = results
    expiry = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    ref = service.store.write_record(task["task_id"], "request", {"text": "운영 절차 설명"}, expiry)
    plan = {"plan_id": "plan-local", "plan_version": 1, "draft": {"steps": [], "memory_refs": [],
        "completion_criteria": [{"criterion_id": "explained", "required": True, "step_ids": []}],
        "output": {"table_rules": [], "artifact_rules": []}}}
    report = {"summary": "등록된 요청의 맥락을 설명했다", "proposed_task_state": "succeeded",
        "findings": [{"kind": "observation", "text": "사용자가 설명을 요청했다", "evidence_refs": [ref]}],
        "tables": [], "checks": [{"criterion_id": "explained", "status": "met", "method": "등록 요청과 대조", "evidence_refs": [ref]}],
        "limitations": [], "uncertain_effects": [], "artifacts": [], "next_actions": []}
    result = service.submit(task, plan, report, load_evidence=lambda value, mode="model": service.store.read_record(task["task_id"], value))
    assert result["task_state"] == "succeeded"


def test_failed_preplan_report_works_without_selected_profile(results):
    service, task = results
    task.update(profile_id=None, profile_snapshot=None, state="awaiting_input")
    expiry = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    ref = service.store.write_record(task["task_id"], "local_check", {"outcome": "failed", "message": "profile 선택 필요"}, expiry)
    report = {"summary": "profile을 선택해야 한다", "proposed_task_state": "failed",
        "findings": [{"kind": "observation", "text": "준비 검사에 실패했다", "evidence_refs": [ref]}],
        "tables": [], "checks": [], "limitations": [], "uncertain_effects": [], "artifacts": [], "next_actions": []}
    result = service.submit(task, None, report, load_evidence=lambda value, mode="model": service.store.read_record(task["task_id"], value))
    assert result["task_state"] == "failed"
    assert result["validation"]["completion"] == "unknown"


@pytest.mark.parametrize("fmt", ["text", "json"])
@pytest.mark.parametrize("exported", [[], ["/rows/0/id", "/rows/1/id"]])
def test_report_file_cannot_export_free_text_from_model_only_fields(results, fmt, exported):
    service, task = results
    plan, report, records, load = setup_plan(service, task)
    report["tables"] = []
    refs = list(records)
    report["summary"] = "private 10.00"
    report["findings"] = [{"kind": "observation", "text": "private 10.00", "evidence_refs": [refs[0]]}]
    report["checks"][0].update(evidence_refs=refs, table_ids=[])
    descriptor = next(item for item in service.formats() if item["format_id"] == fmt)
    plan["draft"]["output"]["artifact_rules"] = [{"artifact_id": "report-file", "format_id": fmt,
        "format_contract_ref": descriptor["contract_ref"], "input_kind": "report", "options": {}, "max_bytes": 10000}]
    report["artifacts"] = [{"kind": "report", "artifact_id": "report-file", "label": "제한된 결과"}]
    def limited(reference, mode="model"):
        value = copy.deepcopy(load(reference, mode))
        if mode == "export":
            value["allowed_paths"] = exported
            value["result"] = {"rows": [{"id": "x"}, {"id": "y"}]} if exported else {}
        return value
    before = service.store.list_records(task["task_id"])
    with pytest.raises(StoreError, match="RESULT_VALIDATION_FAILED"):
        service.submit(task, plan, report, load_evidence=limited)
    assert service.store.list_records(task["task_id"]) == before


@pytest.mark.parametrize("export_allowed", [False, True])
def test_report_file_checks_selected_knowledge_permissions_and_expiry_without_finding_citation(results, export_allowed):
    service, task = results
    plan, report, records, load = setup_plan(service, task)
    memory_ref = service.store.write_record(task["task_id"], "memory", {"statement": "model only knowledge"}, (datetime.now(UTC) + timedelta(days=10)).isoformat())
    records[memory_ref] = {"record_kind": "memory", "payload": {"statement": "model only knowledge"},
        "provided_paths": ["/statement"], "allowed_paths": ["/statement"], "evidence_refs": [],
        "expires_at": (datetime.now(UTC) + timedelta(days=10)).isoformat()}
    plan["draft"]["memory_refs"] = [memory_ref]
    plan["draft"]["output"]["artifact_rules"] = [{"artifact_id": "report-file", "format_id": "text",
        "format_contract_ref": "format_text_v1", "input_kind": "report", "options": {}, "max_bytes": 10000}]
    report["summary"] = "model only knowledge"
    report["artifacts"] = [{"kind": "report", "artifact_id": "report-file", "label": "결과"}]
    def limited(reference, mode="model"):
        value = copy.deepcopy(load(reference, mode))
        if mode == "export" and reference == memory_ref and not export_allowed:
            value["allowed_paths"] = []
            value["payload"] = {}
        return value
    if export_allowed:
        result = service.submit(task, plan, report, load_evidence=limited)
        expected = records[memory_ref]["expires_at"]
        assert service.store.artifact_file(task["task_id"], result["artifact_refs"][0])["expires_at"] == expected
        assert service.store.read_record(task["task_id"], result["report_ref"])["expires_at"] == expected
    else:
        with pytest.raises(StoreError, match="RESULT_VALIDATION_FAILED"):
            service.submit(task, plan, report, load_evidence=limited)


def test_generated_and_derived_artifacts_keep_minimal_source_references(results):
    service, task = results
    plan, report, records, load = setup_plan(service, task, artifact=True)
    result = service.submit(task, plan, report, load_evidence=load)
    assert task["artifact_ids"]["csv-output"]["source_refs"] == sorted(records)
    plan["draft"]["output"]["artifact_rules"] = [{"artifact_id": "raw-json", "format_id": "json",
        "format_contract_ref": "format_json_v1", "input_kind": "json", "options": {}, "max_bytes": 10000}]
    source_ref = next(iter(records))
    report["artifacts"] = [{"kind": "generated", "artifact_id": "raw-json", "source_ref": source_ref, "label": "조회 결과"}]
    service.submit(task, plan, report, load_evidence=load)
    assert task["artifact_ids"]["raw-json"]["source_refs"] == [source_ref]


def test_existing_attachment_inherits_original_source_references(results):
    service, task = results
    plan, report, records, load = setup_plan(service, task, artifact=True)
    first = service.submit(task, plan, report, load_evidence=load)
    file_ref = first["artifact_refs"][0]
    rule = copy.deepcopy(plan["draft"]["output"]["artifact_rules"][0])
    rule["artifact_id"] = "attached-file"
    plan["draft"]["output"]["artifact_rules"] = [rule]
    report["artifacts"] = [{"kind": "existing", "artifact_id": "attached-file", "artifact_ref": file_ref, "label": "기존 결과"}]
    def with_artifact(reference, mode="model"):
        if reference == file_ref:
            return {**service.store.read_record(task["task_id"], reference), "evidence_refs": sorted(records)}
        return load(reference, mode)
    service.submit(task, plan, report, load_evidence=with_artifact)
    assert task["artifact_ids"]["attached-file"]["source_refs"] == sorted(records)


def test_generated_json_cannot_reexport_a_saved_model_only_report(results):
    service, task = results
    plan, report, records, load = setup_plan(service, task)
    private_ref = next(iter(records))
    private_report = service.store.write_record(task["task_id"], "report", {
        "summary": "private 10.00", "findings": [{"kind": "observation", "text": "private 10.00", "evidence_refs": [private_ref]}]},
        (datetime.now(UTC) + timedelta(days=20)).isoformat())
    records[private_report] = {**service.store.read_record(task["task_id"], private_report), "evidence_refs": [private_ref]}
    plan["draft"]["output"]["artifact_rules"] = [{"artifact_id": "copied-report", "format_id": "json",
        "format_contract_ref": "format_json_v1", "input_kind": "json", "options": {}, "max_bytes": 10000}]
    report["artifacts"] = [{"kind": "generated", "artifact_id": "copied-report", "source_ref": private_report, "label": "과거 보고"}]
    def limited(reference, mode="model"):
        value = copy.deepcopy(load(reference, mode))
        if reference == private_ref and mode == "export":
            value.update(result={}, allowed_paths=[])
        return value
    # Remove the current table to isolate the archived report's export checks.
    report["tables"], report["findings"] = [], []
    report["checks"][0].update(table_ids=[], evidence_refs=list(records)[:2])
    with pytest.raises(StoreError, match="RESULT_VALIDATION_FAILED"):
        service.submit(task, plan, report, load_evidence=limited)


def test_existing_file_cannot_outlive_its_current_export_permission(results):
    service, task = results
    plan, report, records, load = setup_plan(service, task, artifact=True)
    original = service.submit(task, plan, report, load_evidence=load)
    file_ref = original["artifact_refs"][0]
    report["tables"], report["findings"] = [], []
    report["checks"][0].update(table_ids=[], evidence_refs=list(records))
    report["artifacts"] = [{"kind": "existing", "artifact_id": "csv-output", "artifact_ref": file_ref, "label": "기존 결과"}]
    def limited(reference, mode="model"):
        if reference == file_ref:
            return {**service.store.read_record(task["task_id"], reference), "evidence_refs": list(records)}
        value = copy.deepcopy(load(reference, mode))
        if mode == "export":
            value.update(result={}, allowed_paths=[])
        return value
    with pytest.raises(StoreError, match="RESULT_VALIDATION_FAILED"):
        service.submit(task, plan, report, load_evidence=limited)
