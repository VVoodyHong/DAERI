"""Private local persistence; body files are published only through SQLite refs."""

import hashlib
import json
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4


class StoreError(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(f"{code}: {message}")


def _json(value) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                          separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise StoreError("INVALID_INPUT", "A JSON-compatible value is required.") from None


def _profile_memory_policy(value, resources):
    flags = {"model_allowed", "retain_allowed", "long_term_allowed", "exports_allowed"}
    if not isinstance(value, dict) or not set(value) <= flags | {"categories", "instance_ids", "target_bindings"}:
        raise StoreError("INVALID_INPUT", "Invalid profile memory policy fields.")
    if not value:
        return {}
    for key in flags & value.keys():
        if type(value[key]) is not bool:
            raise StoreError("INVALID_INPUT", "Memory permissions must be explicit booleans.")
    targets = {item["instance_id"]: item["target"] for item in resources}
    for key, allowed in (("categories", {"service_rule", "resource_structure", "procedure", "lesson"}),
                         ("instance_ids", targets.keys())):
        items = value.get(key, [])
        if (not isinstance(items, list) or any(not isinstance(item, str) for item in items)
                or len(items) != len(set(items)) or not set(items) <= allowed):
            raise StoreError("INVALID_INPUT", "Memory categories and registered resource IDs must be unique lists.")
    selected = value.get("instance_ids", [])
    supplied = value.get("target_bindings", {})
    if (not isinstance(supplied, dict) or not set(supplied) <= set(selected)
            or any(not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                   for digest in supplied.values())):
        raise StoreError("INVALID_INPUT", "Memory target bindings must identify selected resource targets.")
    # Bindings describe current targets; they never select resources or grant permissions.
    return {**value, "target_bindings": {key: hashlib.sha256(_json(targets[key])).hexdigest() for key in selected}}


def _time(value: str | datetime) -> str:
    try:
        instant = datetime.fromisoformat(value) if isinstance(value, str) else value
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError
        return instant.astimezone(UTC).isoformat(timespec="microseconds")
    except (TypeError, ValueError, AttributeError):
        raise StoreError("INVALID_INPUT", "A timestamp with a timezone is required.") from None


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self._lock = threading.RLock()
        try:
            for folder in (self.data_dir, *(self.data_dir / name for name in ("profiles", "records", "staging", "artifacts"))):
                if folder.is_symlink():
                    raise OSError("Managed storage directories must not be symbolic links.")
                folder.mkdir(parents=True, exist_ok=True, mode=0o700)
                folder.chmod(0o700)
            database = self.data_dir / "daeri.sqlite3"
            if any(Path(f"{database}{suffix}").is_symlink() for suffix in ("", "-wal", "-shm", "-journal")):
                raise OSError("SQLite files must not be symbolic links.")
            descriptor = os.open(database, os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
            os.close(descriptor)
            database.chmod(0o600)
            self._db = sqlite3.connect(database, isolation_level=None, timeout=5, check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA foreign_keys=ON")
            deadline = monotonic() + 5
            while True:
                try:
                    if self._db.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                        raise StoreError("STORE_UNAVAILABLE", "SQLite WAL is required.")
                    break
                except sqlite3.OperationalError as error:
                    if error.sqlite_errorcode not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or monotonic() >= deadline:
                        raise
                    sleep(0.02)
            self._db.execute("PRAGMA synchronous=FULL")
            with self.transaction():
                columns = {
                    "profiles": "profile_id name revision path size sha256",
                    "tasks": "task_id profile_id revision metadata expires_at",
                    "records": "record_ref task_id kind path size sha256 expires_at blocked cleaned",
                    "requests": "scope request_key fingerprint response",
                }
                tables = {row[0] for row in self._db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if tables:
                    if "artifact_files" in tables:
                        columns["artifact_files"] = "artifact_ref task_id path size sha256"
                    for table, required in columns.items():
                        actual = {row[1] for row in self._db.execute(f"PRAGMA table_info({table})")}
                        if not set(required.split()) <= actual:
                            raise StoreError("STORE_UNAVAILABLE", "The existing storage schema requires recovery.")
                elif (any(any((self.data_dir / folder).glob("*.json")) for folder in ("profiles", "records", "staging"))
                      or any((self.data_dir / "artifacts").iterdir())):
                    raise StoreError("STORE_UNAVAILABLE", "Existing records require their original database.")
                schema = """
                CREATE TABLE IF NOT EXISTS profiles (
                    profile_id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL,
                    revision INTEGER NOT NULL, path TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY, profile_id TEXT REFERENCES profiles(profile_id),
                    revision INTEGER NOT NULL, metadata TEXT NOT NULL, expires_at TEXT);
                CREATE INDEX IF NOT EXISTS tasks_profile ON tasks(profile_id);
                CREATE TABLE IF NOT EXISTS records (
                    record_ref TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    kind TEXT NOT NULL, path TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL,
                    expires_at TEXT, blocked INTEGER NOT NULL DEFAULT 0, cleaned INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS requests (
                    scope TEXT NOT NULL, request_key TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    response TEXT NOT NULL, PRIMARY KEY(scope, request_key));
                CREATE TABLE IF NOT EXISTS artifact_files (
                    artifact_ref TEXT PRIMARY KEY REFERENCES records(record_ref),
                    task_id TEXT NOT NULL REFERENCES tasks(task_id), path TEXT NOT NULL,
                    size INTEGER NOT NULL, sha256 TEXT NOT NULL);
                """
                for statement in schema.split(";"):
                    if statement.strip():
                        self._db.execute(statement)
            self._db.execute("CREATE VIRTUAL TABLE temp.fts5_probe USING fts5(body)")
            self._db.execute("DROP TABLE temp.fts5_probe")
            self.cleanup()
        except (OSError, sqlite3.Error, StoreError) as error:
            if hasattr(self, "_db"):
                self._db.close()
            if isinstance(error, StoreError):
                raise
            raise StoreError("STORE_UNAVAILABLE", "Local storage could not be opened.") from None

    @contextmanager
    def transaction(self):
        with self._lock:
            outer = not self._db.in_transaction
            try:
                if outer:
                    self._db.execute("BEGIN IMMEDIATE")
                yield self
                if outer:
                    self._db.commit()
            except BaseException as error:
                if outer:
                    self._db.rollback()
                if isinstance(error, sqlite3.Error):
                    raise StoreError("STORE_UNAVAILABLE", "Local storage operation failed.") from None
                raise

    def close(self):
        with self._lock:
            self._db.close()

    def _path(self, reference: str, folder: str) -> Path:
        extension = r"[a-z0-9]{1,8}" if folder == "artifacts" else "json"
        if (not re.fullmatch(rf"{folder}/[0-9a-f]{{32}}\.{extension}", reference)
                or (self.data_dir / folder).is_symlink()):
            raise StoreError("STORE_UNAVAILABLE", "A registered storage path is invalid.")
        return self.data_dir / reference

    def _prepare(self, folder: str, value) -> tuple[str, int, str]:
        return self._prepare_bytes(folder, _json(value), "json")

    def _prepare_bytes(self, folder, content, extension):
        token = uuid4().hex
        name = f"{token}.{extension}"
        staging, destination = self._path(f"staging/{token}.json", "staging"), self._path(f"{folder}/{name}", folder)
        try:
            descriptor = os.open(staging, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            staging.rename(destination)
            if os.name != "nt":
                directory = os.open(destination.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except OSError:
            raise StoreError("STORE_UNAVAILABLE", "A private record could not be prepared.") from None
        return f"{folder}/{name}", len(content), hashlib.sha256(content).hexdigest()

    def _read(self, row, error_code: str):
        try:
            path = self._path(row["path"], "profiles" if error_code == "STORE_UNAVAILABLE" else "records")
            if path.is_symlink():
                raise ValueError
            content = path.read_bytes()
            if len(content) != row["size"] or hashlib.sha256(content).hexdigest() != row["sha256"]:
                raise ValueError
            return json.loads(content)
        except (OSError, ValueError, UnicodeError, StoreError):
            raise StoreError(error_code, "The registered file is missing or has changed.") from None

    def put_profile(self, name, *, core_context=None, resources=None,
                    retention_days=None, long_term_memory_days=None, memory_policy=None, create_only=False) -> dict:
        core_context = [] if core_context is None else core_context
        resources = [] if resources is None else resources
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name)
                or not isinstance(core_context, list) or any(not isinstance(item, str) for item in core_context)
                or not isinstance(resources, list)
                or any(days is not None and (type(days) is not int or days <= 0)
                       for days in (retention_days, long_term_memory_days))):
            raise StoreError("INVALID_INPUT", "Invalid profile name, context, or retention policy.")
        if resources:
            from .resources import validate_resource

            for resource in resources:
                validate_resource(resource)
                from .policy import validate_resource_policy
                try:
                    validate_resource_policy(resource.get("policy", {}))
                except StoreError:
                    raise StoreError("INVALID_INPUT", "The resource field or memory policy is invalid.") from None
            if len({item["instance_id"] for item in resources}) != len(resources):
                raise StoreError("INVALID_INPUT", "Resource instance IDs must be unique.")
        memory_policy = _profile_memory_policy({} if memory_policy is None else memory_policy, resources)
        with self.transaction():
            current = self._db.execute("SELECT * FROM profiles WHERE name=?", (name,)).fetchone()
            if current and create_only:
                raise StoreError("PROFILE_ALREADY_EXISTS", "The profile already exists; inspect it before updating its configuration.")
            if current:
                self.get_profile(current["profile_id"])
            profile = {"profile_id": current["profile_id"] if current else f"profile_{uuid4().hex}",
                       "name": name, "revision": current["revision"] + 1 if current else 1,
                       "core_context": core_context, "resources": resources,
                       "retention_days": retention_days, "long_term_memory_days": long_term_memory_days,
                       "memory_policy": memory_policy}
            path, size, digest = self._prepare("profiles", profile)
            self._db.execute("""INSERT INTO profiles VALUES (?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET
                revision=excluded.revision, path=excluded.path, size=excluded.size, sha256=excluded.sha256""",
                (profile["profile_id"], name, profile["revision"], path, size, digest))
            return profile

    def get_profile(self, name_or_id) -> dict:
        with self.transaction():
            rows = self._db.execute("SELECT * FROM profiles WHERE profile_id=? OR name=?",
                                   (name_or_id, name_or_id)).fetchall()
            if not rows:
                raise StoreError("NOT_FOUND", "Profile was not found.")
            if len(rows) != 1:
                raise StoreError("CONTEXT_REQUIRED", "The profile name and ID refer to different profiles.")
            row = rows[0]
            profile = self._read(row, "STORE_UNAVAILABLE")
            if any(profile.get(key) != row[key] for key in ("profile_id", "name", "revision")):
                raise StoreError("STORE_UNAVAILABLE", "The active profile revision does not match.")
            return profile

    def list_profiles(self) -> list:
        with self.transaction():
            return [self.get_profile(row[0]) for row in self._db.execute("SELECT profile_id FROM profiles ORDER BY name")]

    def insert_task(self, task: dict) -> None:
        with self.transaction():
            if task["profile_id"] is not None:
                self.get_profile(task["profile_id"])
            self._db.execute("INSERT INTO tasks(task_id,profile_id,revision,metadata) VALUES(?,?,?,?)",
                             (task["task_id"], task["profile_id"], task["task_revision"], _json(task).decode()))

    def get_task(self, task_id) -> dict:
        with self.transaction():
            row = self._db.execute("SELECT metadata FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                raise StoreError("NOT_FOUND", "Task was not found.")
            return json.loads(row[0])

    def list_tasks(self, profile_id, limit=20, *, offset=0) -> list:
        if type(limit) is not int or limit <= 0 or type(offset) is not int or offset < 0:
            raise StoreError("INVALID_INPUT", "A positive task limit and nonnegative integer offset are required.")
        with self.transaction():
            return [json.loads(row[0]) for row in self._db.execute(
                """SELECT metadata FROM tasks WHERE profile_id=?
                ORDER BY COALESCE(json_extract(metadata,'$.last_activity_at'),json_extract(metadata,'$.created_at')) DESC,
                task_id ASC LIMIT ? OFFSET ?""",
                (profile_id, limit, offset))]

    def task_page_signature(self, profile_id) -> str:
        with self.transaction():
            revisions = [list(row) for row in self._db.execute(
                "SELECT task_id,revision FROM tasks WHERE profile_id=? ORDER BY task_id", (profile_id,))]
            return hashlib.sha256(_json(revisions)).hexdigest()

    def update_task(self, task: dict, expected_revision: int) -> None:
        with self.transaction():
            current = self.get_task(task["task_id"])
            if current["task_revision"] != expected_revision:
                raise StoreError("REVISION_CONFLICT", "The task revision has changed.")
            if task["task_revision"] != expected_revision + 1:
                raise StoreError("INVALID_INPUT", "Task updates must increment the revision.")
            if task["profile_id"] is not None:
                self.get_profile(task["profile_id"])
            self._db.execute("UPDATE tasks SET revision=?,metadata=?,profile_id=? WHERE task_id=?",
                             (task["task_revision"], _json(task).decode(), task["profile_id"], task["task_id"]))

    def request_get(self, scope, key) -> dict | None:
        with self.transaction():
            row = self._db.execute("SELECT fingerprint,response FROM requests WHERE scope=? AND request_key=?",
                                   (scope, key)).fetchone()
            return {"fingerprint": row[0], "response": json.loads(row[1])} if row else None

    def request_put(self, scope, key, fingerprint, response) -> None:
        with self.transaction():
            existing = self.request_get(scope, key)
            if existing and existing["fingerprint"] != fingerprint:
                raise StoreError("REQUEST_ID_CONFLICT", "The request key already has different input.")
            if not existing:
                self._db.execute("INSERT INTO requests VALUES (?,?,?,?)", (scope, key, fingerprint, _json(response).decode()))

    def write_record(self, task_id, kind, payload, expires_at=None) -> str:
        if not isinstance(kind, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", kind):
            raise StoreError("INVALID_INPUT", "A record kind is required.")
        with self.transaction():
            self.get_task(task_id)
            task_expiry = self._db.execute("SELECT expires_at FROM tasks WHERE task_id=?", (task_id,)).fetchone()[0]
            expiries = [value for value in (task_expiry, _time(expires_at) if expires_at else None) if value]
            expiry = min(expiries) if expiries else None
            if expiry and expiry <= _time(datetime.now(UTC)):
                raise StoreError("RECORD_EXPIRED", "The task's body retention period has ended.")
            path, size, digest = self._prepare("records", payload)
            ref = f"record_{uuid4().hex}"
            self._db.execute("INSERT INTO records(record_ref,task_id,kind,path,size,sha256,expires_at) VALUES(?,?,?,?,?,?,?)",
                             (ref, task_id, kind, path, size, digest, expiry))
            return ref

    def read_record(self, task_id, record_ref) -> dict:
        with self.transaction():
            row = self._db.execute("SELECT * FROM records WHERE record_ref=? AND task_id=?", (record_ref, task_id)).fetchone()
            if row is None:
                raise StoreError("NOT_FOUND", "Record was not found for this task.")
            if row["blocked"] or (row["expires_at"] and row["expires_at"] <= _time(datetime.now(UTC))):
                raise StoreError("RECORD_EXPIRED", "The record retention period has ended.")
            return {"record_kind": row["kind"], "payload": self._read(row, "RECORD_UNAVAILABLE"),
                    "expires_at": row["expires_at"]}

    def list_records(self, task_id) -> list:
        with self.transaction():
            self.get_task(task_id)
            return [{"record_ref": row[0], "record_kind": row[1], "summary": row[1]}
                    for row in self._db.execute("SELECT record_ref,kind FROM records WHERE task_id=? ORDER BY rowid", (task_id,))]

    def restrict_record(self, task_id, record_ref, payload):
        """Publish a reduced privacy view; immutable plans are never rewritten."""
        with self.transaction():
            record = self.read_record(task_id, record_ref)
            if record["record_kind"] != "call_result":
                raise StoreError("INVALID_INPUT", "Only call bodies can be reduced at task termination.")
            path, size, digest = self._prepare("records", payload)
            self._db.execute("UPDATE records SET path=?,size=?,sha256=? WHERE record_ref=? AND task_id=?",
                             (path, size, digest, record_ref, task_id))

    def write_artifact(self, task_id, content, format_id, media_type, extension, expires_at=None):
        if (not isinstance(content, bytes) or len(content) > 10 * 1024 * 1024
                or not isinstance(extension, str) or not re.fullmatch(r"[a-z0-9]{1,8}", extension)):
            raise StoreError("INVALID_INPUT", "A bounded managed artifact is required.")
        with self.transaction():
            self.get_task(task_id)
            path, size, digest = self._prepare_bytes("artifacts", content, extension)
            ref = self.write_record(task_id, "artifact", {
                "format_id": format_id, "media_type": media_type, "size_bytes": size}, expires_at)
            self._db.execute("INSERT INTO artifact_files VALUES (?,?,?,?,?)", (ref, task_id, path, size, digest))
            return ref

    def artifact_file(self, task_id, record_ref):
        with self.transaction():
            record = self.read_record(task_id, record_ref)
            if record["record_kind"] != "artifact":
                raise StoreError("INVALID_INPUT", "The reference is not an artifact.")
            row = self._db.execute("SELECT * FROM artifact_files WHERE artifact_ref=? AND task_id=?",
                                   (record_ref, task_id)).fetchone()
            try:
                if row is None:
                    raise ValueError
                path = self._path(row["path"], "artifacts")
                if path.is_symlink():
                    raise ValueError
                content = path.read_bytes()
                if len(content) != row["size"] or hashlib.sha256(content).hexdigest() != row["sha256"]:
                    raise ValueError
            except (OSError, ValueError):
                raise StoreError("RECORD_UNAVAILABLE", "The managed artifact is missing or has changed.") from None
            return {"artifact_ref": record_ref, **record["payload"], "local_path": str(path.resolve()),
                    "expires_at": record["expires_at"]}

    def set_task_expiry(self, task_id, expires_at: str | None) -> None:
        expiry = _time(expires_at) if expires_at is not None else None
        with self.transaction():
            self.get_task(task_id)
            if expiry is None:
                return
            self._db.execute("UPDATE tasks SET expires_at=? WHERE task_id=? AND (expires_at IS NULL OR expires_at>?)",
                             (expiry, task_id, expiry))
            self._db.execute("UPDATE records SET expires_at=? WHERE task_id=? AND (expires_at IS NULL OR expires_at>?)",
                             (expiry, task_id, expiry))

    def cleanup(self, now: datetime | None = None) -> dict:
        deleted = 0
        with self.transaction():
            self._db.execute("UPDATE records SET blocked=1 WHERE expires_at<=?", (_time(now or datetime.now(UTC)),))
            for artifact in self._db.execute("SELECT a.path FROM artifact_files a JOIN records r ON a.artifact_ref=r.record_ref WHERE r.blocked=1"):
                try:
                    self._path(artifact[0], "artifacts").unlink(missing_ok=True)
                except OSError:
                    pass
            for row in self._db.execute("SELECT record_ref,path FROM records WHERE blocked=1 AND cleaned=0").fetchall():
                try:
                    self._path(row["path"], "records").unlink(missing_ok=True)
                except OSError:
                    continue
                self._db.execute("UPDATE records SET cleaned=1 WHERE record_ref=?", (row["record_ref"],))
                deleted += 1
            registered = {row[0] for row in self._db.execute("SELECT path FROM records UNION SELECT path FROM profiles UNION SELECT path FROM artifact_files")}
            for folder in ("staging", "profiles", "records", "artifacts"):
                if (self.data_dir / folder).is_symlink():
                    raise StoreError("STORE_UNAVAILABLE", "A managed directory has changed.")
                for path in (self.data_dir / folder).glob("*" if folder == "artifacts" else "*.json"):
                    if f"{folder}/{path.name}" not in registered:
                        try:
                            path.unlink()
                        except OSError:
                            pass
            pending = self._db.execute("SELECT count(*) FROM records WHERE blocked=1 AND cleaned=0").fetchone()[0]
        return {"deleted": deleted, "pending": pending}
