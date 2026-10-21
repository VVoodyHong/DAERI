"""Validate source-linked reports and create bounded managed artifacts."""

import copy
import csv
import hashlib
import io
import json
import math
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal, ROUND_HALF_EVEN, localcontext

from .policy import pointer_get, pointer_path
from .planning import evidence_matches_step
from .security import redact
from .storage import StoreError


def require(condition, message):
    if not condition:
        raise StoreError("RESULT_VALIDATION_FAILED", message)


def object_fields(value, required, optional=()):
    require(isinstance(value, dict) and set(required) <= set(value) <= set(required) | set(optional),
            "결과 입력의 필수 필드·허용 필드가 일치하지 않습니다.")


def unique(values):
    require(isinstance(values, list) and all(isinstance(value, str) and value for value in values)
            and len(values) == len(set(values)), "비어 있지 않은 고유한 참조 목록이 필요합니다.")
    return values


def json_bytes(value):
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                          separators=(",", ":")).encode()
    except (TypeError, ValueError, UnicodeError):
        raise StoreError("RESULT_VALIDATION_FAILED", "유효한 JSON 결과가 필요합니다.") from None


def decimal(value):
    require(isinstance(value, str) and re.fullmatch(r"-?(?:0|[1-9]\d*)(?:\.\d+)?", value),
            "정확한 10진 문자열이 필요합니다.")
    number = Decimal(value)
    require(len(number.as_tuple().digits) <= 38 and max(0, -number.as_tuple().exponent) <= 18,
            "10진 유효숫자 38자리·소수부 18자리 한도를 초과했습니다.")
    return number


def decimal_text(value):
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    text = "0" if value == 0 else text
    decimal(text)
    return text


def key_token(values):
    require(isinstance(values, list) and all(value is not None and type(value) in (str, bool, int, float)
            and (not isinstance(value, float) or math.isfinite(value)) for value in values),
            "결합키에는 null 아닌 원본 스칼라 값만 사용할 수 있습니다.")
    return tuple((type(value).__name__, value) for value in values)


def instant(value):
    try:
        parsed = datetime.fromisoformat(value)
        require(parsed.tzinfo is not None and parsed.utcoffset() is not None, "시간대가 있는 기한이 필요합니다.")
        return parsed.astimezone(UTC)
    except (TypeError, ValueError):
        raise StoreError("RESULT_VALIDATION_FAILED", "유효한 보존기한이 필요합니다.") from None


def evidence_expiry(current, record):
    require("expires_at" in record, "출처의 보존기한 또는 명시적인 무기한 값이 필요합니다.")
    value = record["expires_at"]
    return earliest_expiry(current, instant(value) if value is not None else None)


def earliest_expiry(*values):
    return min((value for value in values if value is not None), default=None)


def expiry_text(value):
    return value.isoformat() if value is not None else None


FORMATS = [
    {"format_id": "text", "contract_ref": "format_text_v1", "description": "UTF-8 텍스트 또는 검증한 보고 파일",
     "media_type": "text/plain; charset=utf-8", "extensions": ["txt"], "input_kinds": ["text", "report"]},
    {"format_id": "json", "contract_ref": "format_json_v1", "description": "검증한 구조화 자료·표·보고의 UTF-8 JSON",
     "media_type": "application/json", "extensions": ["json"], "input_kinds": ["json", "table", "report"]},
    {"format_id": "csv", "contract_ref": "format_csv_v1", "description": "검증표에서만 생성하는 UTF-8 CSV; 수식 문자열 무해화",
     "media_type": "text/csv; charset=utf-8", "extensions": ["csv"], "input_kinds": ["table"]},
]


