"""Local compact metadata, durable source checkpoints and atomic publication."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import tempfile


def atomic_write_json(path: str | Path, value: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".pakuri-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class WriterBusy(RuntimeError):
    pass


@contextmanager
def writer_lock(state_path: str | Path):
    """OS-held lock survives no crash; stale file existence does not block recovery."""
    lock_path = Path(str(state_path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    stream = open(lock_path, "a+b")
    try:
        # Reading the locked byte itself fails on Windows. Inspect file size instead.
        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise WriterBusy("another Pakuri writer is active") from exc
        else:
            import fcntl
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise WriterBusy("another Pakuri writer is active") from exc
        yield
    finally:
        # Closing the descriptor releases the lock on either operating system.
        stream.close()


class State:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=2)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sources (
                key TEXT PRIMARY KEY, initialized_at TEXT, last_success TEXT);
            CREATE TABLE IF NOT EXISTS items (
                id TEXT PRIMARY KEY, body TEXT NOT NULL, published_at TEXT NOT NULL,
                observed_at TEXT NOT NULL, last_seen TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cache (
                path TEXT PRIMARY KEY, body TEXT NOT NULL, etag TEXT,
                link TEXT, checked_at TEXT NOT NULL, next_poll TEXT);
            CREATE TABLE IF NOT EXISTS repositories (
                name TEXT PRIMARY KEY, body TEXT NOT NULL, origin TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        """)
        self.db.commit()

    def close(self):
        self.db.close()

    def source(self, key):
        row = self.db.execute("SELECT initialized_at,last_success FROM sources WHERE key=?", (key,)).fetchone()
        return {"initialized_at": row[0], "last_success": row[1]} if row else None

    def save_items(self, items, now):
        with self.db:
            for item in items:
                row = self.db.execute("SELECT body FROM items WHERE id=?", (item["id"],)).fetchone()
                if row:
                    existing = json.loads(row[0])
                    # First observation and baseline decision are immutable across sources.
                    item = dict(item, observed_at=existing["observed_at"], baseline=existing["baseline"])
                    if item["id"].startswith("repository:"):
                        # Discovery/creation classification belongs to first observation.
                        item["kind"] = existing["kind"]
                        item["title"] = existing["title"]
                    item["topics"] = sorted(set(existing.get("topics", [])) | set(item.get("topics", [])))
                    item["relevance"] = sorted(set(existing.get("relevance", [])) | set(item.get("relevance", [])))
                    if existing.get("title") and item["title"].startswith("Commit "):
                        item["title"] = existing["title"]
                body = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
                self.db.execute("""INSERT INTO items VALUES (?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET body=excluded.body,last_seen=excluded.last_seen""",
                    (item["id"], body, item["published_at"], item["observed_at"], now))

    def checkpoint(self, key, now):
        with self.db:
            self.db.execute("""INSERT INTO sources VALUES (?,?,?)
                ON CONFLICT(key) DO UPDATE SET last_success=excluded.last_success""", (key, now, now))

    def initialize(self, key, now):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO sources VALUES (?,?,NULL)", (key, now))

    def output_items(self, cutoff):
        rows = self.db.execute("SELECT body FROM items WHERE published_at>=? OR observed_at>=? ORDER BY published_at DESC,id", (cutoff, cutoff))
        return [json.loads(row[0]) for row in rows]

    def count_items(self):
        return self.db.execute("SELECT count(*) FROM items").fetchone()[0]

    def cache_get(self, path):
        row = self.db.execute("SELECT body,etag,link,checked_at,next_poll FROM cache WHERE path=?", (path,)).fetchone()
        if row:
            return dict(zip(("data", "etag", "link", "checked_at", "next_poll"), (json.loads(row[0]), *row[1:])))
        return None

    def cache_put(self, path, data, etag, link, checked_at, next_poll):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?,?,?,?)", (path, json.dumps(data, ensure_ascii=False), etag, link, checked_at, next_poll))

    def prune_cache(self, cutoff):
        with self.db:
            cursor = self.db.execute("DELETE FROM cache WHERE checked_at<?", (cutoff,))
            return cursor.rowcount

    def repository_put(self, data, origin):
        # Callers must validate public status before persistence.
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO repositories VALUES (?,?,?)", (data["full_name"].lower(), json.dumps(data, ensure_ascii=False), origin))

    def repositories(self):
        return [(json.loads(body), origin) for body, origin in self.db.execute("SELECT body,origin FROM repositories ORDER BY name")]

    def repository_remove(self, name):
        with self.db:
            self.db.execute("DELETE FROM repositories WHERE name=?", (name.lower(),))

    def setting(self, key, default=""):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, str(value)))
