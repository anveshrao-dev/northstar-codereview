"""Private project-local SQLite state for the small synchronous approval graph.

Not a general LangGraph backend: full snapshots, root namespace, LastValue
channels, no history API, pruning, async saver, or arbitrary object serializer.
The application owns one transaction/connection per operation. Saver calls from
LangGraph worker threads share it under a lock and NEVER commit independently.
"""

import json
import math
import os
import sqlite3
import stat
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from threading import RLock

from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple, WRITES_IDX_MAP
from langgraph.types import Interrupt

from .approval_models import ApprovalError
from .local_source import PROJECT_ROOT
from .output_safety import contains_sensitive


MAX_STATE_BYTES = 2 * 1024 * 1024


class ApprovalJson:
    """Closed tagged JSON; no pickle, imports, hooks or arbitrary constructors."""
    def _encode(self, value, depth=0):
        if depth > 64:
            raise ApprovalError("invalid_checkpoint")
        child = lambda item: self._encode(item, depth + 1)
        if value is None or type(value) in {bool, int, str}:
            return ["scalar", value]
        if type(value) is float and math.isfinite(value):
            return ["scalar", value]
        if type(value) is dict and all(type(key) is str for key in value):
            return ["dict", [[key, child(item)] for key, item in sorted(value.items())]]
        if type(value) in {tuple, list}:
            return ["tuple" if type(value) is tuple else "list", [child(item) for item in value]]
        if type(value) is Interrupt and value.response_schema is None:
            return ["interrupt", child(value.value), value.id]
        raise ApprovalError("unsupported_checkpoint_type")

    def _decode(self, value, depth=0):
        if depth > 64 or type(value) is not list or not value:
            raise ApprovalError("invalid_checkpoint")
        child = lambda item: self._decode(item, depth + 1)
        if len(value) == 2:
            tag, data = value
            if tag == "scalar" and (data is None or type(data) in {bool, int, str} or type(data) is float and math.isfinite(data)):
                return data
            if tag in {"tuple", "list"} and type(data) is list:
                values = [child(item) for item in data]
                return tuple(values) if tag == "tuple" else values
            if tag == "dict" and type(data) is list:
                result = {}
                for pair in data:
                    if type(pair) is not list or len(pair) != 2 or type(pair[0]) is not str or pair[0] in result:
                        raise ApprovalError("invalid_checkpoint")
                    result[pair[0]] = child(pair[1])
                return result
        if len(value) == 3 and value[0] == "interrupt" and type(value[2]) is str:
            return Interrupt(value=child(value[1]), id=value[2])
        raise ApprovalError("invalid_checkpoint")

    def dumps_typed(self, value):
        encoded = self._encode(value)
        if contains_sensitive(encoded):
            raise ApprovalError("unsafe_checkpoint")
        raw = json.dumps(encoded, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()
        if len(raw) > MAX_STATE_BYTES:
            raise ApprovalError("checkpoint_too_large")
        return "approval-json-v1", raw

    def loads_typed(self, data):
        label, raw = data
        if label != "approval-json-v1" or len(raw) > MAX_STATE_BYTES:
            raise ApprovalError("invalid_checkpoint")
        try:
            encoded = json.loads(raw)
            if contains_sensitive(encoded):
                raise ApprovalError("unsafe_checkpoint")
            return self._decode(encoded)
        except ApprovalError:
            raise
        except Exception:
            raise ApprovalError("invalid_checkpoint") from None


def pack(value):
    raw = ApprovalJson().dumps_typed(value)[1].decode()
    return raw, sha256(raw.encode()).hexdigest()


def unpack(raw, digest):
    if type(raw) is not str or sha256(raw.encode()).hexdigest() != digest:
        raise ApprovalError("invalid_checkpoint")
    return ApprovalJson().loads_typed(("approval-json-v1", raw.encode()))


_SCHEMA = (
    "CREATE TABLE threads (thread_id TEXT PRIMARY KEY, anchor TEXT NOT NULL, digest TEXT NOT NULL, phase TEXT NOT NULL, latest TEXT)",
    "CREATE TABLE checkpoints (thread_id TEXT NOT NULL, checkpoint_id TEXT NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL, PRIMARY KEY(thread_id,checkpoint_id))",
    "CREATE TABLE writes (thread_id TEXT NOT NULL, checkpoint_id TEXT NOT NULL, task_id TEXT NOT NULL, idx INTEGER NOT NULL, task_path TEXT NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL, PRIMARY KEY(thread_id,checkpoint_id,task_id,idx))",
    "CREATE TABLE decisions (thread_id TEXT PRIMARY KEY, payload TEXT NOT NULL, digest TEXT NOT NULL)",
    "CREATE TABLE reviews (review_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, report_hash TEXT NOT NULL, thread_id TEXT UNIQUE NOT NULL)",
)


def checked_path(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if ".." in path.parts or not path.is_relative_to(PROJECT_ROOT / ".runtime") or path.suffix != ".sqlite3":
        raise ApprovalError("unsafe_storage_path")
    return path


def _safe_existing(path, *, directory=False):
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid()
            or (not stat.S_ISDIR(info.st_mode) if directory else not stat.S_ISREG(info.st_mode) or info.st_nlink != 1)):
        raise ApprovalError("unsafe_storage_path")
    if info.st_mode & (0o022 if directory else 0o077):
        raise ApprovalError("unsafe_storage_permissions")
    return info


def _present_without_following(path):
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


@contextmanager
def transaction(path: Path, *, create=False, busy_timeout_ms=250):
    """Serialize all mutations, including graph checkpoint/audit writes, atomically."""
    connection = None
    try:
        path = checked_path(path)
        parent = PROJECT_ROOT
        for part in path.parent.relative_to(PROJECT_ROOT).parts:
            parent = parent / part
            if not _present_without_following(parent):
                if not create:
                    raise ApprovalError("missing_checkpoint")
                parent.mkdir(mode=0o700)
            _safe_existing(parent, directory=True)
        # Refuse linked sidecars before SQLite can open or write one.
        for suffix in ("-journal", "-wal", "-shm"):
            sidecar = Path(str(path) + suffix)
            if _present_without_following(sidecar):
                _safe_existing(sidecar)
        fresh = not _present_without_following(path)
        if fresh:
            if not create:
                raise ApprovalError("missing_checkpoint")
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        before = _safe_existing(path)
        connection = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=busy_timeout_ms / 1000,
                                     isolation_level=None, check_same_thread=False)
        after = _safe_existing(path)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise ApprovalError("unsafe_storage_path")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("BEGIN IMMEDIATE")
        if fresh:
            for statement in _SCHEMA:
                connection.execute(statement)
            connection.execute("PRAGMA user_version=1")
            # Commit the empty schema separately. A first workflow failure or
            # crash then leaves a usable empty database, never half a workflow.
            # An already-existing uninitialized/corrupt file is still refused.
            connection.commit()
            connection.execute("BEGIN IMMEDIATE")
        elif connection.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise ApprovalError("invalid_checkpoint")
        if connection.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
            raise ApprovalError("unsupported_storage_mode")
        yield connection
        connection.commit()
    except ApprovalError:
        raise
    except sqlite3.OperationalError as error:
        code = "storage_busy" if getattr(error, "sqlite_errorcode", None) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} else "storage_error"
        raise ApprovalError(code) from None
    except sqlite3.DatabaseError:
        raise ApprovalError("invalid_checkpoint") from None
    except Exception:
        raise ApprovalError("storage_error") from None
    finally:
        if connection is not None:
            try:
                if connection.in_transaction:
                    connection.rollback()
            finally:
                connection.close()


