"""SQLite event store for Glasshouse.

Design rules:
  * one file, no server, no dependency (stdlib sqlite3 only);
  * append-only events with a SHA-256 hash chain (prev_hash -> hash);
  * agents are rows that can be updated (status/heartbeat) but never deleted;
  * the todo list is mirrored from ``TODO.md`` so the file stays the source of truth.

This is a local transparency tool, not a security boundary. The chain detects
accidental edits and naive tampering; it cannot stop someone who can rewrite
the whole database (same residual risk as audit S5).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "glasshouse.db"
GENESIS = "0" * 64

# An agent that has not spoken for this long while claiming to be busy is
# considered stalled. This is what makes a crashed or paused agent visible.
STALL_AFTER = int(os.environ.get("GLASSHOUSE_STALL_AFTER", "180"))
OFFLINE_AFTER = int(os.environ.get("GLASSHOUSE_OFFLINE_AFTER", "900"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'builder',
    harness     TEXT NOT NULL DEFAULT 'unknown',
    model       TEXT NOT NULL DEFAULT 'unknown',
    session_id  TEXT,
    parent_id   TEXT,
    task        TEXT,
    status      TEXT NOT NULL DEFAULT 'working',
    pid         INTEGER,
    host        TEXT,
    cwd         TEXT,
    started_at  REAL NOT NULL,
    last_seen   REAL NOT NULL,
    meta        TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    agent_id    TEXT,
    session_id  TEXT,
    kind        TEXT NOT NULL,
    action      TEXT,
    target      TEXT,
    status      TEXT,
    duration_ms INTEGER,
    detail      TEXT,
    prev_hash   TEXT NOT NULL,
    hash        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS todos (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    status      TEXT NOT NULL,
    priority    TEXT,
    note        TEXT,
    updated     REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_agent ON events(agent_id);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);
"""


def now() -> float:
    return time.time()


def connect(db_path: str | os.PathLike[str] | None = None) -> sqlite3.Connection:
    """Open a project's history.

    Explicit path wins (tests, tooling). Otherwise ``GLASSHOUSE_DB``, otherwise
    the active project under ``~/.local/share/glasshouse``. The repository's old
    ``data/glasshouse.db`` is only copied during project registration, so a
    published copy can never inherit a local history by accident.
    """
    if db_path is not None:
        path = Path(db_path)
    else:
        from . import home as _home
        path = _home.db_for()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


# --------------------------------------------------------------------------- #
# hash chain
# --------------------------------------------------------------------------- #

def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(prev: str, payload: dict[str, Any]) -> str:
    return hashlib.sha256((prev + _canonical(payload)).encode("utf-8")).hexdigest()


