"""Profile-bound selective memory; indexes never grant access to canonical data."""

import hashlib
import json
import math
import re
import sqlite3
import struct
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from .security import redact
from .policy import selector_covers, within_selectors
from .storage import StoreError
from .embedding import OpenAICompatibleEmbedder, validate_embedding_config


CATEGORIES = {"service_rule", "resource_structure", "procedure", "lesson"}
MODEL_NAME = "intfloat/multilingual-e5-small"
VECTOR_LIMITATION = "선택한 임베딩 제공자 또는 벡터 색인을 사용할 수 없어 키워드 검색만 수행했습니다."


def _require(condition, code, message):
    if not condition:
        raise StoreError(code, message)


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _instant(value):
    try:
        value = datetime.fromisoformat(value)
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError
        return value.astimezone(UTC)
    except (TypeError, ValueError):
        raise StoreError("RESULT_VALIDATION_FAILED", "기억 근거에 유효한 확인·만료 시각이 필요합니다.") from None


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _finite_min(*values):
    return min((value for value in values if value is not None), default=None)


def _expiry(value):
    return _instant(value) if value is not None else None


def _source_key(source):
    return _digest([source["task_id"], source["record_ref"]])


def purge_profile_memory(store, profile_id):
    """Remove all profile memory revisions without loading shared model runtimes."""
    tables = {"memory_heads", "memory_versions", "memory_vectors", "memory_fts"}
    with store.transaction():
        present = {row[0] for row in store._db.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name IN (?,?,?,?)", sorted(tables))}
        if not present:
            return 0
        _require(present == tables, "STORE_UNAVAILABLE", "기억 저장 구조가 완전하지 않아 profile을 삭제할 수 없습니다.")
        references = "SELECT memory_ref FROM memory_versions WHERE profile_id=?"
        for table in ("memory_fts", "memory_vectors"):
            store._db.execute(f"DELETE FROM {table} WHERE memory_ref IN ({references})", (profile_id,))
        deleted = store._db.execute("DELETE FROM memory_versions WHERE profile_id=?", (profile_id,)).rowcount
        store._db.execute("DELETE FROM memory_heads WHERE profile_id=?", (profile_id,))
        return deleted


class LocalEmbedder:
    """ONNX CPU embeddings. Only prepare() may download the selected public model."""

    def __init__(self, model, model_id):
        self.model, self.model_id, self.dimensions = model, model_id, 384

    @classmethod
    def _open(cls, cache_dir, *, download):
        try:
            from fastembed import TextEmbedding
        except ImportError:
            raise StoreError("MEMORY_SEARCH_INCOMPLETE", "로컬 임베딩용 선택 패키지를 명시적으로 준비한 뒤 다시 시도하세요.") from None
        from fastembed.common.model_description import ModelSource, PoolingType

        if not any(model["model"] == MODEL_NAME for model in TextEmbedding.list_supported_models()):
            TextEmbedding.add_custom_model(
                model=MODEL_NAME, pooling=PoolingType.MEAN, normalization=True,
                sources=ModelSource(hf=MODEL_NAME), dim=384, model_file="onnx/model.onnx",
                license="MIT", size_in_gb=0.48)
        model = TextEmbedding(model_name=MODEL_NAME, cache_dir=str(cache_dir),
                              local_files_only=not download, providers=["CPUExecutionProvider"], threads=2)
        # Include the actual cached model and tokenizer bytes so incompatible revisions never mix.
        directory = Path(model.model._model_dir)
        fingerprint = hashlib.sha256()
        for relative in ("onnx/model.onnx", "tokenizer.json", "tokenizer_config.json", "config.json"):
            path = directory / relative
            fingerprint.update(relative.encode())
            if path.is_file():
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        fingerprint.update(block)
        return cls(model, f"{MODEL_NAME}:{fingerprint.hexdigest()}")

    @classmethod
    def prepare(cls, cache_dir):
        cache_dir = Path(cache_dir)
        _require(not cache_dir.is_symlink(), "INVALID_INPUT", "모델 저장 위치에 심볼릭 링크를 사용할 수 없습니다.")
        cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        model = cls._open(cache_dir, download=True)
        (cache_dir / "prepared.json").write_text(_json({"model": MODEL_NAME, "model_id": model.model_id}))
        return model

    @classmethod
    def load(cls, cache_dir):
        cache_dir = Path(cache_dir)
        _require((cache_dir / "prepared.json").is_file(), "MEMORY_SEARCH_INCOMPLETE", "로컬 임베딩 모델 준비가 필요합니다.")
        return cls._open(cache_dir, download=False)

    def embed_query(self, text):
        return self._embed("query: " + text)

    def embed_document(self, text):
        return self._embed("passage: " + text)

    def _embed(self, text):
        _require(not self.model.model.tokenizer.encode(text).overflowing,
                 "MEMORY_INDEX_INCOMPLETE", "로컬 임베딩의 입력 토큰 한도를 초과했습니다. 지식 후보를 줄여 다시 제출하세요.")
        return next(iter(self.model.embed([text]))).tolist()


class MemoryService:
    def __init__(self, store, embedder=None, *, auto_load=True, credentials=None):
        self.store, self.embedder, self.vector_ready = store, embedder, False
        self._auto_load, self._credentials, self._providers = auto_load, credentials, {}
        with self.store.transaction():
            statements = [
                """CREATE TABLE IF NOT EXISTS memory_heads (
                    memory_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL REFERENCES profiles(profile_id),
                    current_ref TEXT NOT NULL, invalidated INTEGER NOT NULL DEFAULT 0)""",
                """CREATE TABLE IF NOT EXISTS memory_versions (
                    memory_ref TEXT PRIMARY KEY, memory_id TEXT NOT NULL REFERENCES memory_heads(memory_id),
                    profile_id TEXT NOT NULL REFERENCES profiles(profile_id), version INTEGER NOT NULL,
                    body TEXT, content_hash TEXT NOT NULL, source_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL, expires_at TEXT, index_state TEXT NOT NULL,
                    UNIQUE(memory_id,version))""",
                """CREATE TABLE IF NOT EXISTS memory_vectors (
                    memory_ref TEXT PRIMARY KEY REFERENCES memory_versions(memory_ref),
                    model_id TEXT NOT NULL, dimensions INTEGER NOT NULL, embedding BLOB NOT NULL)""",
                "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(memory_ref UNINDEXED, body, tokenize='unicode61')",
                "CREATE INDEX IF NOT EXISTS memory_profile ON memory_heads(profile_id)",
            ]
            for statement in statements:
                self.store._db.execute(statement)
            self._upgrade_expiry(statements[1])
            columns = {row["name"] for row in self.store._db.execute("PRAGMA table_info(memory_versions)")}
            if "source_lineage" not in columns:
                self.store._db.execute("ALTER TABLE memory_versions ADD COLUMN source_lineage TEXT")
            for row in self.store._db.execute("SELECT memory_ref,body FROM memory_versions WHERE source_lineage IS NULL").fetchall():
                sources = json.loads(row["body"]).get("_sources") if row["body"] else None
                # Missing old bodies cannot prove that an old observation was never blocked.
                lineage = {"keys": sorted({_source_key(source) for source in sources}) if sources else None,
                           "blocked_at": None if sources else datetime.now(UTC).isoformat()}
                self.store._db.execute("UPDATE memory_versions SET source_lineage=? WHERE memory_ref=?",
                                       (_json(lineage), row["memory_ref"]))
        if self.embedder is not None:
            self._enable_vectors()
        self.cleanup()

    def _selected_embedder(self, profile):
        config = validate_embedding_config(profile.get("embedding"))
        if config["provider"] == "keyword_only":
            return None
        if config["provider"] == "openai_compatible":
            if not config["allow_remote"]:
                return None
            key = _json(config)
            if key not in self._providers:
                self._providers[key] = OpenAICompatibleEmbedder(config, credentials=self._credentials)
            embedder = self._providers[key]
        else:
            embedder = self.embedder
            if embedder is None and self._auto_load and (self.store.data_dir / "models" / "prepared.json").is_file():
                try:
                    embedder = self.embedder = LocalEmbedder.load(self.store.data_dir / "models")
                except (StoreError, ImportError, OSError, ValueError, RuntimeError):
                    return None
        if embedder is not None:
            self._enable_vectors()
        return embedder

    def _enable_vectors(self):
        if self.vector_ready:
            return
        try:
            import sqlite_vec
            with self.store.transaction():
                try:
                    self.store._db.enable_load_extension(True)
                    sqlite_vec.load(self.store._db)
                    self.store._db.execute("SELECT vec_version()").fetchone()
                    self.vector_ready = True
                finally:
                    self.store._db.enable_load_extension(False)
        except (ImportError, AttributeError, sqlite3.Error, StoreError):
            self.vector_ready = False

    def status(self, profile_id=None):
        profile = self.store.get_profile(profile_id) if profile_id else None
        config = validate_embedding_config(profile.get("embedding") if profile else None)
        embedder = self._selected_embedder(profile or {"embedding": config})
        return {"profile_id": profile["profile_id"] if profile else None,
                "provider": config["provider"], "allow_remote": config.get("allow_remote", False),
                "model_prepared": config["provider"] == "local" and embedder is not None,
                "vector_ready": embedder is not None and self.vector_ready,
                "model_id": embedder.model_id if embedder is not None else None,
                "api_verified": False}

    def diagnose(self, profile_id):
        profile = self.store.get_profile(profile_id)
        config = validate_embedding_config(profile.get("embedding"))
        _require(config["provider"] != "keyword_only", "MEMORY_SEARCH_INCOMPLETE", "키워드 검색만 선택한 profile입니다.")
        embedder = self._selected_embedder(profile)
        _require(embedder is not None, "MEMORY_SEARCH_INCOMPLETE", "선택한 임베딩의 전송 허용과 준비 상태를 확인하세요.")
        self._check_embedding_context(profile)
        self._vector(embedder.embed_query("DAERI embedding readiness"), embedder)
        _require(self.store.get_profile(profile_id)["embedding"] == profile["embedding"],
                 "CONTEXT_CHANGED", "임베딩 진단 중 설정이 변경되었습니다.")
        return {"provider": config["provider"], "profile_id": profile["profile_id"],
                "api_verified": config["provider"] == "openai_compatible",
                "model_id": embedder.model_id, "dimensions": embedder.dimensions,
                "vector_ready": self.vector_ready}

    def _check_embedding_context(self, profile, memory_refs=None, instance_ids=None):
        """Recheck current permission immediately before an embedding operation."""
        with self.store.transaction():
            current, policy = self._policy(profile["profile_id"])
            _require(current["embedding"] == profile["embedding"], "CONTEXT_CHANGED", "임베딩 요청 전에 설정이 변경되었습니다.")
            if memory_refs is not None:
                permitted = False
                for reference in memory_refs:
                    try:
                        _, item = self._row(profile["profile_id"], reference)
                    except StoreError:
                        continue
                    permitted |= self._allowed(item, policy, instance_ids, profile=current)
                _require(permitted, "ACCESS_DENIED", "현재 허용된 기억에 대해서만 임베딩을 요청할 수 있습니다.")

    def _upgrade_expiry(self, schema):
        columns = self.store._db.execute("PRAGMA table_info(memory_versions)").fetchall()
        if not next(column for column in columns if column["name"] == "expires_at")["notnull"]:
            return
        names = "memory_ref,memory_id,profile_id,version,body,content_hash,source_hash,created_at,expires_at,index_state"
        _require({column["name"] for column in columns} == set(names.split(",")),
                 "STORE_UNAVAILABLE", "알려진 기억 스키마만 보존기한 형식을 이관할 수 있습니다.")
        db = self.store._db
        vector_schema = db.execute("SELECT sql FROM sqlite_schema WHERE name='memory_vectors'").fetchone()[0]
        dependencies = db.execute("""SELECT sql FROM sqlite_schema
            WHERE tbl_name IN ('memory_versions','memory_vectors') AND type IN ('index','trigger') AND sql IS NOT NULL""").fetchall()
        # Rebuild the child in the same transaction so foreign-key enforcement stays enabled.
        db.execute("CREATE TEMP TABLE memory_vectors_backup AS SELECT * FROM memory_vectors")
        db.execute(schema.replace("IF NOT EXISTS memory_versions", "memory_versions_next"))
        db.execute(f"INSERT INTO memory_versions_next({names}) SELECT {names} FROM memory_versions")
        db.execute("DROP TABLE memory_vectors")
        db.execute("DROP TABLE memory_versions")
        db.execute("ALTER TABLE memory_versions_next RENAME TO memory_versions")
        db.execute(vector_schema)
        db.execute("INSERT INTO memory_vectors SELECT * FROM memory_vectors_backup")
        db.execute("DROP TABLE memory_vectors_backup")
        for dependency in dependencies:
            db.execute(dependency[0])
        _require(not db.execute("PRAGMA foreign_key_check").fetchall(),
                 "STORE_UNAVAILABLE", "기억 스키마 이관 뒤 출처 연결을 확인할 수 없습니다.")

    def _policy(self, profile_id):
        profile = self.store.get_profile(profile_id)
        return profile, profile.get("memory_policy", {})

    def _sources(self, profile_id, records, *, require_retention=True):
        _require(isinstance(records, list) and 0 < len(records) <= 100,
                 "RESULT_VALIDATION_FAILED", "현재 업무의 실제 출처 기록이 필요합니다.")
        checked = []
        for value in records:
            _require(isinstance(value, dict) and all(value.get(key) is True for key in (
                ("available", "model_allowed", "retain_allowed") if require_retention else ("available",))),
                "ACCESS_DENIED", "출처의 현재 제공·보관 허용을 확인할 수 없습니다.")
            _require(isinstance(value.get("fields"), list) and bool(value["fields"]),
                     "RESULT_VALIDATION_FAILED", "실제 제공한 출처 필드를 확인해야 합니다.")
            task = self.store.get_task(value.get("task_id"))
            _require(task["profile_id"] == profile_id, "ACCESS_DENIED", "다른 profile의 자료를 기억에 사용할 수 없습니다.")
            record = self.store.read_record(task["task_id"], value.get("record_ref"))
            _require(record["record_kind"] in {"request", "clarification", "local_check", "call_result"},
                     "RESULT_VALIDATION_FAILED", "말단 관찰·사용자 확인 기록으로 출처를 해소해야 합니다.")
            _require("expires_at" in value, "RESULT_VALIDATION_FAILED", "출처의 보존기한 또는 명시적인 무기한 값이 필요합니다.")
            expiry = _finite_min(_expiry(value["expires_at"]), _expiry(record["expires_at"]))
            verified = _instant(value.get("verified_at"))
            _require(verified <= datetime.now(UTC) and (expiry is None or expiry > datetime.now(UTC)),
                     "RECORD_EXPIRED", "사용 가능한 확인 시각·출처 보존기한이 필요합니다.")
            keys = {"task_id", "record_ref", "fields", "instance_ids", "target_bindings",
                    "available", "model_allowed", "retain_allowed", "long_term_allowed"}
            checked.append({**{key: value[key] for key in keys if key in value},
                            "record_kind": record["record_kind"], "export_allowed": value.get("export_allowed") is True,
                            "expires_at": expiry.isoformat() if expiry is not None else None, "verified_at": verified.isoformat()})
        return checked

    def _resource_allowed(self, item, profile, *, export=False):
        resources = {value["instance_id"]: value for value in profile.get("resources", [])}
        for source in item["_sources"]:
            if not source.get("instance_ids") or (export and source.get("export_allowed") is not True):
                return False
            for instance in source["instance_ids"]:
                resource = resources.get(instance)
                if resource is None or _digest(resource["target"]) != source["target_bindings"].get(instance):
                    return False
                ceiling = resource.get("policy", {})
                memory = ceiling.get("memory", {})
                if (memory.get("allow_model_recall") is not True or item["category"] not in memory.get("categories", [])
                        or memory.get("retention") not in {"source_bound", "long_term"}
                        or (item["retention"] == "long_term" and memory["retention"] != "long_term")):
                    return False
                if source.get("record_kind") in {"request", "clarification"}:
                    if source["record_kind"] not in memory.get("context_sources", []):
                        return False
                elif not within_selectors(source["fields"], memory.get("source_fields", [])):
                    return False
                elif any(not within_selectors(source["fields"], ceiling.get(mode, []))
                         for mode in ("allowed_model_fields", "retained_fields")):
                    return False
                if export and not within_selectors(source["fields"], ceiling.get("export_fields", [])):
                    return False
                if any(selector_covers(mask["field"], field) or selector_covers(field, mask["field"])
                       for mask in ceiling.get("masking", []) for field in source["fields"]):
                    return False
        return True

    def _allowed(self, item, policy, instance_ids=None, *, profile, check_sources=True):
        instances = set(item["instance_ids"])
        return (policy.get("model_allowed") is True and policy.get("retain_allowed") is True
                and item["category"] in policy.get("categories", [])
                and instances <= set(policy.get("instance_ids", []))
                and all(policy.get("target_bindings", {}).get(key) == target
                        for key, target in item["_target_bindings"].items())
                and (instance_ids is None or instances <= set(instance_ids))
                and (item["retention"] != "long_term" or policy.get("long_term_allowed") is True)
                and (not check_sources or self._sources_available(item)) and self._resource_allowed(item, profile))

    def _sources_available(self, item):
        for source in item["_sources"]:
            row = self.store._db.execute("SELECT blocked,expires_at FROM records WHERE task_id=? AND record_ref=?",
                                         (source["task_id"], source["record_ref"])).fetchone()
            if row is None:
                return False
            expired = row["expires_at"] is not None and _instant(row["expires_at"]) <= datetime.now(UTC)
            if (row["blocked"] and not expired) or (expired and item["retention"] != "long_term"):
                return False
        return True

    def _row(self, profile_id, memory_ref, *, check_policy=True, allow_expired=False):
        row = self.store._db.execute("""SELECT v.*,h.current_ref,h.invalidated FROM memory_versions v
            JOIN memory_heads h USING(memory_id) WHERE v.profile_id=? AND v.memory_ref=?""",
            (profile_id, memory_ref)).fetchone()
        _require(row is not None, "NOT_FOUND", "해당 profile의 기억 참조를 찾을 수 없습니다.")
        _require(row["current_ref"] == memory_ref and not row["invalidated"],
                 "MEMORY_CONFLICT", "기억이 정정되거나 무효화되어 현재 개정을 다시 확인해야 합니다.")
        if not allow_expired:
            _require((row["expires_at"] is None or _instant(row["expires_at"]) > datetime.now(UTC)) and row["body"] is not None,
                     "RECORD_EXPIRED", "기억의 보존기한이 끝났습니다.")
        item = json.loads(row["body"]) if row["body"] is not None else {}
        # The canonical expiry column controls access even while cleanup is pending.
        item["expires_at"] = row["expires_at"]
        _require(row["body"] is None or row["expires_at"] is not None or item.get("retention") in {"source_bound", "long_term"},
                 "RESULT_VALIDATION_FAILED", "기억의 보존 유형을 확인할 수 없습니다.")
        if check_policy:
            profile, policy = self._policy(profile_id)
            _require(self._allowed(item, policy, profile=profile), "ACCESS_DENIED", "현재 profile 정책·실제 대상이 기억 제공을 허용하지 않습니다.")
        return row, item

    def _public(self, item):
        result = {key: value for key, value in item.items() if not key.startswith("_")}
        result["sources"] = []
        for source in item["_sources"]:
            try:
                self.store.read_record(source["task_id"], source["record_ref"])
            except StoreError:
                continue
            if source["expires_at"] is None or _instant(source["expires_at"]) > datetime.now(UTC):
                result["sources"].append({key: source[key] for key in ("task_id", "record_ref", "fields")})
        return result

    def get(self, profile_id, memory_ref, *, instance_ids=None):
        with self.store.transaction():
            _, item = self._row(profile_id, memory_ref)
            _require(instance_ids is None or set(item["instance_ids"]) <= set(instance_ids),
                     "ACCESS_DENIED", "기억의 실제 대상이 요청한 범위를 초과합니다.")
            return self._public(item)

    def can_export(self, profile_id, memory_ref):
        try:
            with self.store.transaction():
                profile, policy = self._policy(profile_id)
                _, item = self._row(profile_id, memory_ref)
                if policy.get("exports_allowed") is not True:
                    return False
                return self._resource_allowed(item, profile, export=True)
        except StoreError as error:
            if error.code == "STORE_UNAVAILABLE":
                raise
            return False

    def save(self, profile_id, candidate, *, source_records, policy, memory_ref=None, index=True):
        required = {"category", "statement", "applicability", "instance_ids", "verification_method"}
        _require(isinstance(candidate, dict) and set(candidate) == required
                 and candidate["category"] in CATEGORIES
                 and all(isinstance(candidate[key], str) and candidate[key].strip()
                         for key in ("statement", "applicability", "verification_method"))
                 and len(_json(candidate).encode()) <= 16384
                 and isinstance(candidate["instance_ids"], list) and bool(candidate["instance_ids"])
                 and all(isinstance(value, str) and value for value in candidate["instance_ids"])
                 and len(candidate["instance_ids"]) == len(set(candidate["instance_ids"])),
                 "INVALID_INPUT", "지식 범주·적용 조건·확인 방법·실제 대상을 가진 기억 후보가 필요합니다.")
        with self.store.transaction():
            profile, current_policy = self._policy(profile_id)
            sources = self._sources(profile_id, source_records)
            for source in sources:
                source["export_allowed"] = source["export_allowed"] and current_policy.get("exports_allowed") is True
            retention = policy.get("retention")
            _require(retention in {"source_bound", "long_term"}, "ACCESS_DENIED", "기억 보관을 승인한 계획이 필요합니다.")
            bindings = {}
            for source in sources:
                for instance in source.get("instance_ids", []):
                    target = source.get("target_bindings", {}).get(instance)
                    _require(isinstance(target, str) and target and (instance not in bindings or bindings[instance] == target),
                             "RESULT_VALIDATION_FAILED", "출처의 실제 연결 대상이 일치하지 않습니다.")
                    bindings[instance] = target
            _require(set(bindings) == set(candidate["instance_ids"]),
                     "RESULT_VALIDATION_FAILED", "후보의 기여 리소스가 실제 출처와 일치해야 합니다.")
            item = {**redact(candidate), "retention": retention, "_target_bindings": bindings, "_sources": sources}
            _require(self._allowed(item, current_policy, profile=profile) and self._allowed(item, policy, profile=profile),
                     "ACCESS_DENIED", "profile과 승인 계획의 기억 범위 안에서만 저장할 수 있습니다.")
            if retention == "long_term":
                _require(all(source.get("long_term_allowed") is True for source in sources),
                         "ACCESS_DENIED", "모든 출처가 독립적인 장기 운영지식 보존을 허용해야 합니다.")
                limits = [profile["long_term_memory_days"], policy.get("retention_days", profile["long_term_memory_days"])]
                _require(all(value is None or (type(value) is int and value > 0) for value in limits),
                         "INVALID_INPUT", "서버가 해소한 장기 보존기간은 null 또는 양의 정수여야 합니다.")
                days = _finite_min(*limits)
                expiry = datetime.now(UTC) + timedelta(days=days) if days is not None else None
            else:
                _require("expires_at" in policy, "RESULT_VALIDATION_FAILED", "업무 보존기한 또는 명시적인 무기한 값이 필요합니다.")
                expiry = _finite_min(*(_expiry(source["expires_at"]) for source in sources), _expiry(policy["expires_at"]))
            content_hash = _digest({key: item[key] for key in required})
            source_hash = _digest(sorted([{
                key: source[key] for key in ("task_id", "record_ref", "fields", "verified_at")}
                for source in sources], key=_json))
            previous = None
            if memory_ref is not None:
                previous, old = self._row(profile_id, memory_ref)
                _require(old["_target_bindings"] == bindings, "ACCESS_DENIED", "다른 실제 대상의 기억을 같은 ID로 정정할 수 없습니다.")
            source_times = {_source_key(source): _instant(source["verified_at"]) for source in sources}
            keys = set(source_times)
            history = self.store._db.execute("""SELECT v.memory_id,v.content_hash,v.created_at,v.expires_at,
                v.source_lineage,v.body IS NOT NULL AS body_available,h.invalidated FROM memory_versions v
                JOIN memory_heads h USING(memory_id) WHERE v.profile_id=?""", (profile_id,)).fetchall()
            for version in history:
                lineage = json.loads(version["source_lineage"]) if version["source_lineage"] else {"keys": None}
                old_keys = set(lineage["keys"]) if lineage["keys"] is not None else None
                if (old_keys is not None and not keys & old_keys and version["content_hash"] != content_hash
                        and (previous is None or version["memory_id"] != previous["memory_id"])):
                    continue
                expired = version["expires_at"] is not None and _instant(version["expires_at"]) <= datetime.now(UTC)
                cutoff = (lineage.get("blocked_at") or (version["expires_at"] if expired else None)
                          or version["created_at"])
                fresh = any(key not in (old_keys or set()) and verified > _instant(cutoff)
                            for key, verified in source_times.items())
                if not fresh:
                    _require(not version["invalidated"] and (version["body_available"] or expired),
                             "MEMORY_CONFLICT", "사용 차단·정리한 기억의 출처는 새로 확인한 자료 없이 다시 사용할 수 없습니다.")
                    expiry = _finite_min(expiry, _expiry(version["expires_at"]))
            _require(expiry is None or expiry > datetime.now(UTC), "RECORD_EXPIRED", "기존 출처를 반복 사용해 기억의 기한을 연장할 수 없습니다.")
            duplicate = self.store._db.execute("""SELECT v.memory_ref FROM memory_versions v JOIN memory_heads h USING(memory_id)
                WHERE v.profile_id=? AND v.content_hash=? AND v.source_hash=? AND h.current_ref=v.memory_ref
                AND h.invalidated=0""", (profile_id, content_hash, source_hash)).fetchone()
            if duplicate:
                row, existing = self._row(profile_id, duplicate[0])
                return {**self._public(existing), "index_state": row["index_state"]}
            instant = datetime.now(UTC).isoformat()
            identity = previous["memory_id"] if previous else f"memory_{uuid4().hex}"
            reference = f"memory_{uuid4().hex}"
            version = previous["version"] + 1 if previous else 1
            item.update(memory_ref=reference, verified_at=max(source["verified_at"] for source in sources),
                        expires_at=expiry.isoformat() if expiry is not None else None)
            self.store._db.execute("""INSERT INTO memory_heads(memory_id,profile_id,current_ref) VALUES(?,?,?)
                ON CONFLICT(memory_id) DO UPDATE SET current_ref=excluded.current_ref,invalidated=0""",
                (identity, profile_id, reference))
            self.store._db.execute("""INSERT INTO memory_versions(memory_ref,memory_id,profile_id,version,body,
                content_hash,source_hash,created_at,expires_at,index_state,source_lineage) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (reference, identity, profile_id, version, _json(item), content_hash, source_hash, instant,
                 item["expires_at"], "pending", _json({"keys": sorted(keys), "blocked_at": None})))
            if previous:
                self._drop_indexes(previous["memory_ref"])
        state = self._index(profile_id, reference) if index else "pending"
        return {**self.get(profile_id, reference), "index_state": state}

    def _drop_indexes(self, memory_ref):
        self.store._db.execute("DELETE FROM memory_fts WHERE memory_ref=?", (memory_ref,))
        self.store._db.execute("DELETE FROM memory_vectors WHERE memory_ref=?", (memory_ref,))

    def _vector(self, values, embedder=None):
        embedder = embedder or self.embedder
        _require(embedder is not None and len(values) == embedder.dimensions and all(
            type(value) in (int, float) and math.isfinite(value) for value in values)
            and any(value != 0 for value in values), "MEMORY_INDEX_INCOMPLETE", "유효한 임베딩 벡터가 필요합니다.")
        try:
            packed = struct.pack(f"<{len(values)}f", *values)
            _require(any(struct.unpack(f"<{len(values)}f", packed)), "MEMORY_INDEX_INCOMPLETE", "유효한 임베딩 벡터가 필요합니다.")
            return packed
        except (OverflowError, struct.error):
            raise StoreError("MEMORY_INDEX_INCOMPLETE", "저장 가능한 임베딩 벡터가 필요합니다.") from None

    def _index(self, profile_id, memory_ref):
        try:
            with self.store.transaction():
                _, item = self._row(profile_id, memory_ref)
                profile, _ = self._policy(profile_id)
                text = f"{item['category']}\n{item['statement']}\n{item['applicability']}"
                self.store._db.execute("UPDATE memory_versions SET index_state='pending' WHERE memory_ref=?", (memory_ref,))
                self.store._db.execute("DELETE FROM memory_fts WHERE memory_ref=?", (memory_ref,))
                self.store._db.execute("INSERT INTO memory_fts VALUES(?,?)", (memory_ref, text))
                if profile["embedding"]["provider"] == "keyword_only":
                    self.store._db.execute("UPDATE memory_versions SET index_state='ready' WHERE memory_ref=?", (memory_ref,))
                    return "ready"
            embedder = self._selected_embedder(profile)
            if embedder is not None and self.vector_ready:
                self._check_embedding_context(profile, [memory_ref])
                vector = self._vector(embedder.embed_document(text), embedder)
                with self.store.transaction():
                    self._row(profile_id, memory_ref)
                    current, _ = self._policy(profile_id)
                    _require(current["embedding"] == profile["embedding"], "CONTEXT_CHANGED", "색인 중 임베딩 설정이 변경되었습니다.")
                    self.store._db.execute("INSERT OR REPLACE INTO memory_vectors VALUES(?,?,?,?)",
                        (memory_ref, embedder.model_id, embedder.dimensions, vector))
                    self.store._db.execute("UPDATE memory_versions SET index_state='ready' WHERE memory_ref=?", (memory_ref,))
                return "ready"
        except (StoreError, sqlite3.Error, ValueError, RuntimeError, OSError):
            pass
        return "pending"

    def search(self, profile_id, query, limit=10, instance_ids=None):
        _require(isinstance(query, str) and query.strip() and len(query.encode()) <= 4096
                 and type(limit) is int and 0 < limit <= 50,
                 "INVALID_INPUT", "검색어와 1~50건의 반환 한도를 지정하세요.")
        with self.store.transaction():
            profile, policy = self._policy(profile_id)
            _require(instance_ids is None or (isinstance(instance_ids, list) and bool(instance_ids)
                     and all(isinstance(value, str) for value in instance_ids)
                     and set(instance_ids) <= set(policy.get("instance_ids", []))),
                     "ACCESS_DENIED", "현재 profile의 기억 조회 허용 대상만 선택할 수 있습니다.")
            rows = self.store._db.execute("""SELECT v.* FROM memory_versions v JOIN memory_heads h USING(memory_id)
                WHERE h.profile_id=? AND h.current_ref=v.memory_ref AND h.invalidated=0 AND v.body IS NOT NULL
                AND (v.expires_at IS NULL OR v.expires_at>?) ORDER BY v.memory_ref LIMIT 10001""", (profile_id, datetime.now(UTC).isoformat())).fetchall()
            eligible = {}
            for row in rows[:10000]:
                item = json.loads(row["body"])
                item["expires_at"] = row["expires_at"]
                if self._allowed(item, policy, instance_ids, profile=profile):
                    eligible[row["memory_ref"]] = item
        query_vector = None
        embedder = self._selected_embedder(profile) if eligible or profile["embedding"]["provider"] == "local" else None
        if embedder is not None and self.vector_ready:
            try:
                if getattr(embedder, "remote", False):
                    self._check_embedding_context(profile, eligible, instance_ids)
                query_vector = self._vector(embedder.embed_query(redact(query)), embedder)
            except (StoreError, sqlite3.Error, ValueError, RuntimeError, OSError):
                pass
        with self.store.transaction():
            current, _ = self._policy(profile_id)
            if current["embedding"] != profile["embedding"]:
                query_vector = None
            # Embedding can wait on a remote service; recheck permissions before ranking.
            for reference in list(eligible):
                try:
                    self._row(profile_id, reference)
                except StoreError:
                    eligible.pop(reference)
            limitations = ["기억 10,000건의 검색 상한에 도달해 일부 항목을 검색하지 못했습니다."] if len(rows) > 10000 else []
            ranked, keyword_ok, vector_ok = [], False, False
            allowed = list(eligible)
            placeholders = ",".join("?" for _ in allowed) or "NULL"
            tokens = re.findall(r"[^\W_]+", redact(query), re.UNICODE)[:64]
            expression = " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)
            try:
                hits = self.store._db.execute(f"""SELECT memory_ref FROM memory_fts WHERE memory_fts MATCH ?
                    AND memory_ref IN ({placeholders}) ORDER BY bm25(memory_fts),memory_ref LIMIT ?""",
                    [expression, *allowed, limit * 4]).fetchall() if expression else []
                ranked.append([row[0] for row in hits])
                keyword_ok = True
                indexed = self.store._db.execute(f"SELECT count(*) FROM memory_fts WHERE memory_ref IN ({placeholders})", allowed).fetchone()[0]
                if indexed < len(allowed):
                    limitations.append("키워드 색인 갱신이 대기 중인 기억이 있습니다.")
            except sqlite3.Error:
                limitations.append("키워드 색인을 사용할 수 없습니다.")
            if query_vector is not None:
                try:
                    vectors = self.store._db.execute(f"""SELECT memory_ref FROM memory_vectors
                        WHERE model_id=? AND dimensions=? AND memory_ref IN ({placeholders})
                        ORDER BY vec_distance_cosine(embedding,?),memory_ref LIMIT ?""",
                        [embedder.model_id, embedder.dimensions, *allowed, query_vector, limit * 4]).fetchall()
                    ranked.append([row[0] for row in vectors])
                    vector_ok = True
                    indexed = self.store._db.execute(f"""SELECT count(*) FROM memory_vectors
                        WHERE model_id=? AND dimensions=? AND memory_ref IN ({placeholders})""",
                        [embedder.model_id, embedder.dimensions, *allowed]).fetchone()[0]
                    if indexed < len(allowed):
                        limitations.append("현재 모델의 벡터 색인 갱신이 대기 중인 기억이 있습니다.")
                except (StoreError, sqlite3.Error, ValueError, RuntimeError, OSError):
                    limitations.append(VECTOR_LIMITATION)
            elif profile["embedding"]["provider"] != "keyword_only":
                limitations.append(VECTOR_LIMITATION)
            scores = {}
            for ranking in ranked:
                for position, reference in enumerate(ranking, start=1):
                    scores[reference] = scores.get(reference, 0) + 1 / (60 + position)
            items, size = [], 0
            for reference in sorted(scores, key=lambda ref: (-scores[ref], ref))[:limit]:
                # Re-read canonical state after candidate selection; indexes are never authoritative.
                try:
                    _, item = self._row(profile_id, reference)
                except StoreError:
                    continue
                public = self._public(item)
                item_size = len(_json(public).encode())
                if size + item_size > 65536:
                    limitations.append("검색 결과 본문 64KiB 상한으로 일부 기억을 제외했습니다.")
                    break
                size += item_size
                items.append(public)
                if len(public["sources"]) < len(item["_sources"]):
                    limitations.append("일부 원출처의 본문은 만료되었거나 현재 열람할 수 없습니다.")
            state = "hybrid" if keyword_ok and vector_ok else "keyword_only" if keyword_ok else "vector_only" if vector_ok else "unavailable"
            return {"items": items, "retrieval_state": state, "limitations": list(dict.fromkeys(limitations))}

    def invalidate(self, profile_id, memory_ref, *, reason, source_records):
        _require(isinstance(reason, str) and reason.strip(), "INVALID_INPUT", "실제 반증·사용자 정정의 이유가 필요합니다.")
        with self.store.transaction():
            row, _ = self._row(profile_id, memory_ref, check_policy=False, allow_expired=True)
            self._sources(profile_id, source_records, require_retention=False)
            self.store._db.execute("UPDATE memory_heads SET invalidated=1 WHERE memory_id=?", (row["memory_id"],))
            self._drop_indexes(memory_ref)
            self.store._db.execute("""UPDATE memory_versions SET body=NULL,index_state='pending',
                source_lineage=json_set(COALESCE(source_lineage,'{"keys":null}'),'$.blocked_at',?) WHERE memory_id=?""",
                (datetime.now(UTC).isoformat(), row["memory_id"]))
            return {"memory_ref": memory_ref, "memory_state": "invalidated", "expires_at": None, "index_state": "ready"}

    def reindex(self, profile_id=None):
        with self.store.transaction():
            rows = self.store._db.execute("""SELECT v.profile_id,v.memory_ref FROM memory_versions v
                JOIN memory_heads h USING(memory_id) WHERE h.current_ref=v.memory_ref AND h.invalidated=0
                AND v.body IS NOT NULL AND (v.expires_at IS NULL OR v.expires_at>?)
                AND NOT EXISTS (SELECT 1 FROM profile_deletions d WHERE d.profile_id=v.profile_id)""",
                (datetime.now(UTC).isoformat(),)).fetchall()
        states = [self._index(row["profile_id"], row["memory_ref"]) for row in rows
                  if profile_id is None or row["profile_id"] == profile_id]
        return {"ready": states.count("ready"), "pending": states.count("pending")}

    def cleanup(self):
        with self.store.transaction():
            rows = self.store._db.execute("""SELECT v.memory_ref,v.profile_id,v.body,v.expires_at
                FROM memory_versions v WHERE v.body IS NOT NULL
                AND NOT EXISTS (SELECT 1 FROM profile_deletions d WHERE d.profile_id=v.profile_id)""").fetchall()
            deleted = 0
            policies = {}
            for row in rows:
                if row["profile_id"] not in policies:
                    policies[row["profile_id"]] = self._policy(row["profile_id"])
                expired = row["expires_at"] is not None and _instant(row["expires_at"]) <= datetime.now(UTC)
                item = json.loads(row["body"])
                allowed = self._allowed(item, policies[row["profile_id"]][1],
                                        profile=policies[row["profile_id"]][0], check_sources=False)
                if expired or not allowed or not self._sources_available(item):
                    self._drop_indexes(row["memory_ref"])
                    self.store._db.execute("""UPDATE memory_versions SET body=NULL,index_state='pending',
                        source_lineage=json_set(COALESCE(source_lineage,'{"keys":null}'),'$.blocked_at',
                            COALESCE(json_extract(source_lineage,'$.blocked_at'),?)) WHERE memory_ref=?""",
                        (row["expires_at"] if expired and allowed else datetime.now(UTC).isoformat(), row["memory_ref"]))
                    deleted += 1
            return {"deleted": deleted, "pending": 0}
