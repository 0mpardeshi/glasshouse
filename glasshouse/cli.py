"""Glasshouse CLI — the one command every agent can call.

    python -m glasshouse.cli serve
    python -m glasshouse.cli register --name builder --model deepseek-v4.1-flash-free --task "..."
    python -m glasshouse.cli event --kind tool.call --action webfetch --target https://... --status ok
    python -m glasshouse.cli heartbeat --task "patching ab/cli.py"
    python -m glasshouse.cli pause --reason "waiting for owner"
    python -m glasshouse.cli status --json

``bin/glass`` is a thin wrapper around this module. The CLI is intentionally
forgiving: telemetry must never block real work.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, home, store
from .server import Server

ROOT = store.ROOT
ID_FILE = home.agent_id_path()


# --------------------------------------------------------------------------- #
# identity
# --------------------------------------------------------------------------- #

def _slug(text: str) -> str:
    keep = [c.lower() if c.isalnum() else "-" for c in text]
    return "".join(keep).strip("-")[:40] or "agent"


def _migrate_identity() -> None:
    legacy = ROOT / ".glasshouse" / "agent-id"
    try:
        if legacy.is_file() and not ID_FILE.exists():
            ID_FILE.parent.mkdir(parents=True, exist_ok=True)
            ID_FILE.write_text(legacy.read_text(encoding="utf-8"), encoding="utf-8")
    except OSError:
        pass


def agent_id(explicit: str | None = None, name: str | None = None) -> str:
    if explicit:
        return explicit
    env = os.environ.get("GLASSHOUSE_AGENT_ID")
    if env:
        return env
    _migrate_identity()
    if ID_FILE.exists():
        value = ID_FILE.read_text(encoding="utf-8").strip()
        if value:
            return value
    new = f"{_slug(name or os.environ.get('GLASSHOUSE_AGENT_NAME', 'agent'))}-{secrets.token_hex(3)}"
    _remember(new)
    return new


def _remember(value: str) -> None:
    try:
        ID_FILE.parent.mkdir(parents=True, exist_ok=True)
        ID_FILE.write_text(value + "\n", encoding="utf-8")
    except OSError:
        pass


def _conn():
    return store.connect()


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #

def cmd_serve(args) -> int:
    Server(args.host, args.port).serve_forever()
    return 0


def cmd_daemon(args) -> int:
    """Start the server in the background (used by autostart) and return."""
    if home.start_server(args.port):
        print(f"Glasshouse listening on {home.server_url(args.port)}")
        return 0
    print(f"could not start Glasshouse; see {home.home() / 'server.log'}", file=sys.stderr)
    return 1


def cmd_projects(args) -> int:
    projects = home.list_projects()
    if args.json:
        print(json.dumps({"projects": projects, "home": str(home.home())}, indent=2))
        return 0
    if not projects:
        print("no projects yet — add one with: glass add .")
        return 0
    print(f"Glasshouse home: {home.home()}")
    for p in projects:
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(p.get("last_opened") or 0))
        print(f"  {p['id']:<32} {stamp}  {p['path']}")
    return 0


def cmd_add(args) -> int:
    entry = home.add_project(args.path or Path.cwd(), args.name)
    print(json.dumps(entry, indent=2) if args.json else f"added {entry['id']} → {entry['path']}")
    return 0


def cmd_remove(args) -> int:
    entry = home.get_project(args.project)
    if not entry:
        print(f"no such project: {args.project}", file=sys.stderr)
        return 1
    result = home.remove_project(entry["id"], delete_history=bool(args.delete))
    print(f"removed {entry['id']} from the registry (history kept)")
    if args.delete:
        if result["deleted"]:
            print(f"deleted {result['deleted']}")
        else:
            print("refusing to delete a database outside the Glasshouse home", file=sys.stderr)
            return 1
    return 0


def cmd_open(args) -> int:
    if args.path:
        entry = home.add_project(args.path, args.name)
    else:
        entry = home.resolve()
    home.touch(entry)
    if not home.start_server(args.port):
        print(f"could not start the server; see {home.home() / 'server.log'}", file=sys.stderr)
        return 1
    url = f"{home.server_url(args.port)}/?project={entry['id']}"
    print(url)
    if not args.no_browser:
        home.open_url(url)
    return 0


def cmd_menu(args) -> int:
    """Open the project picker — what the desktop icon runs."""
    if not home.start_server(args.port):
        print(f"could not start the server; see {home.home() / 'server.log'}", file=sys.stderr)
        return 1
    url = f"{home.server_url(args.port)}/projects"
    print(url)
    if not args.no_browser:
        home.open_url(url)
    return 0


def cmd_doctor(args) -> int:
    result = home.doctor(args.port, start=args.start)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Glasshouse {__version__} — self-check")
        for check in result["checks"]:
            print(f"  [{'PASS' if check['ok'] else 'FAIL'}] {check['name']:<12} {check['detail']}")
        print(f"RESULT: {'PASS' if result['ok'] else 'FAIL'}  ({result['url']})")
        if not result["ok"]:
            print("Hint: glass setup --start   # writes the launcher and starts the server")
    return 0 if result["ok"] else 1


def cmd_setup(args) -> int:
    if args.remove:
        removed = home.remove_setup()
        for path in removed:
            print(f"removed {path}")
        return 0
    result = home.setup(autostart=not args.no_autostart)
    for path in result["written"]:
        print(f"wrote {path}")
    started = args.start and home.start_server(args.port)
    print(f"home: {result['home']}")
    print(f"launcher: {result['command'][0]} menu")
    if started:
        print(f"dashboard: {home.server_url(args.port)}")
    elif args.start:
        print(f"server did not start; see {home.home() / 'server.log'}", file=sys.stderr)
        return 1
    return 0


def cmd_export(args) -> int:
    result = home.export_public(args.out, force=args.force)
    print(f"exported {len(result['copied'])} files → {result['target']}")
    for rel in result["missing"]:
        print(f"  [skip] {rel} (not in this checkout)")
    if result["findings"]:
        print(f"PRIVACY CHECK FAILED — {len(result['findings'])} finding(s):", file=sys.stderr)
        for f in result["findings"]:
            print(f"  {f['file']}:{f['line']}  {f['kind']}: {f['text']}", file=sys.stderr)
        print("Nothing was published. Fix the text or add an allow-list rule.", file=sys.stderr)
        return 1
    print("privacy: PASS — no personal paths, hostnames, tokens, or private project names")
    return 0


def cmd_register(args) -> int:
    conn = _conn()
    aid = agent_id(args.agent, args.name)
    _remember(aid)
    agent = store.register_agent(
        conn, agent_id=aid, name=args.name, role=args.role,
        harness=args.harness, model=args.model, session_id=args.session,
        parent_id=args.parent, task=args.task, pid=os.getpid(),
        cwd=str(Path.cwd()), meta={"argv": sys.argv},
    )
    print(json.dumps(agent, indent=2) if args.json else f"registered {aid}")
    return 0


def cmd_event(args) -> int:
    conn = _conn()
    aid = agent_id(args.agent, args.name)
    event = store.add_event(
        conn, kind=args.kind, agent_id=aid, session_id=args.session,
        action=args.action, target=args.target, status=args.status,
        duration_ms=args.duration_ms,
        detail=_parse_detail(args.detail),
    )
    if args.task:
        store.update_agent(conn, aid, task=args.task)
    print(json.dumps(event, indent=2) if args.json else f"event #{event['id']} {args.kind}")
    return 0


def cmd_heartbeat(args) -> int:
    conn = _conn()
    aid = agent_id(args.agent, args.name)
    store.update_agent(conn, aid, task=args.task, status=args.status, model=args.model)
    store.add_event(conn, kind="heartbeat", agent_id=aid,
                    session_id=args.session, status=args.status, detail=args.detail)
    print(json.dumps({"ok": True, "agent_id": aid}) if args.json else f"beat {aid}")
    return 0


def cmd_status(args) -> int:
    conn = _conn()
    store.sync_todos(conn)
    data = store.state(conn)
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(f"Glasshouse {__version__} — {len(data['agents'])} agents, "
              f"{data['integrity']['message']}")
        for a in data["agents"]:
            print(f"  [{a['effective_status']:8}] {a['name']:<12} {a['role']:<8} "
                  f"{a['model']:<30} {a['task'] or ''}")
        for t in data["todos"]:
            if t["status"] != "done":
                print(f"  {t['status']:8} {t['id']:<8} {t['title']}")
    return 0


def cmd_verify(args) -> int:
    conn = _conn()
    ok, message = store.verify_chain(conn)
    print(json.dumps({"ok": ok, "message": message}) if args.json else message)
    return 0 if ok else 1


def cmd_seed(args) -> int:
    conn = _conn()
    if args.clear:
        store.clear_demo(conn)
        print("demo data cleared")
    else:
        store.seed_demo(conn)
        print("demo race seeded (clearly labelled demo-*)")
    return 0


def cmd_todo(args) -> int:
    conn = _conn()
    if args.action == "list":
        print(json.dumps(store.sync_todos(conn), indent=2) if args.json
              else "\n".join(f"[{t['status']:7}] {t['id']:<8} {t['title']}"
                             for t in store.sync_todos(conn)))
    elif args.action == "add":
        item = store.add_todo(conn, args.title, args.priority)
        print(json.dumps(item) if args.json else f"added {item['id']}")
    else:
        item = store.set_todo_status(conn, args.todo_id or "", args.status)
        print(json.dumps(item) if args.json else f"{args.todo_id} -> {args.status}")
    return 0


def cmd_install_hook(args) -> int:
    """A git post-commit hook so every commit is visible in the dashboard."""
    hook = ROOT / ".git" / "hooks" / "post-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(
        "#!/bin/sh\n"
        "# installed by glasshouse: make every commit visible to the owner\n"
        'cd "$(git rev-parse --show-toplevel)" || exit 0\n'
        'SHA=$(git rev-parse --short HEAD)\n'
        'MSG=$(git log -1 --pretty=%s)\n'
        f'"{sys.executable}" -m glasshouse.cli event --kind git.commit '
        '--action commit --target "$SHA" --status ok --detail "$MSG" >/dev/null 2>&1 || true\n',
        encoding="utf-8",
    )
    hook.chmod(0o755)
    print(f"installed {hook}")
    return 0


def _parse_detail(value: str | None):
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="glass", description="Glasshouse agent telemetry + dashboard")
    p.add_argument("--version", action="version", version=f"glasshouse {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("serve", help="start the local dashboard")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=7077)
    s.set_defaults(func=cmd_serve)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--agent", "--id", dest="agent", default=None)
    common.add_argument("--name", default=None)
    common.add_argument("--session", default=None)
    common.add_argument("--json", action="store_true")

    s = sub.add_parser("register", parents=[common], help="register this agent")
    s.add_argument("--role", default="builder")
    s.add_argument("--harness", default="opencode")
    s.add_argument("--model", default=os.environ.get("GLASSHOUSE_MODEL", "unknown"))
    s.add_argument("--parent", default=None)
    s.add_argument("--task", default=None)
    s.set_defaults(func=cmd_register)

    s = sub.add_parser("event", parents=[common], help="append one event")
    s.add_argument("--kind", default="note",
                   help="agent.join|tool.call|file.edit|test|status|git.commit|note")
    s.add_argument("--action", default=None)
    s.add_argument("--target", default=None)
    s.add_argument("--status", default="ok")
    s.add_argument("--duration-ms", type=int, default=None)
    s.add_argument("--detail", default=None)
    s.add_argument("--task", default=None)
    s.set_defaults(func=cmd_event)

    s = sub.add_parser("heartbeat", parents=[common], help="prove the agent is alive")
    s.add_argument("--task", default=None)
    s.add_argument("--status", default="working")
    s.add_argument("--model", default=None)
    s.add_argument("--detail", default=None)
    s.set_defaults(func=cmd_heartbeat)

    for name, status, help_text in (
        ("pause", "paused", "mark paused (owner action or waiting)"),
        ("resume", "working", "mark working again"),
        ("done", "done", "mark finished"),
    ):
        s = sub.add_parser(name, parents=[common], help=help_text)
        s.add_argument("--task", default=None)
        s.add_argument("--reason", default=None)
        s.set_defaults(func=cmd_heartbeat, status=status, detail=None)

    s = sub.add_parser("status", parents=[common], help="show the current state")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("verify", parents=[common], help="verify the hash chain")
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("seed", parents=[common], help="seed/clear the labelled demo race")
    s.add_argument("--clear", action="store_true")
    s.set_defaults(func=cmd_seed)

    s = sub.add_parser("todo", parents=[common], help="list/add/tick TODO.md")
    s.add_argument("action", choices=["list", "add", "set"])
    s.add_argument("--title", default="new task")
    s.add_argument("--priority", default="P9")
    s.add_argument("--todo", dest="todo_id", default=None)
    s.add_argument("--status", default="done")
    s.set_defaults(func=cmd_todo)

    s = sub.add_parser("install-hook", parents=[common], help="record every git commit")
    s.set_defaults(func=cmd_install_hook)

    s = sub.add_parser("projects", parents=[common], help="list registered project folders")
    s.set_defaults(func=cmd_projects)

    s = sub.add_parser("add", parents=[common], help="register a project folder")
    s.add_argument("path", nargs="?", default=None)
    s.set_defaults(func=cmd_add)

    s = sub.add_parser("remove", parents=[common], help="unregister a project (optionally delete its history)")
    s.add_argument("project")
    s.add_argument("--delete", action="store_true")
    s.set_defaults(func=cmd_remove)

    s = sub.add_parser("open", parents=[common],
                       help="open a project: register, start the server, open the browser")
    s.add_argument("path", nargs="?", default=None)
    s.add_argument("--port", type=int, default=None)
    s.add_argument("--no-browser", action="store_true")
    s.set_defaults(func=cmd_open)

    s = sub.add_parser("menu", parents=[common], help="open the project picker (desktop icon)")
    s.add_argument("--port", type=int, default=None)
    s.add_argument("--no-browser", action="store_true")
    s.set_defaults(func=cmd_menu)

    s = sub.add_parser("daemon", parents=[common], help="start the dashboard in the background")
    s.add_argument("--port", type=int, default=None)
    s.set_defaults(func=cmd_daemon)

    s = sub.add_parser("doctor", parents=[common],
                       help="self-check: home, database, chain, server, admin and AI views")
    s.add_argument("--port", type=int, default=None)
    s.add_argument("--start", action="store_true", help="start the server if it is down")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("setup", parents=[common],
                       help="install the desktop launcher, icon, and login autostart")
    s.add_argument("--start", action="store_true", help="start the dashboard now")
    s.add_argument("--no-autostart", action="store_true")
    s.add_argument("--remove", action="store_true", help="remove the launcher and autostart")
    s.add_argument("--port", type=int, default=None)
    s.set_defaults(func=cmd_setup)

    s = sub.add_parser("export", parents=[common],
                       help="build a privacy-checked standalone copy for publishing")
    s.add_argument("--out", required=True)
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_export)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