def add_event(
    conn: sqlite3.Connection,
    *,
    kind: str,
    agent_id: str | None = None,
    session_id: str | None = None,
    action: str | None = None,
    target: str | None = None,
    status: str | None = None,
    duration_ms: int | None = None,
    detail: Any = None,
    ts: float | None = None,
) -> dict[str, Any]:
    """Append one event and return it. The chain is computed inside one lock."""
    ts = round(now() if ts is None else float(ts), 6)
    detail_text = None if detail is None else (
        detail if isinstance(detail, str) else _canonical(detail)
    )
    payload = {
        "ts": round(ts, 6),
        "agent_id": agent_id,
        "session_id": session_id,
        "kind": kind,
        "action": action,
        "target": target,
        "status": status,
        "duration_ms": duration_ms,
        "detail": detail_text,
    }
    row = None
    digest = ""
    cur = None
    # The chain is global, so the read-last-hash/insert pair must be one
    # serialized transaction across processes. Without BEGIN IMMEDIATE two
    # harnesses writing at once both read the same prev_hash and fork the chain
    # (observed live on 2026-10-25). Retry briefly instead of failing telemetry.
    for attempt in range(6):
        try:
            if conn.in_transaction:
                conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT hash FROM events ORDER BY id DESC LIMIT 1").fetchone()
            prev = row["hash"] if row else GENESIS
            digest = _digest(prev, payload)
            cur = conn.execute(
                """INSERT INTO events
                   (ts, agent_id, session_id, kind, action, target, status, duration_ms,
                    detail, prev_hash, hash)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (ts, agent_id, session_id, kind, action, target, status, duration_ms,
                 detail_text, prev, digest),
            )
            conn.commit()
            break
        except sqlite3.OperationalError as exc:
            conn.rollback()
            if "locked" in str(exc).lower() and attempt < 5:
                time.sleep(0.02 * (attempt + 1))
                continue
            raise
    else:  # pragma: no cover - six lock timeouts in a row is not normal
        raise sqlite3.OperationalError("could not acquire the event-chain lock")
    payload["id"] = cur.lastrowid
    payload["prev_hash"] = prev
    payload["hash"] = digest
    return payload


def verify_chain(conn: sqlite3.Connection) -> tuple[bool, str]:
    """Recompute every event hash. Returns (ok, message)."""
    prev = GENESIS
    count = 0
    for row in conn.execute("SELECT * FROM events ORDER BY id ASC"):
        payload = {
            "ts": row["ts"],
            "agent_id": row["agent_id"],
            "session_id": row["session_id"],
            "kind": row["kind"],
            "action": row["action"],
            "target": row["target"],
            "status": row["status"],
            "duration_ms": row["duration_ms"],
            "detail": row["detail"],
        }
        if row["prev_hash"] != prev:
            return False, f"chain broken at event {row['id']}: prev_hash mismatch"
        expected = _digest(prev, payload)
        if row["hash"] != expected:
            return False, f"chain broken at event {row['id']}: hash mismatch"
        prev = row["hash"]
        count += 1
    return True, f"{count} events verified"


# --------------------------------------------------------------------------- #
# agents
# --------------------------------------------------------------------------- #

def register_agent(
    conn: sqlite3.Connection,
    *,
    agent_id: str,
    name: str | None = None,
    role: str = "builder",
    harness: str = "unknown",
    model: str = "unknown",
    session_id: str | None = None,
    parent_id: str | None = None,
    task: str | None = None,
    pid: int | None = None,
    cwd: str | None = None,
    meta: Any = None,
) -> dict[str, Any]:
    ts = now()
    host = socket.gethostname()
    meta_text = _canonical(meta) if isinstance(meta, dict) else (meta or "{}")
    existing = conn.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
    if existing:
        conn.execute(
            """UPDATE agents SET name=COALESCE(?,name), role=?, harness=?, model=?,
               session_id=COALESCE(?,session_id), parent_id=COALESCE(?,parent_id),
               task=COALESCE(?,task), status='working', pid=?, host=?, cwd=?,
               last_seen=?, meta=? WHERE id=?""",
            (name, role, harness, model, session_id, parent_id, task, pid, host,
             cwd, ts, meta_text, agent_id),
        )
    else:
        conn.execute(
            """INSERT INTO agents
               (id,name,role,harness,model,session_id,parent_id,task,status,pid,
                host,cwd,started_at,last_seen,meta)
               VALUES (?,?,?,?,?,?,?,?, 'working', ?,?,?,?,?,?)""",
            (agent_id, name or agent_id, role, harness, model, session_id,
             parent_id, task, pid, host, cwd, ts, ts, meta_text),
        )
    conn.commit()
    add_event(conn, kind="agent.join", agent_id=agent_id, session_id=session_id,
              action="register", target=role, status="working",
              detail={"name": name, "harness": harness, "model": model,
                      "parent_id": parent_id, "task": task})
    return get_agent(conn, agent_id) or {}


def update_agent(conn: sqlite3.Connection, agent_id: str, **fields: Any) -> dict[str, Any] | None:
    allowed = {"name", "role", "harness", "model", "session_id", "parent_id",
               "task", "status", "pid", "cwd", "meta"}
    sets, vals = [], []
    for key, value in fields.items():
        if key not in allowed or value is None:
            continue
        if key == "meta" and not isinstance(value, str):
            value = _canonical(value)
        sets.append(f"{key}=?")
        vals.append(value)
    sets.append("last_seen=?")
    vals.append(now())
    vals.append(agent_id)
    conn.execute(f"UPDATE agents SET {', '.join(sets)} WHERE id=?", vals)
    conn.commit()
    return get_agent(conn, agent_id)


def get_agent(conn: sqlite3.Connection, agent_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
    return _agent_dict(row) if row else None


def _agent_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    try:
        d["meta"] = json.loads(d.get("meta") or "{}")
    except json.JSONDecodeError:
        pass
    age = now() - float(d.get("last_seen") or 0)
    d["age_seconds"] = round(age, 1)
    declared = (d.get("status") or "").lower()
    if declared in {"working", "busy", "active", "running"}:
        if age > OFFLINE_AFTER:
            d["effective_status"] = "offline"
        elif age > STALL_AFTER:
            d["effective_status"] = "stalled"
        else:
            d["effective_status"] = "working"
    else:
        d["effective_status"] = declared or "unknown"
    return d


def list_agents(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM agents ORDER BY started_at ASC").fetchall()
    return [_agent_dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# reads
# --------------------------------------------------------------------------- #

def recent_events(conn: sqlite3.Connection, limit: int = 200,
                  agent_id: str | None = None, kinds: Iterable[str] | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM events"
    where, vals = [], []
    if agent_id:
        where.append("agent_id=?")
        vals.append(agent_id)
    if kinds:
        kinds = list(kinds)
        where.append("kind IN (" + ",".join("?" * len(kinds)) + ")")
        vals.extend(kinds)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    vals.append(max(1, min(int(limit), 2000)))
    rows = conn.execute(sql, vals).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("detail"):
            try:
                d["detail"] = json.loads(d["detail"])
            except (json.JSONDecodeError, TypeError):
                pass
        out.append(d)
    return out


def file_changes(conn: sqlite3.Connection, limit: int = 100) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT target, MAX(ts) AS ts, COUNT(*) AS hits,
                  (SELECT agent_id FROM events e2
                   WHERE e2.kind='file.edit' AND e2.target=e.target
                   ORDER BY id DESC LIMIT 1) AS last_agent
           FROM events e WHERE kind='file.edit' GROUP BY target
           ORDER BY ts DESC LIMIT ?""", (limit,)).fetchall()
    return [dict(r) for r in rows]


