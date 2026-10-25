"""Glasshouse HTTP server — stdlib only, bound to loopback.

Two audiences, one event store:

  /            admin dashboard (owner: total transparency)
  /ai          AI race dashboard (agents: who is where, what is next)
  /api/state   one JSON snapshot for any program
  /api/stream  Server-Sent Events, live

Security note: this listens on 127.0.0.1 by default and must not be exposed to a
network. It shows prompts, paths, and command lines. If GLASSHOUSE_TOKEN is set,
every /api request must send `X-Glasshouse-Token` (or `?token=`).
"""

from __future__ import annotations

import json
import mimetypes
import os
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, home, store

STATIC = Path(__file__).resolve().parent / "static"
TOKEN = os.environ.get("GLASSHOUSE_TOKEN", "").strip()

_subscribers: list[queue.Queue] = []
_sub_lock = threading.Lock()


def _broadcast(event: dict) -> None:
    with _sub_lock:
        dead = []
        for q in _subscribers:
            try:
                q.put_nowait(event)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _subscribers.remove(q)


def _tail_loop(stop: threading.Event) -> None:
    """Poll every registered project and fan new rows out to SSE clients.

    Each event carries its ``project`` id, so a browser tab watching one
    project can ignore the others while a single server watches them all.
    """
    last: dict[str, int] = {}
    conns: dict[str, object] = {}
    while not stop.is_set():
        try:
            for entry in home.list_projects():
                pid = entry["id"]
                try:
                    conn = conns.get(pid)
                    if conn is None:
                        conn = store.connect(entry["db"])
                        conns[pid] = conn
                        last[pid] = conn.execute(
                            "SELECT COALESCE(MAX(id),0) AS m FROM events").fetchone()["m"]
                    rows = conn.execute(
                        "SELECT * FROM events WHERE id > ? ORDER BY id ASC",
                        (last[pid],)).fetchall()
                    for r in rows:
                        d = dict(r)
                        if d.get("detail"):
                            try:
                                d["detail"] = json.loads(d["detail"])
                            except (json.JSONDecodeError, TypeError):
                                pass
                        d["project"] = pid
                        _broadcast(d)
                        last[pid] = d["id"]
                except Exception:  # one broken project must not stop the stream
                    conns.pop(pid, None)
        except Exception:
            pass
        stop.wait(0.8)
    for conn in conns.values():
        try:
            conn.close()
        except Exception:
            pass


