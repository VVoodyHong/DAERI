"""Preserve host-authored reports and files with checked sources and permissions."""

import copy
import re
from datetime import UTC, datetime

from daeri.core.policy import pointer_path
from daeri.core.security import redact
from daeri.core.errors import StoreError
from daeri.core.policy import retention_deadline


def require(condition, message):
    if not condition:
        raise StoreError('RESULT_VALIDATION_FAILED', message)


def object_fields(value, required, optional=()):
    require(isinstance(value, dict) and set(required) <= set(value) <= set(required) | set(optional),
            '결과 입력의 필수 필드·허용 필드가 일치하지 않습니다.')


def unique(values):
    require(isinstance(values, list) and all(isinstance(value, str) and value for value in values)
            and len(values) == len(set(values)), '비어 있지 않은 고유한 참조 목록이 필요합니다.')
    return values


def instant(value):
    try:
        parsed = datetime.fromisoformat(value)
        require(parsed.tzinfo is not None and parsed.utcoffset() is not None, '시간대가 있는 기한이 필요합니다.')
        return parsed.astimezone(UTC)
    except (TypeError, ValueError):
        raise StoreError('RESULT_VALIDATION_FAILED', '유효한 보존기한이 필요합니다.') from None


def evidence_expiry(current, record):
    require('expires_at' in record, '출처의 보존기한 또는 명시적인 무기한 값이 필요합니다.')
    value = record['expires_at']
    return earliest_expiry(current, instant(value) if value is not None else None)


def earliest_expiry(*values):
    return min((value for value in values if value is not None), default=None)


def expiry_text(value):
    return value.isoformat() if value is not None else None