def state(conn: sqlite3.Connection, root: str | os.PathLike[str] | None = None,
          project: dict[str, Any] | None = None) -> dict[str, Any]:
    ok, message = verify_chain(conn)
    agents = list_agents(conn)
    counts = {r["kind"]: r["n"] for r in conn.execute(
        "SELECT kind, COUNT(*) n FROM events GROUP BY kind")}
    return {
        "generated_at": now(),
        "repo": str(root or ROOT),
        "project": project,
        "agents": agents,
        "counts": counts,
        "events": recent_events(conn, 100),
        "files": file_changes(conn),
        "todos": list_todos(conn),
        "integrity": {"ok": ok, "message": message},
        "stalled": [a["id"] for a in agents if a["effective_status"] == "stalled"],
        "offline": [a["id"] for a in agents if a["effective_status"] == "offline"],
    }


# --------------------------------------------------------------------------- #
# todos — TODO.md is the source of truth, the table is a queryable mirror
# --------------------------------------------------------------------------- #

TODO_LINE = re.compile(r"^\s*-\s*\[(?P<box>[ x~!])\]\s*(?P<rest>.+)$")
TODO_ID = re.compile(r"\*\*(?P<id>[A-Z]\d*-\d+)(?P<after>(?:\*\*|\s).*)?")
BOX = {" ": "queued", "~": "active", "x": "done", "!": "blocked"}


def _split_todo(rest: str) -> tuple[str, str, str] | None:
    """Accept both `**T-001** **Title** — note` and `**T-001 Title** — note`."""
    m = TODO_ID.match(rest.strip())
    if not m:
        return None
    todo_id = m.group("id")
    after = (m.group("after") or "")
    # `after` still contains the closing `**` of the id when the title follows.
    after = after.lstrip("*").strip()
    title, note = after, ""
    if title.startswith("**"):
        end = title.find("**", 2)
        if end != -1:
            note = title[end + 2:].lstrip(" —-–:").strip()
            title = title[2:end].strip()
    else:
        # `**T-001 Title** — note` form: title is up to the closing marker.
        end = title.find("**")
        if end != -1:
            note = title[end + 2:].lstrip(" —-–:").strip()
            title = title[:end].strip()
    # `**T-001 Title**` already lost its leading `**`; clean leftover separators.
    title = title.strip(" *—–-:")
    return todo_id, title or todo_id, note


def _todo_path(path: str | os.PathLike[str] | None = None) -> Path:
    """TODO.md of the active project; the old repo file is the fallback."""
    if path:
        return Path(path)
    try:
        from . import home as _home
        found = _home.todo_for()
        if found:
            return found
    except Exception:  # noqa: BLE001 - a missing registry must not break reads
        pass
    return ROOT / "TODO.md"


def parse_todo_file(path: str | os.PathLike[str] | None = None) -> list[dict[str, Any]]:
    p = _todo_path(path)
    if not p.exists():
        return []
    items: list[dict[str, Any]] = []
    for raw in p.read_text(encoding="utf-8").splitlines():
        m = TODO_LINE.match(raw)
        if not m:
            continue
        parsed = _split_todo(m.group("rest"))
        if not parsed:
            continue
        todo_id, title, note = parsed
        items.append({
            "id": todo_id,
            "title": title,
            "note": note,
            "status": BOX[m.group("box")],
            "priority": todo_id.split("-")[0],
        })
    return items