class ResultService:
    def __init__(self, store):
        self.store = store

    def formats(self):
        return [{**copy.deepcopy(item), "options_schema": {"type": "object", "properties": {}, "additionalProperties": False},
                 "available": True, "unavailable_reason": None} for item in FORMATS]

    def _input(self, evidence):
        if "result" in evidence:
            return evidence["result"]
        payload = evidence.get("payload")
        return payload.get("result", payload) if isinstance(payload, dict) else payload

    def _field(self, evidence, pointer):
        value = self._input(evidence)
        path = pointer_path(value, pointer)
        allowed = {tuple(item) if isinstance(item, (tuple, list)) else pointer_path(value, item)
                   for item in evidence.get("allowed_paths", [])}
        require(path in allowed, "결합·연산에 사용한 원본 필드의 현재 내보내기가 허용되지 않았습니다.")
        result = pointer_get(value, pointer)
        require(result is None or type(result) in (str, bool, int, float), "셀 근거는 스칼라 값 하나여야 합니다.")
        require(result != "[REDACTED]", "가려진 값을 결합·연산의 입력으로 사용할 수 없습니다.")
        return result, path

    def _report_exports(self, references, load):
        """Free prose has no reliable field attribution, so every provided field must export."""
        seen, visiting = set(), set()

        def visit(reference, depth=0):
            require(depth <= 32 and len(seen | visiting) <= 256 and reference not in visiting,
                    "보고 파일의 원출처가 순환하거나 처리 한도를 초과했습니다.")
            if reference in seen:
                return
            visiting.add(reference)
            model = load(reference, mode="model")
            exported = load(reference, mode="export")
            kind = model.get("record_kind")
            if kind in {"request", "clarification", "local_check", "call_result", "memory"}:
                provided = model.get("provided_paths")
                allowed = exported.get("allowed_paths")
                require(isinstance(provided, list) and isinstance(allowed, list),
                        "자유 보고의 실제 제공·내보내기 필드를 모두 확인해야 파일을 만들 수 있습니다.")
                body = self._input(model)
                def resolved(values):
                    return {tuple(path) if isinstance(path, (tuple, list)) else pointer_path(body, path) for path in values}
                require(resolved(provided) <= resolved(allowed),
                        "모델에만 제공한 필드를 자유 보고 파일로 내보낼 수 없습니다.")
                require(kind != "memory" or bool(provided), "기억 본문의 내보내기 허용을 확인할 수 없습니다.")
                if kind == "memory":
                    # Memory export validates its stored proof; sources may belong to expired earlier tasks.
                    visiting.remove(reference)
                    seen.add(reference)
                    return
            else:
                require(kind in {"report", "table", "artifact"}, "보고 파일의 출처 제공 조건을 확인할 수 없습니다.")
            payload = model.get("payload") if isinstance(model.get("payload"), dict) else {}
            links = {*model.get("evidence_refs", []), *payload.get("evidence_refs", [])}
            for source in payload.get("sources", []):
                links.update(source.get("record_refs", []))
            for finding in payload.get("findings", []):
                links.update(finding.get("evidence_refs", []))
            for check in payload.get("checks", []):
                links.update(check.get("evidence_refs", []))
            for validation in payload.get("table_validations", []):
                links.update(validation.get("source_refs", []))
            require(kind not in {"report", "table", "artifact"} or bool(links),
                    "기여 출처를 해소하지 못한 보고·파일을 다른 보고 파일에 복제할 수 없습니다.")
            for link in sorted(links):
                visit(link, depth + 1)
            visiting.remove(reference)
            seen.add(reference)

        for reference in sorted(references):
            visit(reference)
        return sorted(references)

    def _branch_excluded(self, criterion, plan, records):
        """A missing or masked value cannot prove an approved branch false."""
        steps = {step["step_id"]: step for step in plan["draft"]["steps"]}
        if not criterion["step_ids"]:
            return False
        for step_id in criterion["step_ids"]:
            step = steps.get(step_id, {})
            bindings = {item["binding_id"]: item for item in step.get("input_bindings", [])}
            excluded = False
            for condition in step.get("conditions", []) or []:
                binding = bindings.get(condition["binding_id"])
                if not binding or condition["operator"] not in {"eq", "in"}:
                    continue
                source = steps.get(binding["source_step_id"], {})
                observations = [record for ref, record in records.items() if record.get("record_kind") == "call_result"
                    and evidence_matches_step(record, source)
                    and ((record.get("plan_id") == plan["plan_id"] and record.get("plan_version") == plan["plan_version"])
                         or ref in plan["draft"].get("carried_evidence_refs", []))]
                if not observations:
                    continue
                try:
                    values = [self._field(record, binding["source_pointer"])[0] for record in observations]
                except StoreError:
                    continue
                choices = [condition["value"]] if condition["operator"] == "eq" else condition["value"]
                if all(not any(type(value) is type(choice) and value == choice for choice in choices) for value in values):
                    excluded = True
                    break
            if not excluded:
                return False
        return True

    def _table(self, task, plan, table, rule, load, expiry):
        object_fields(table, {"table_id", "sources", "columns", "rows"})
        require(table["table_id"] == rule["table_id"], "승인받은 표 ID가 필요합니다.")
        require(isinstance(table["sources"], list) and isinstance(table["columns"], list)
                and isinstance(table["rows"], list) and len(table["rows"]) <= rule["max_rows"], "표의 구조·행 한도가 승인과 다릅니다.")
        rules = {item["source_id"]: item for item in rule["sources"]}
        require(len(rules) == len(rule["sources"]) and bool(rules), "고유한 승인 소스가 필요합니다.")
        sources, refs, used_steps, limitations = {}, [], set(), []
        steps = {item["step_id"]: item for item in plan["draft"]["steps"]}
        for source in table["sources"]:
            object_fields(source, {"source_id", "record_refs"})
            sid = source["source_id"]
            require(sid in rules and sid not in sources, "표 소스가 승인 목록과 일치하지 않습니다.")
            record_refs = unique(source["record_refs"])
            require(0 < len(record_refs) <= 1000, "표 소스별 실제 기록이 1~1000개 필요합니다.")
            source_rule = rules[sid]
            step = steps.get(source_rule["step_id"])
            require(step is not None, "자료 확보 단계가 현재 계획에 없습니다.")
            source_rows, records, complete, fingerprints = {}, {}, True, set()
            for ref in record_refs:
                record = load(ref, mode="export")
                require(evidence_matches_step(record, step),
                        "표의 기록과 승인 단계·리소스·기능 계약·범위가 일치하지 않습니다.")
                require((record.get("plan_id") == plan["plan_id"] and record.get("plan_version") == plan["plan_version"])
                        or ref in plan["draft"].get("carried_evidence_refs", []), "이전 계획의 자료는 명시적으로 승계해야 합니다.")
                expiry = evidence_expiry(expiry, record)
                complete = complete and record.get("source_complete") is True
                fingerprints.add(record.get("query_fingerprint"))
                value = self._input(record)
                collection = pointer_get(value, source_rule["rows_pointer"])
                is_object = isinstance(collection, dict)
                require(is_object or isinstance(collection, list), "행 경로는 객체 또는 객체 배열이어야 합니다.")
                rows = [collection] if is_object else collection
                require(all(isinstance(row, dict) for row in rows) and len(rows) <= 10000,
                        "소스의 로컬 행 처리 한도를 초과했거나 객체 행이 아닙니다.")
                require(bool(source_rule["key_pointers"]) or (is_object and len(rules) == 1 and len(record_refs) == 1),
                        "여러 행·소스 결합에는 원본 유일키가 필요합니다.")
                for index, row in enumerate(rows):
                    prefix = source_rule["rows_pointer"] + ("" if is_object else f"/{index}")
                    keys = [self._field(record, prefix + pointer)[0] for pointer in source_rule["key_pointers"]]
                    token = key_token(keys)
                    require(token not in source_rows, "소스에 누락·중복 결합키가 있습니다.")
                    source_rows[token] = {"ref": ref, "prefix": pointer_path(value, prefix), "keys": keys}
                    require(len(source_rows) <= 10000, "소스의 전체 로컬 행 처리 한도를 초과했습니다.")
                records[ref] = record
                refs.append(ref)
            require(len(record_refs) == 1 or (None not in fingerprints and len(fingerprints) == 1),
                    "여러 기록이 동일한 승인 조회를 구성한다는 근거가 필요합니다.")
            sources[sid] = {"rows": source_rows, "records": records, "complete": complete}
            used_steps.add(source_rule["step_id"])
        require(set(sources) == set(rules), "표의 승인 소스가 누락되었습니다.")
        signatures = {tuple(part[0] for part in token) for source in sources.values() for token in source["rows"]}
        require(len(signatures) <= 1 and len({len(item["key_pointers"]) for item in rules.values()}) == 1,
                "모든 소스의 결합키 개수·순서·타입이 같아야 합니다.")
        require(rule["join_mode"] in {"none", "inner", "full"} and (rule["join_mode"] != "none" or len(sources) == 1),
                "결합 방식이 유효하지 않습니다.")
        key_sets = [set(source["rows"]) for source in sources.values()]
        expected = set.intersection(*key_sets) if rule["join_mode"] == "inner" else set.union(*key_sets)
        if rule["coverage"] == "complete":
            require(all(source["complete"] for source in sources.values()), "원본 완전성 근거가 없는 표를 전체 결과로 보고할 수 없습니다.")
        else:
            require(rule["coverage"] == "selected", "표의 완전성 범위가 유효하지 않습니다.")
            limitations.append("승인한 일부 행만 제공하는 selected 표입니다.")
        columns = {}
        for column in table["columns"]:
            object_fields(column, {"column_id", "label", "value_type"})
            require(isinstance(column["column_id"], str) and column["column_id"].strip() and column["column_id"] not in columns
                    and isinstance(column["label"], str) and column["label"].strip()
                    and column["value_type"] in {"text", "decimal", "boolean"}, "표 열은 고유 ID·이름·지원 타입이 필요합니다.")
            columns[column["column_id"]] = column
        require(bool(columns), "표의 열을 하나 이상 지정하세요.")
        received = set()
        normalized = copy.deepcopy(table)
        for row in normalized["rows"]:
            object_fields(row, {"key", "cells"})
            token = key_token(row["key"])
            require(token in expected and token not in received, "결과에 승인 결합 결과 밖의 키·중복 키가 있습니다.")
            received.add(token)
            require(isinstance(row["cells"], list) and len(row["cells"]) == len(columns), "행별 모든 열의 셀이 필요합니다.")
            found = set()
            for cell in row["cells"]:
                object_fields(cell, {"column_id", "value", "operation", "inputs"}, {"scale", "missing_source_id"})
                require(cell["column_id"] in columns and cell["column_id"] not in found, "열별 셀은 하나여야 합니다.")
                found.add(cell["column_id"])
                require(cell["operation"] in rule["allowed_operations"], "승인하지 않은 셀 연산입니다.")
                require(isinstance(cell["inputs"], list) and len(cell["inputs"]) <= 1000, "셀 입력이 유효하지 않습니다.")
                values = []
                for item in cell["inputs"]:
                    object_fields(item, {"source_id", "record_ref", "pointer"})
                    source = sources.get(item["source_id"])
                    require(source is not None and item["record_ref"] in source["records"], "셀 근거가 표 소스에 없습니다.")
                    original_row = source["rows"].get(token)
                    require(original_row is not None and original_row["ref"] == item["record_ref"], "다른 행의 값을 같은 행의 셀 근거로 사용할 수 없습니다.")
                    record = source["records"][item["record_ref"]]
                    value, path = self._field(record, item["pointer"])
                    prefix = original_row["prefix"]
                    require(path[:len(prefix)] == prefix, "셀 근거는 해당 원본 행 안의 값이어야 합니다.")
                    value_type = record.get("value_types", {}).get(item["pointer"])
                    if type(value) is int:
                        value_type = "decimal"
                        value = str(value)
                    elif type(value) is bool:
                        value_type = "boolean"
                    elif value is not None and value_type != "decimal":
                        require(isinstance(value, str), "불확실한 부동소수 표현을 정확한 숫자로 바꿀 수 없습니다.")
                        value_type = "text"
                    values.append((value, value_type, record.get("units", {}).get(item["pointer"])))
                expected_value = self._compute(cell, values, rule, sources, token, limitations)
                kind = columns[cell["column_id"]]["value_type"]
                if expected_value is not None:
                    require((kind == "text" and isinstance(expected_value, str) and type(cell["value"]) is str)
                            or (kind == "boolean" and type(expected_value) is bool and type(cell["value"]) is bool)
                            or (kind == "decimal" and isinstance(expected_value, Decimal) and isinstance(cell["value"], str)),
                            "계산한 값과 열 타입이 일치하지 않습니다.")
                if kind == "decimal" and expected_value is not None:
                    require(decimal(cell["value"]) == expected_value, "모델이 제출한 숫자가 원본으로 재계산한 값과 다릅니다.")
                    cell["value"] = decimal_text(expected_value)
                else:
                    require(type(cell["value"]) is type(expected_value) and cell["value"] == expected_value,
                            "모델이 제출한 셀이 원본·연산 결과와 다릅니다.")
        require(rule["coverage"] != "complete" or received == expected, "전체 표의 행이 누락되었습니다.")
        require(expiry is None or expiry > datetime.now(UTC), "표에 기한이 끝난 출처가 있습니다.")
        if len({source["records"][ref].get("observed_at") for source in sources.values() for ref in source["records"]}) > 1:
            limitations.append("원본의 관찰 시각이 다르며 동일 시점의 원자적 조회를 보장하지 않습니다.")
        validation = {"table_id": table["table_id"], "source_refs": list(dict.fromkeys(refs)), "row_count": len(received),
                      "coverage": rule["coverage"], "checks": ["structure", "sources", "join", "arithmetic", "field_access", "retention"],
                      "limitations": list(dict.fromkeys(limitations)), "expires_at": expiry_text(expiry)}
        return normalized, validation, used_steps

    def _compute(self, cell, inputs, rule, sources, token, limitations):
        operation = cell["operation"]
        require((operation == "ratio") == ("scale" in cell) and (operation == "missing") == ("missing_source_id" in cell),
                "비율 자릿수·부재 소스는 해당 연산에만 지정해야 합니다.")
        if operation == "missing":
            source = sources.get(cell["missing_source_id"])
            require(not inputs and source is not None and rule["join_mode"] == "full" and source["complete"]
                    and token not in source["rows"], "원본 전체에서 확인하지 않은 부재를 null로 만들 수 없습니다.")
            return None
        require(bool(inputs), "셀 연산에 원본 입력이 필요합니다.")
        if operation == "copy":
            require(len(inputs) == 1, "복사는 원본 셀 하나만 사용합니다.")
            value, kind, _ = inputs[0]
            return decimal(value) if kind == "decimal" and value is not None else value
        if operation == "equal":
            require(len(inputs) == 2 and inputs[0][1] == inputs[1][1], "동등 비교는 같은 타입의 셀 두 개가 필요합니다.")
            a, b = inputs
            return decimal(a[0]) == decimal(b[0]) if a[1] == "decimal" and a[0] is not None and b[0] is not None else type(a[0]) is type(b[0]) and a[0] == b[0]
        require(operation in {"sum", "difference", "ratio"} and all(kind == "decimal" and value is not None for value, kind, _ in inputs),
                "산술에는 정확한 숫자 셀만 사용할 수 있습니다.")
        require(operation == "sum" or len(inputs) == 2, "차이·비율은 원본 셀 두 개가 필요합니다.")
        if operation in {"sum", "difference"}:
            require(len({unit for _, _, unit in inputs}) == 1, "단위가 다른 값은 합산·차감할 수 없습니다.")
            if inputs[0][2] is None:
                limitations.append("원본 계약에 단위 정보가 없어 단위 대응은 승인한 설명 범위에서 확인해야 합니다.")
        numbers = [decimal(value) for value, _, _ in inputs]
        with localcontext() as context:
            context.prec = 80
            if operation == "sum":
                value = sum(numbers, Decimal(0))
            elif operation == "difference":
                value = numbers[0] - numbers[1]
            else:
                require(type(cell["scale"]) is int and 0 <= cell["scale"] <= 18 and cell["scale"] == rule["ratio_scale"]
                        and numbers[1] != 0, "비율의 승인 자릿수·0 나눗셈을 확인하세요.")
                value = (numbers[0] / numbers[1]).quantize(Decimal(1).scaleb(-cell["scale"]), rounding=ROUND_HALF_EVEN)
            decimal_text(value)
            return value

    def _render(self, format_id, kind, value):
        if format_id == "json":
            return json_bytes(value), False
        if format_id == "text":
            require(kind in {"text", "report"}, "텍스트 생성기가 받지 않는 입력 종류입니다.")
            if kind == "report":
                lines = [value["summary"]]
                lines.extend(f"{item['kind']}: {item['text']}\n근거: {', '.join(item['evidence_refs'])}"
                             for item in value["findings"])
                for table in value["tables"]:
                    lines.append("표 " + table["table_id"])
                    lines.append(" | ".join(column["label"] for column in table["columns"]))
                    for row in table["rows"]:
                        cells = {cell["column_id"]: cell["value"] for cell in row["cells"]}
                        lines.append(" | ".join("null" if cells[column["column_id"]] is None else str(cells[column["column_id"]])
                                                for column in table["columns"]))
                lines.extend(f"검토 {item['criterion_id']}: {item['status']} — {item['method']}" for item in value["checks"])
                value = "\n\n".join([*lines, *value["limitations"], *value["next_actions"]])
            require(isinstance(value, str), "텍스트 파일에는 텍스트 입력이 필요합니다.")
            return value.encode(), False
        require(format_id == "csv" and kind == "table", "등록하지 않은 파일 형식·입력 종류입니다.")
        stream = io.StringIO(newline="")
        writer, changed = csv.writer(stream, lineterminator="\r\n"), False
        def safe(item):
            nonlocal changed
            if isinstance(item, str) and (item[:1] in {"=", "+", "-", "@", "\t", "\r", "\n"}
                                          or any(char in item for char in "\t\r\n")):
                changed = True
                return "'" + item.replace("\t", " ").replace("\r", " ").replace("\n", " ")
            return item
        writer.writerow([safe(column["label"]) for column in value["columns"]])
        kinds = {column["column_id"]: column["value_type"] for column in value["columns"]}
        for row in value["rows"]:
            cells = {cell["column_id"]: cell["value"] for cell in row["cells"]}
            values = []
            for column in value["columns"]:
                item = cells[column["column_id"]]
                values.append("" if item is None else ("true" if item else "false") if type(item) is bool else
                              safe(item) if kinds[column["column_id"]] == "text" else item)
            writer.writerow(values)
        return stream.getvalue().encode(), changed

    def submit(self, task, plan, report, *, load_evidence):
        object_fields(report, {"summary", "proposed_task_state", "findings", "tables", "checks", "limitations", "uncertain_effects", "artifacts", "next_actions"})
        require(isinstance(report["summary"], str) and report["summary"].strip()
                and report["proposed_task_state"] in {"succeeded", "partial", "failed", "cancelled"}, "보고 요약·종료 상태가 필요합니다.")
        for key in ("findings", "tables", "checks", "limitations", "uncertain_effects", "artifacts", "next_actions"):
            require(isinstance(report[key], list) and len(report[key]) <= 1000, "보고 목록의 구조·크기를 확인하세요.")
        require(all(isinstance(value, str) for key in ("limitations", "next_actions") for value in report[key]), "보고 제한·후속 행동은 문자열 목록입니다.")
        unique(report["uncertain_effects"])
        require(task["state"] != "paused", "일시중지한 업무에는 새 결과를 제출할 수 없습니다.")
        with self.store.transaction():
            task_expiry = self.store._db.execute("SELECT expires_at FROM tasks WHERE task_id=?", (task["task_id"],)).fetchone()[0]
            days = (task.get("profile_snapshot") or {}).get("retention_days")
            expiry = instant(task_expiry) if task_expiry is not None else (
                datetime.now(UTC) + timedelta(days=days) if days is not None else None)
            if expiry is not None and expiry <= datetime.now(UTC):
                raise StoreError("RECORD_EXPIRED", "보고의 보존기한이 끝났습니다.")
            normalized = redact(copy.deepcopy(report))
            tables, table_validations, table_steps, evidence = {}, [], {}, {}
            draft = plan["draft"] if plan else {"steps": [], "completion_criteria": [], "memory_refs": [], "output": {"table_rules": [], "artifact_rules": []}}
            table_rules = {item["table_id"]: item for item in draft["output"]["table_rules"]}
            for table in normalized["tables"]:
                require(isinstance(table, dict), "표는 구조화된 객체여야 합니다.")
                tid = table.get("table_id")
                require(tid in table_rules and tid not in tables, "표마다 고유한 승인 범위가 필요합니다.")
                value, validation, steps = self._table(task, plan, table, table_rules[tid], load_evidence, expiry)
                tables[tid], table_steps[tid] = value, steps
                table_validations.append(validation)
                expiry = evidence_expiry(expiry, validation)

            def load(ref):
                if ref not in evidence:
                    evidence[ref] = load_evidence(ref, mode="model")
                return evidence[ref]

            for finding in normalized["findings"]:
                object_fields(finding, {"kind", "text", "evidence_refs"}, {"table_ids"})
                require(finding["kind"] in {"observation", "derived", "memory", "hypothesis"}
                        and isinstance(finding["text"], str) and finding["text"].strip(), "관찰·파생·기억·가설을 구분해야 합니다.")
                refs = unique(finding["evidence_refs"])
                tids = unique(finding.get("table_ids", []))
                require(set(tids) <= set(tables), "이번 보고의 검증표만 인용할 수 있습니다.")
                require(finding["kind"] == "hypothesis" or bool(refs) or (finding["kind"] == "derived" and bool(tids)), "확인된 결과에 실제 근거가 필요합니다.")
                for ref in refs:
                    record = load(ref)
                    require(finding["kind"] != "memory" or (ref in draft.get("memory_refs", []) and record.get("record_kind") == "memory"), "현재 계획에 선택한 기억만 인용할 수 있습니다.")
                    require(finding["kind"] != "observation" or record.get("record_kind") in {"request", "clarification", "local_check", "call_result"}, "관찰은 실제 요청·검사·호출 기록이 필요합니다.")
                    expiry = evidence_expiry(expiry, record)
            criteria = {item["criterion_id"]: item for item in draft["completion_criteria"]}
            steps = {item["step_id"]: item for item in draft["steps"]}
            checks = {}
            for check in normalized["checks"]:
                object_fields(check, {"criterion_id", "status", "method", "evidence_refs"}, {"table_ids", "reason"})
                require("reason" not in check or isinstance(check["reason"], str), "완료 검사 이유는 문자열이어야 합니다.")
                cid = check["criterion_id"]
                require(cid in criteria and cid not in checks and check["status"] in {"met", "unmet", "unknown", "not_applicable"}
                        and isinstance(check["method"], str) and check["method"].strip(), "승인한 완료 조건을 중복 없이 검사해야 합니다.")
                refs, tids = unique(check["evidence_refs"]), unique(check.get("table_ids", []))
                require(set(tids) <= set(tables), "검사에 인용한 표가 없습니다.")
                records = [load(ref) for ref in refs]
                used_steps = set().union(*(table_steps[tid] for tid in tids)) if tids else set()
                used_steps |= {record["step_id"] for record in records
                    if evidence_matches_step(record, steps.get(record.get("step_id"), {}))}
                if check["status"] == "met":
                    require(bool(refs or tids) and set(criteria[cid]["step_ids"]) <= used_steps,
                            "완료 조건의 실제 단계·근거가 빠졌습니다.")
                    require(not any(record.get("record_kind") == "memory" for record in records) or not criteria[cid]["step_ids"],
                            "과거 기억으로 현재 외부 작업의 완료를 증명할 수 없습니다.")
                    require(all(record.get("source_complete") is True for record in records if record.get("record_kind") == "call_result"),
                            "잘림·누락·완전성 미확인 결과로 완료 조건을 충족할 수 없습니다.")
                    require(not tids or all(next(item for item in table_validations if item["table_id"] == tid)["coverage"] == "complete" for tid in tids),
                            "선택한 일부 표로 전체 완료를 증명할 수 없습니다.")
                else:
                    require(isinstance(check.get("reason"), str) and check["reason"].strip(), "미완료·미확인·적용 제외 이유가 필요합니다.")
                    require(check["status"] != "not_applicable" or not criteria[cid]["required"]
                            or self._branch_excluded(criteria[cid], plan, dict(zip(refs, records))),
                            "필수 조건은 모든 해당 단계의 승인 분기가 실제 허용 관찰에서 false여야 적용 제외할 수 있습니다.")
                for record in records:
                    expiry = evidence_expiry(expiry, record)
                checks[cid] = check
            require(set(checks) == set(criteria), "계획의 모든 완료 조건을 한 번씩 검사해야 합니다.")
            completion = "met" if criteria and all(checks[cid]["status"] in {"met", "not_applicable"} for cid in criteria if criteria[cid]["required"]) else "unknown" if not criteria else "unmet"
            proposed = normalized["proposed_task_state"]
            require(proposed != "succeeded" or (completion == "met" and not normalized["uncertain_effects"]), "필수 완료 조건·효과 확인이 없는 성공을 보존할 수 없습니다.")
            require(proposed != "cancelled" or task["state"] == "cancelled", "취소된 업무에서만 취소 결과를 보고할 수 있습니다.")
            if plan is None:
                require(proposed in {"failed", "cancelled"} and not any(normalized[key] for key in ("tables", "checks", "artifacts", "uncertain_effects")), "계획 전에는 실제 실패·취소 근거만 보고할 수 있습니다.")
                require(proposed != "failed" or any(record.get("record_kind") == "local_check"
                        and record.get("payload", {}).get("outcome") == "failed" for record in evidence.values()), "계획 전 실패에는 실제 실패한 로컬 검사가 필요합니다.")
            normalized["tables"] = list(tables.values())
            for table, validation in zip(normalized["tables"], table_validations):
                validation["expires_at"] = expiry_text(evidence_expiry(expiry, validation))
                validation["table_ref"] = self.store.write_record(task["task_id"], "table", table,
                                                                  validation["expires_at"])
            artifact_refs, artifact_validations, artifact_ids, artifact_checks = [], [], copy.deepcopy(task.get("artifact_ids", {})), []
            format_map = {item["format_id"]: item for item in self.formats()}
            artifact_rules = {item["artifact_id"]: item for item in draft["output"]["artifact_rules"]}
            seen = set()
            for artifact in normalized["artifacts"]:
                require(isinstance(artifact, dict), "산출물은 승인 ID를 가진 객체여야 합니다.")
                kind = artifact.get("kind")
                extra = {"generated": "source_ref", "existing": "artifact_ref", "derived": "table_id", "report": None}.get(kind)
                require(kind in {"generated", "existing", "derived", "report"}, "등록된 산출물 입력 종류가 필요합니다.")
                object_fields(artifact, {"kind", "artifact_id", "label"} | ({extra} if extra else set()))
                aid = artifact["artifact_id"]
                require(aid in artifact_rules and aid not in seen and isinstance(artifact["label"], str) and artifact["label"].strip(), "산출물마다 고유한 승인 ID·표시 이름이 필요합니다.")
                seen.add(aid)
                rule = artifact_rules[aid]
                descriptor = format_map.get(rule["format_id"])
                require(descriptor and descriptor["available"] and rule["format_contract_ref"] == descriptor["contract_ref"]
                        and rule["input_kind"] in descriptor["input_kinds"] and rule["options"] == {}
                        and type(rule["max_bytes"]) is int and 0 < rule["max_bytes"] <= 10 * 1024 * 1024,
                        "산출물 형식·계약·입력·옵션·크기가 승인한 등록 계약과 다릅니다.")
                artifact_expiry = expiry
                source_refs = []
                if kind == "existing":
                    file = self.store.artifact_file(task["task_id"], artifact["artifact_ref"])
                    artifact_expiry = evidence_expiry(artifact_expiry, file)
                    require(file["format_id"] == rule["format_id"] and file["media_type"] == descriptor["media_type"]
                            and file["size_bytes"] <= rule["max_bytes"], "첨부 파일의 실제 형식·크기가 승인과 다릅니다.")
                    load_evidence(artifact["artifact_ref"], mode="export")
                    original = next((value for value in artifact_ids.values() if value["artifact_ref"] == artifact["artifact_ref"]), None)
                    require(original is not None and isinstance(original.get("source_refs"), list),
                            "기존 첨부의 기여 출처 등록을 확인할 수 없습니다.")
                    source_refs = self._report_exports(original["source_refs"], load_evidence)
                    ref, fingerprint, changed = artifact["artifact_ref"], artifact_ids.get(aid, {}).get("fingerprint"), False
                else:
                    if kind == "derived":
                        require(artifact["table_id"] in tables and rule["input_kind"] == "table", "파일을 생성할 검증표가 없습니다.")
                        value = tables[artifact["table_id"]]
                        source_refs = next(item["source_refs"] for item in table_validations if item["table_id"] == artifact["table_id"])
                    elif kind == "report":
                        require(rule["input_kind"] == "report", "보고 파일 입력을 승인받아야 합니다.")
                        value = {key: value for key, value in normalized.items() if key != "artifacts"}
                        contributing = set(evidence) | set(draft.get("memory_refs", []))
                        for validation in table_validations:
                            contributing.update(validation["source_refs"])
                        if task.get("request_ref"):
                            contributing.add(task["request_ref"])
                        contributing.update(call["result_ref"] for call in task.get("calls", []) if call.get("result_ref"))
                        source_refs = self._report_exports(contributing, load_evidence)
                        for reference in source_refs:
                            artifact_expiry = evidence_expiry(artifact_expiry, load(reference))
                    else:
                        source = load_evidence(artifact["source_ref"], mode="export")
                        if source.get("record_kind") in {"report", "table", "artifact", "memory"}:
                            self._report_exports({artifact["source_ref"]}, load_evidence)
                        value = self._input(source)
                        require(rule["input_kind"] == ("text" if isinstance(value, str) else "json"), "원자료의 입력 종류가 승인과 다릅니다.")
                        artifact_expiry = evidence_expiry(artifact_expiry, source)
                        source_refs = [artifact["source_ref"]]
                    content, changed = self._render(rule["format_id"], rule["input_kind"], value)
                    require(len(content) <= rule["max_bytes"], "생성 파일이 승인한 최대 크기를 초과했습니다.")
                    fingerprint = hashlib.sha256(json_bytes(rule) + content).hexdigest()
                    previous = artifact_ids.get(aid)
                    if previous:
                        require(previous.get("fingerprint") == fingerprint, "기존 산출물 ID로 다른 내용·형식을 덮어쓸 수 없습니다.")
                        stored_file = self.store.artifact_file(task["task_id"], previous["artifact_ref"])
                        artifact_expiry = evidence_expiry(artifact_expiry, stored_file)
                        ref = previous["artifact_ref"]
                    else:
                        ref = self.store.write_artifact(task["task_id"], content, rule["format_id"], descriptor["media_type"], descriptor["extensions"][0], expiry_text(artifact_expiry))
                expiry = earliest_expiry(expiry, artifact_expiry)
                if aid in artifact_ids:
                    require(artifact_ids[aid]["artifact_ref"] == ref, "같은 산출물 ID는 기존 파일만 재사용할 수 있습니다.")
                artifact_ids[aid] = {"artifact_ref": ref, "fingerprint": fingerprint, "source_refs": sorted(set(source_refs))}
                artifact_refs.append(ref)
                if changed:
                    artifact_checks.append(f"{aid}: CSV 수식·탭·개행 문자열을 내보낼 때 무해화했습니다.")
                if kind == "derived":
                    table_validation = next(item for item in table_validations if item["table_id"] == artifact["table_id"])
                    artifact_validations.append({"artifact_ref": ref, "artifact_id": aid, "table_id": artifact["table_id"], "table_ref": table_validation["table_ref"]})
            validation = {"structure": "passed", "evidence": "passed", "completion": completion,
                          "checks": list(checks.values()), "issues": [*normalized["limitations"], *artifact_checks]}
            report_ref = self.store.write_record(task["task_id"], "report", {**normalized,
                "validation": validation, "table_validations": table_validations, "artifact_refs": artifact_refs,
                "artifact_validations": artifact_validations}, expiry_text(expiry))
            task["artifact_ids"] = artifact_ids
            state = task["state"] if task["state"] in {"succeeded", "partial", "failed", "cancelled"} else proposed
            return {"report_ref": report_ref, "table_validations": table_validations, "artifact_refs": artifact_refs,
                    "artifact_validations": artifact_validations, "task_state": state, "validation": validation,
                    "delivery_status": "not_confirmed"}