class ApprovalSaver(BaseCheckpointSaver[int]):
    """Full-checkpoint adapter confined to its application's transaction/thread."""
    def __init__(self, connection, thread_id):
        super().__init__(serde=ApprovalJson())
        self.connection, self.thread_id, self.lock = connection, thread_id, RLock()

    def with_allowlist(self, extra_allowlist):
        return self  # Closed JSON whitelist never expands to arbitrary classes.

    def _config(self, config):
        values = config["configurable"]
        if values["thread_id"] != self.thread_id or values.get("checkpoint_ns", "") != "":
            raise ApprovalError("invalid_checkpoint")
        return values.get("checkpoint_id")

    def get_tuple(self, config):
        checkpoint_id = self._config(config)
        with self.lock:
            row = self.connection.execute("SELECT latest, phase FROM threads WHERE thread_id=?", (self.thread_id,)).fetchone()
            if row is None:
                raise ApprovalError("missing_checkpoint")
            checkpoint_id = checkpoint_id or row[0]
            if checkpoint_id is None and row[1] == "preparing":
                return None
            entry = self.connection.execute("SELECT payload,digest FROM checkpoints WHERE thread_id=? AND checkpoint_id=?", (self.thread_id, checkpoint_id)).fetchone()
            if entry is None:
                raise ApprovalError("invalid_checkpoint")
            data = unpack(*entry)
            if data["checkpoint"]["id"] != checkpoint_id or data["thread_id"] != self.thread_id:
                raise ApprovalError("invalid_checkpoint")
            writes = []
            for task, idx, path, raw, digest in self.connection.execute("SELECT task_id,idx,task_path,payload,digest FROM writes WHERE thread_id=? AND checkpoint_id=? ORDER BY task_path,task_id,idx", (self.thread_id, checkpoint_id)):
                value = unpack(raw, digest)
                if (value["thread_id"], value["checkpoint_id"], value["task_id"], value["idx"], value["task_path"]) != (self.thread_id, checkpoint_id, task, idx, path):
                    raise ApprovalError("invalid_checkpoint")
                writes.append((task, value["channel"], value["value"]))
            def cfg(identity):
                return {"configurable": {"thread_id": self.thread_id, "checkpoint_ns": "", "checkpoint_id": identity}}
            return CheckpointTuple(cfg(checkpoint_id), data["checkpoint"], data["metadata"],
                                   cfg(data["parent"]) if data["parent"] else None, writes)

    def put(self, config, checkpoint, metadata, new_versions):
        parent = self._config(config)
        payload, digest = pack(dict(thread_id=self.thread_id, checkpoint=checkpoint, metadata=metadata, parent=parent))
        with self.lock:
            self.connection.execute("INSERT INTO checkpoints VALUES (?,?,?,?)", (self.thread_id, checkpoint["id"], payload, digest))
            self.connection.execute("UPDATE threads SET latest=? WHERE thread_id=?", (checkpoint["id"], self.thread_id))
        return {"configurable": {"thread_id": self.thread_id, "checkpoint_ns": "", "checkpoint_id": checkpoint["id"]}}

    def put_writes(self, config, writes, task_id, task_path=""):
        checkpoint_id = self._config(config)
        with self.lock:
            for offset, (channel, value) in enumerate(writes):
                idx = WRITES_IDX_MAP.get(channel, offset)
                payload, digest = pack(dict(thread_id=self.thread_id, checkpoint_id=checkpoint_id,
                                            task_id=task_id, idx=idx, task_path=task_path, channel=channel, value=value))
                verb = "INSERT OR REPLACE" if idx < 0 else "INSERT OR IGNORE"
                self.connection.execute(f"{verb} INTO writes VALUES (?,?,?,?,?,?,?)",
                                        (self.thread_id, checkpoint_id, task_id, idx, task_path, payload, digest))