def sync_todos(conn: sqlite3.Connection, path: str | os.PathLike[str] | None = None) -> list[dict[str, Any]]:
    items = parse_todo_file(path)
    ts = now()
    for item in items:
        conn.execute(
            """INSERT INTO todos (id,title,status,priority,note,updated)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 title=excluded.title, status=excluded.status,
                 priority=excluded.priority, note=excluded.note, updated=excluded.updated""",
            (item["id"], item["title"], item["status"], item["priority"],
             item["note"], ts),
        )
    conn.commit()
    return items


def list_todos(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM todos ORDER BY priority ASC, id ASC").fetchall()
    return [dict(r) for r in rows]


def set_todo_status(conn: sqlite3.Connection, todo_id: str, status: str) -> dict[str, Any] | None:
    """Update TODO.md itself, then the mirror. Keeps the file editable and honest."""
    if status not in {"queued", "active", "done", "blocked"}:
        raise ValueError("status must be queued|active|done|blocked")
    box = {"queued": " ", "active": "~", "done": "x", "blocked": "!"}[status]
    p = _todo_path()
    if p.exists():
        lines = p.read_text(encoding="utf-8").splitlines()
        needle = f"**{todo_id}"
        for i, line in enumerate(lines):
            if needle in line and re.match(r"^\s*-\s*\[", line):
                lines[i] = re.sub(r"\[[ x~!]\]", f"[{box}]", line, count=1)
                break
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    conn.execute("UPDATE todos SET status=?, updated=? WHERE id=?",
                 (status, now(), todo_id))
    conn.commit()
    row = conn.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone()
    return dict(row) if row else None


def add_todo(conn: sqlite3.Connection, title: str, priority: str = "P9") -> dict[str, Any]:
    p = _todo_path()
    items = parse_todo_file(p)
    prefix = priority.upper() if re.fullmatch(r"P\d+", priority.upper()) else "P9"
    used = {int(i["id"].split("-")[1]) for i in items if i["id"].startswith(prefix + "-")}
    number = 1
    while number in used:
        number += 1
    todo_id = f"{prefix}-{number:03d}"
    block = f"\n- [ ] **{todo_id}** **{title.strip()}**\n"
    # append under "## P4" if present, else at the end
    text = p.read_text(encoding="utf-8") if p.exists() else "# TODO\n"
    marker = "## P4 —"
    if marker in text:
        idx = text.index(marker)
        end = text.find("\n## ", idx + 1)
        insert_at = len(text) if end == -1 else end
        text = text[:insert_at].rstrip("\n") + block + text[insert_at:]
    else:
        text = text.rstrip("\n") + block
    p.write_text(text, encoding="utf-8")
    sync_todos(conn, p)
    return {"id": todo_id, "title": title, "status": "queued", "priority": prefix}


# --------------------------------------------------------------------------- #
# demo / seed
# --------------------------------------------------------------------------- #

def seed_demo(conn: sqlite3.Connection) -> None:
    """Create a clearly-labelled demo race so the dashboard is never blank.

    Demo agents are prefixed ``demo-`` and can be removed with
    ``python -m glasshouse.cli seed --clear``.
    """
    if conn.execute("SELECT 1 FROM agents WHERE id LIKE 'demo-%' LIMIT 1").fetchone():
        return
    base = now() - 240
    demo = [
        ("demo-builder", "Builder", "builder", "demo-harness", "model-a",
         "Add the project picker", "working", 12),
        ("demo-critic", "Critic", "critic", "demo-harness", "model-b",
         "Review the pull request", "working", 41),
        ("demo-scout", "Scout", "scout", "demo-harness", "model-c",
         "Index the documentation", "stalled", 245),
    ]
    for agent_id, name, role, harness, model, task, status, age in demo:
        conn.execute(
            """INSERT OR REPLACE INTO agents
               (id,name,role,harness,model,task,status,host,started_at,last_seen)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (agent_id, name, role, harness, model, task, status,
             socket.gethostname(), base, now() - age),
        )
    for i, (action, target) in enumerate([
        ("read", "README.md"),
        ("edit", "src/app.py"),
        ("test", "pytest -q"),
        ("tool", "fetch https://example.com"),
        ("edit", "docs/design.md"),
    ]):
        add_event(conn, kind="file.edit" if action == "edit" else "tool.call",
                  agent_id="demo-builder", action=action, target=target,
                  status="ok", ts=base + i * 8)
    add_event(conn, kind="status", agent_id="demo-critic", action="stalled",
              target="no heartbeat", status="stalled", ts=now() - 245)
    conn.commit()


def clear_demo(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM agents WHERE id LIKE 'demo-%'")
    conn.execute("DELETE FROM events WHERE agent_id LIKE 'demo-%'")
    conn.commit()