class Handler(BaseHTTPRequestHandler):
    server_version = f"Glasshouse/{__version__}"
    protocol_version = "HTTP/1.1"

    # ---------------------------------------------------------------- helpers
    def _authorised(self, query: dict) -> bool:
        if not TOKEN:
            return True
        sent = self.headers.get("X-Glasshouse-Token") or (query.get("token") or [""])[0]
        return sent == TOKEN

    def _json(self, data, code: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"invalid JSON body: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def _project(self, query: dict, body: dict | None = None):
        """Resolve ?project=, else the session's project, else the last opened.

        ``?project=@none`` means the owner deliberately closed the project. That
        has to win over the "last opened" fallback, otherwise there is no way to
        leave a project without stopping the server.
        """
        ref = (query.get("project") or [None])[0]
        if ref == "@none":
            return None
        if not ref and body:
            sessions = getattr(self.server, "session_project", None) or {}
            ref = sessions.get(str(body.get("session_id") or ""))
        try:
            # create=False: looking at the dashboard must not re-register a
            # folder behind the owner's back. Otherwise removing the last
            # project silently adds the server's own working directory back.
            return home.resolve(ref, create=False)
        except FileNotFoundError:
            return None

    def _remember(self, session_id, entry) -> None:
        if not session_id or not entry:
            return
        sessions = getattr(self.server, "session_project", None)
        if sessions is None:
            sessions = self.server.session_project = {}
        sessions[str(session_id)] = entry["id"]

    def _folders(self, query: dict) -> None:
        """List the folders inside a directory, so the UI can browse instead of
        asking the owner to type a path from memory.

        Loopback only, and it lists folders, never file contents: it is a folder
        picker, not a file reader.
        """
        raw = (query.get("path") or [""])[0].strip()
        target = Path(raw).expanduser() if raw else Path.home()
        try:
            target = target.resolve()
        except OSError as exc:
            self._json({"error": f"cannot resolve that path: {exc}"}, 400)
            return
        if not target.is_dir():
            self._json({"error": f"not a folder: {target}"}, 400)
            return

        entries: list[dict] = []
        try:
            with os.scandir(target) as it:
                for item in it:
                    try:
                        if not item.is_dir(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    entries.append({"name": item.name, "path": os.path.join(str(target), item.name)})
        except PermissionError:
            self._json({"error": f"cannot read {target} — permission denied"}, 403)
            return
        except OSError as exc:
            self._json({"error": f"cannot read {target}: {exc}"}, 400)
            return

        entries.sort(key=lambda e: (e["name"].startswith("."), e["name"].lower()))
        known = home.get_project(str(target))
        parent = target.parent
        self._json({
            "path": str(target),
            "display": home.display_path(target),
            "parent": None if parent == target else str(parent),
            "parent_display": None if parent == target else home.display_path(parent),
            "home": home.display_path(Path.home()),
            "dirs": entries[:400],
            "truncated": len(entries) > 400,
            "registered": known["id"] if known else None,
        })

    def _file(self, rel: str) -> None:
        target = (STATIC / rel).resolve()
        if not str(target).startswith(str(STATIC.resolve())) or not target.is_file():
            self.send_error(404, "not found")
            return
        body = target.read_bytes()
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    # -------------------------------------------------------------------- GET
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if not self._authorised(query):
            self._json({"error": "unauthorised"}, 401)
            return

        if path in {"/", "/admin", "/index.html"}:
            self._file("index.html")
            return
        if path in {"/ai", "/ai.html"}:
            self._file("ai.html")
            return
        if path in {"/projects", "/projects.html"}:
            self._file("projects.html")
            return
        if path in {"/logo.svg", "/favicon.svg"}:
            self._file("logo.svg")
            return
        if path.startswith("/static/"):
            self._file(path[len("/static/"):])
            return
        if path == "/healthz":
            self._json({"ok": True, "version": __version__, "token": bool(TOKEN),
                        "projects": len(home.list_projects())})
            return
        if path == "/api/projects":
            active = self._project(query)
            projects = [{**p, "display": home.display_path(p["path"])}
                        for p in home.list_projects()]
            if active:
                active = {**active, "display": home.display_path(active["path"])}
            self._json({"projects": projects, "active": active,
                        "home": str(home.home())})
            return
        if path == "/api/folders":
            self._folders(query)
            return
        if path == "/api/state":
            entry = self._project(query)
            if entry is None:
                # Say "nothing is selected" without hiding the list: the picker
                # is the way back in, so it has to keep working in this state.
                projects = [{**p, "display": home.display_path(p["path"])}
                            for p in home.list_projects()]
                self._json({"error": "no project selected", "project": None,
                            "agents": [], "events": [], "todos": [], "files": [],
                            "counts": {}, "projects": projects,
                            "integrity": {"ok": True, "message": "no project selected"},
                            "generated_at": time.time()})
                return
            conn = store.connect(entry["db"])
            store.sync_todos(conn, home.todo_for(entry["id"]))
            data = store.state(conn, root=entry["path"], project=entry)
            data["projects"] = home.list_projects()
            self._json(data)
            conn.close()
            return
        if path == "/api/events":
            entry = self._project(query)
            if entry is None:
                self._json({"events": []})
                return
            conn = store.connect(entry["db"])
            limit = int((query.get("limit") or ["200"])[0])
            agent = (query.get("agent") or [None])[0]
            self._json({"events": store.recent_events(conn, limit, agent)})
            conn.close()
            return
        if path == "/api/todos":
            entry = self._project(query)
            if entry is None:
                self._json({"todos": []})
                return
            conn = store.connect(entry["db"])
            self._json({"todos": store.sync_todos(conn, home.todo_for(entry["id"]))})
            conn.close()
            return
        if path == "/api/verify":
            entry = self._project(query)
            if entry is None:
                self._json({"ok": True, "message": "no project registered"})
                return
            conn = store.connect(entry["db"])
            ok, message = store.verify_chain(conn)
            conn.close()
            self._json({"ok": ok, "message": message, "project": entry["id"]})
            return
        if path == "/api/stream":
            self._stream()
            return
        self.send_error(404, "not found")

    # ------------------------------------------------------------------- POST
    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if not self._authorised(query):
            self._json({"error": "unauthorised"}, 401)
            return
        try:
            body = self._read_json()
        except ValueError as exc:
            self._json({"error": str(exc)}, 400)
            return
        try:
            if parsed.path == "/api/projects":
                # Two shapes on one route: add {"path": ...} or remove
                # {"remove": "<id>"}. Removal keeps the history unless the
                # caller also says {"delete_history": true}.
                if body.get("remove"):
                    try:
                        result = home.remove_project(
                            str(body["remove"]),
                            delete_history=bool(body.get("delete_history")),
                        )
                    except KeyError as exc:
                        self._json({"error": exc.args[0] if exc.args else str(exc)}, 404)
                        return
                    self._json({"ok": True, **result,
                                "projects": home.list_projects()})
                    return
                raw = body.get("path")
                if not raw:
                    self._json({"error": "path is required"}, 400)
                    return
                try:
                    entry = home.add_project(str(raw), body.get("name"))
                except (FileNotFoundError, NotADirectoryError) as exc:
                    self._json({"error": str(exc)}, 400)
                    return
                self._json({"ok": True, "project": entry,
                            "projects": home.list_projects()})
                return
        except Exception as exc:  # noqa: BLE001
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)
            return

        entry = None
        if parsed.path == "/api/register":
            cwd = body.get("cwd")
            if not (query.get("project") or [None])[0] and cwd:
                try:
                    entry = home.add_project(str(cwd))
                except (FileNotFoundError, NotADirectoryError):
                    entry = None
        entry = entry or self._project(query, body)
        if entry is None:
            self._json({"error": "no project registered — open one first"}, 400)
            return
        conn = store.connect(entry["db"])
        try:
            if parsed.path == "/api/register":
                agent = store.register_agent(
                    conn,
                    agent_id=str(body.get("agent_id") or body.get("id") or ""),
                    name=body.get("name"),
                    role=body.get("role") or "builder",
                    harness=body.get("harness") or "unknown",
                    model=body.get("model") or "unknown",
                    session_id=body.get("session_id"),
                    parent_id=body.get("parent_id"),
                    task=body.get("task"),
                    pid=body.get("pid"),
                    cwd=body.get("cwd"),
                    meta=body.get("meta"),
                )
                self._remember(body.get("session_id"), entry)
                self._json({"ok": True, "agent": agent})
                return
            if parsed.path == "/api/heartbeat":
                agent_id = str(body.get("agent_id") or body.get("id") or "")
                fields = {k: body.get(k) for k in ("task", "status", "model")}
                agent = store.update_agent(conn, agent_id, **fields)
                if agent:
                    store.add_event(conn, kind="heartbeat", agent_id=agent_id,
                                    session_id=body.get("session_id"),
                                    status=agent.get("status"), detail=body.get("detail"))
                self._json({"ok": bool(agent), "agent": agent})
                return
            if parsed.path == "/api/event":
                event = store.add_event(
                    conn,
                    kind=str(body.get("kind") or "note"),
                    agent_id=body.get("agent_id"),
                    session_id=body.get("session_id"),
                    action=body.get("action"),
                    target=body.get("target"),
                    status=body.get("status"),
                    duration_ms=body.get("duration_ms"),
                    detail=body.get("detail"),
                )
                if body.get("agent_id"):
                    store.update_agent(conn, body["agent_id"],
                                       task=body.get("task") or None,
                                       status=body.get("agent_status") or None)
                self._json({"ok": True, "event": event})
                return
            if parsed.path == "/api/status":
                agent = store.update_agent(
                    conn, str(body.get("agent_id") or ""),
                    status=body.get("status"),
                    task=body.get("task"),
                )
                store.add_event(conn, kind="status",
                                agent_id=body.get("agent_id"),
                                action=body.get("status"),
                                target=body.get("task"),
                                status=body.get("status"),
                                detail=body.get("detail"))
                self._json({"ok": bool(agent), "agent": agent})
                return
            if parsed.path == "/api/todos":
                action = body.get("action") or "set"
                if action == "add":
                    item = store.add_todo(conn, str(body.get("title") or "untitled"),
                                          str(body.get("priority") or "P9"))
                else:
                    item = store.set_todo_status(conn, str(body.get("id") or ""),
                                                 str(body.get("status") or "queued"))
                self._json({"ok": bool(item), "todo": item})
                return
            self._json({"error": "unknown endpoint"}, 404)
        except Exception as exc:  # never crash the dashboard on bad input
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)
        finally:
            conn.close()

    # ------------------------------------------------------------------- SSE
    def _stream(self) -> None:
        q: queue.Queue = queue.Queue(maxsize=500)
        with _sub_lock:
            _subscribers.append(q)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    event = q.get(timeout=15)
                    payload = json.dumps(event, ensure_ascii=False)
                    self.wfile.write(f"event: message\ndata: {payload}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with _sub_lock:
                if q in _subscribers:
                    _subscribers.remove(q)

    def log_message(self, fmt: str, *args) -> None:  # keep stdout readable
        if os.environ.get("GLASSHOUSE_VERBOSE"):
            super().log_message(fmt, *args)


class Server:
    def __init__(self, host: str = "127.0.0.1", port: int = 7077):
        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.httpd.session_project = {}
        self.httpd.daemon_threads = True
        self._stop = threading.Event()
        self._tail = threading.Thread(target=_tail_loop, args=(self._stop,), daemon=True)

    def serve_forever(self) -> None:
        self._tail.start()
        host, port = self.httpd.server_address[:2]
        print(f"Glasshouse {__version__} → http://{host}:{port}/  (AI view: /ai)")
        try:
            self.httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self._stop.set()
            self.httpd.server_close()


def main(host: str = "127.0.0.1", port: int = 7077) -> None:
    Server(host, port).serve_forever()