class ResultService:
    def __init__(self, store):
        self.store = store

    def _input(self, evidence):
        if "result" in evidence:
            return evidence["result"]
        payload = evidence.get("payload")
        return payload.get("result", payload) if isinstance(payload, dict) else payload

    def _report_exports(self, references, load):
        """Free prose has no reliable field attribution, so every provided field must export."""
        seen, visiting, sources = {}, set(), set()
        expiry = None

        def visit(reference, depth=0):
            nonlocal expiry
            require(depth < 32 and reference not in visiting,
                    "보고 파일의 원출처가 순환하거나 처리 한도를 초과했습니다.")
            # A shared subtree must be checked again when reached on a deeper path.
            if seen.get(reference, -1) >= depth:
                return
            visiting.add(reference)
            model = load(reference, mode="model")
            exported = load(reference, mode="export")
            expiry = evidence_expiry(evidence_expiry(expiry, model), exported)
            kind = model.get("record_kind")
            if kind in {"request", "clarification", "local_check", "call_result", "memory"}:
                sources.add(reference)
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
                    seen[reference] = depth
                    return
            else:
                require(kind in {"report", "table", "artifact", "subagent_result"}, "보고 파일의 출처 제공 조건을 확인할 수 없습니다.")
            payload = model.get("payload") if isinstance(model.get("payload"), dict) else {}
            links = {*model.get("evidence_refs", []), *payload.get("evidence_refs", []), *payload.get("source_refs", [])}
            for source in payload.get("sources", []):
                links.update(source.get("record_refs", []))
            for finding in payload.get("findings", []):
                links.update(finding.get("evidence_refs", []))
            for check in payload.get("checks", []):
                links.update(check.get("evidence_refs", []))
            for validation in payload.get("table_validations", []):
                links.update(validation.get("source_refs", []))
            require(kind not in {"report", "table", "artifact", "subagent_result"} or bool(links),
                    "기여 출처를 해소하지 못한 보고·파일을 다른 보고 파일에 복제할 수 없습니다.")
            for link in sorted(links):
                visit(link, depth + 1)
            visiting.remove(reference)
            seen[reference] = depth

        for reference in sorted(references):
            visit(reference)
        return sorted(sources), expiry

    def submit(self, task, plan, report, *, state='succeeded', evidence_refs=(), artifacts=(), load_evidence):
        require(state in {'succeeded', 'partial', 'failed', 'cancelled'}, '업무 종료 상태를 확인하세요.')
        require(task['state'] != 'paused', '일시중지한 업무에는 새 결과를 제출할 수 없습니다.')
        with self.store.transaction():
            task_expiry = self.store._db.execute('SELECT expires_at FROM tasks WHERE task_id=?', (task['task_id'],)).fetchone()[0]
            days = (task.get('profile_snapshot') or {}).get('retention_days')
            expiry = instant(task_expiry) if task_expiry is not None else retention_deadline(days)
            task_deadline = expiry
            if expiry is not None and expiry <= datetime.now(UTC):
                raise StoreError('RECORD_EXPIRED', '보고의 보존기한이 끝났습니다.')
            evidence = {}

            def load(ref):
                if ref not in evidence:
                    evidence[ref] = load_evidence(ref, mode='model')
                return evidence[ref]

            for reference in unique(list(evidence_refs)):
                expiry = evidence_expiry(expiry, load(reference))
            artifact_refs, artifact_ids, seen = [], copy.deepcopy(task.get('artifact_ids', {})), set()
            for artifact in artifacts:
                require(isinstance(artifact, dict), '파일 등록 정보를 확인하세요.')
                existing = artifact.get('kind') == 'existing'
                required = {'artifact_id', 'label', 'artifact_ref', 'kind'} if existing else {
                    'artifact_id', 'label', 'local_path', 'media_type'}
                object_fields(artifact, required, () if existing else {'kind', 'source_refs'})
                require(existing or artifact.get('kind', 'file') == 'file', '호스트가 생성한 파일 또는 기존 파일을 등록하세요.')
                aid = artifact['artifact_id']
                require(isinstance(aid, str) and aid.strip() and aid not in seen
                        and isinstance(artifact['label'], str) and artifact['label'].strip(), '파일 ID·표시 이름은 고유한 값이어야 합니다.')
                seen.add(aid)
                prior = artifact_ids.get(aid)
                if existing:
                    ref = artifact['artifact_ref']
                    original = next((item for item in artifact_ids.values() if item['artifact_ref'] == ref), None)
                    require(original is not None, '해당 업무에 등록된 파일을 선택하세요.')
                    load_evidence(ref, mode='export')
                    source_refs = original['source_refs']
                    file = self.store.artifact_file(task['task_id'], ref)
                    expiry = evidence_expiry(expiry, file)
                else:
                    require(isinstance(artifact['local_path'], str) and isinstance(artifact['media_type'], str)
                            and len(artifact['media_type']) <= 255 and re.fullmatch(r'[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+(?:;[ -~]*)?', artifact['media_type']),
                            '생성 파일의 경로·미디어 타입을 확인하세요.')
                    source_refs = unique(artifact.get('source_refs') or list(evidence_refs))
                    require(bool(source_refs), '파일의 실제 기여 출처를 연결하세요.')
                source_refs, source_expiry = self._report_exports(source_refs, load_evidence)
                expiry = earliest_expiry(expiry, source_expiry)
                # Keep file contributors in the report's proof graph for later reads and memory.
                for reference in source_refs:
                    load(reference)
                if not existing:
                    ref = self.store.import_artifact(task['task_id'], artifact['local_path'], artifact['media_type'],
                                                     expiry_text(earliest_expiry(task_deadline, source_expiry)),
                                                     existing_ref=prior['artifact_ref'] if prior else None)
                require(prior is None or prior['artifact_ref'] == ref, '같은 파일 ID는 기존 내용을 유지해야 합니다.')
                artifact_ids[aid] = {'artifact_ref': ref, 'source_refs': source_refs}
                artifact_refs.append(ref)
            validation = {'structure': 'passed', 'evidence': 'passed',
                          'completion': 'met' if state == 'succeeded' else 'unmet',
                          'completion_assessed_by': 'host', 'issues': []}
            report_ref = self.store.write_record(task['task_id'], 'report', {
                'content': redact(copy.deepcopy(report)), 'task_state': state, 'validation': validation,
                'evidence_refs': sorted(evidence), 'artifact_refs': artifact_refs}, expiry_text(expiry))
            task['artifact_ids'] = artifact_ids
            state = task['state'] if task['state'] in {'succeeded', 'partial', 'failed', 'cancelled'} else state
            return {'report_ref': report_ref, 'artifact_refs': artifact_refs, 'task_state': state,
                    'validation': validation, 'delivery_status': 'not_confirmed'}
