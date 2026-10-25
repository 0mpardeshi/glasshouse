"""Glasshouse home: projects, per-project storage, desktop integration, self-check.

The idea is borrowed from an editor. You do not open "a database"; you open a
**project folder**. Glasshouse remembers that folder, gives it its own event
history, and puts it back on screen next time — so quitting the terminal or
rebooting loses nothing.

Layout (all outside any repository, so a project can be published safely)::

    ~/.local/share/glasshouse/
        projects.json              registry: id, name, path, last_opened
        projects/<id>/glasshouse.db  one append-only history per project
        agent-id                   this machine's agent identity
        server.log                 background server output
        icon.svg                   the icon used by the desktop launcher

Nothing here is required to live inside a git repository, and nothing inside a
git repository is required to be committed. That separation is what keeps
personal data out of a published copy.

Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

APP = "glasshouse"
DEFAULT_PORT = 7077
REGISTRY_VERSION = 1

# Files that must never leave this machine in a public export.
PRIVATE_DIRS = {".git", ".venv", "venv", "data", "memory", "audit", "work-log",
                "__pycache__", ".pytest_cache", ".glasshouse", "node_modules"}
PRIVATE_FILES = {"TODO.md", "PROMPT.md", "review.md", ".env", ".env.local"}
PRIVATE_PATTERNS = [
    (re.compile(r"/home/[A-Za-z0-9._-]+"), "absolute home path"),
    (re.compile(r"/Users/[A-Za-z0-9._-]+"), "absolute home path"),
    (re.compile(r"C:\\\\Users\\\\[A-Za-z0-9._-]+"), "absolute home path"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "email address"),
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "GitHub token"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "GitHub token"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "API key"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS key"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key"),
]

# Names that must not appear in a published copy: the registered project names
# (they describe this machine's private work) plus anything configured. They are
# read from the environment and a local file, never hardcoded here — this module
# ships publicly.
PRIVATE_NAMES_ENV = "GLASSHOUSE_PRIVATE_NAMES"
PRIVATE_NAMES_FILE = "private-names.txt"


def private_names() -> set[str]:
    names: set[str] = set()
    for blob in (os.environ.get(PRIVATE_NAMES_ENV) or "",):
        names.update(n.strip() for n in blob.replace(",", " ").split() if len(n.strip()) > 3)
    config = home() / PRIVATE_NAMES_FILE
    try:
        if config.is_file():
            for line in config.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and len(line) > 3:
                    names.add(line)
    except OSError:
        pass
    try:
        for project in list_projects():
            name = (project.get("name") or "").strip()
            if len(name) > 4:
                names.add(name)
    except Exception:  # noqa: BLE001 - a broken registry must not block a scan
        pass
    return names


# --------------------------------------------------------------------------- #
# where everything lives
# --------------------------------------------------------------------------- #

def home() -> Path:
    """The one directory Glasshouse owns. ``GLASSHOUSE_HOME`` overrides it."""
    override = os.environ.get("GLASSHOUSE_HOME")
    if override:
        return Path(override).expanduser().resolve()
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return (base / APP).resolve()


def ensure_home() -> Path:
    path = home()
    (path / "projects").mkdir(parents=True, exist_ok=True)
    return path


def registry_path() -> Path:
    return ensure_home() / "projects.json"


def agent_id_path() -> Path:
    return ensure_home() / "agent-id"


def load_registry() -> dict[str, Any]:
    path = registry_path()
    if not path.is_file():
        return {"version": REGISTRY_VERSION, "projects": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": REGISTRY_VERSION, "projects": []}
    if not isinstance(data, dict) or not isinstance(data.get("projects"), list):
        return {"version": REGISTRY_VERSION, "projects": []}
    data.setdefault("version", REGISTRY_VERSION)
    return data


def save_registry(data: dict[str, Any]) -> None:
    path = registry_path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def _slug(text: str) -> str:
    keep = [c.lower() if c.isalnum() else "-" for c in text]
    return "".join(keep).strip("-")[:24] or "project"


def project_id(root: Path) -> str:
    """Stable for a path, so the same folder always finds the same history."""
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:8]
    return f"{_slug(root.name)}-{digest}"


def display_path(path: str | os.PathLike[str]) -> str:
    """Replace the user's home prefix with ``~``.

    A dashboard that shouts a full home path at you is harder to read and shows
    more than it needs to. The real path is still used everywhere internally.
    """
    text = str(path)
    try:
        base = str(Path.home())
    except RuntimeError:  # no resolvable home directory
        return text
    if text == base:
        return "~"
    if text.startswith(base + os.sep):
        return "~" + text[len(base):]
    return text


def entry_db(entry: dict[str, Any]) -> Path:
    return Path(entry["db"])


def add_project(path: str | os.PathLike[str] | None = None,
                name: str | None = None) -> dict[str, Any]:
    """Register a folder (idempotent) and adopt a legacy in-repo database."""
    root = Path(path or Path.cwd()).expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError(f"no such project folder: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"not a folder: {root}")

    data = load_registry()
    pid = project_id(root)
    entry = next((p for p in data["projects"] if p.get("id") == pid), None)
    if entry is None:
        folder = ensure_home() / "projects" / pid
        folder.mkdir(parents=True, exist_ok=True)
        db = folder / "glasshouse.db"
        entry = {
            "id": pid,
            "name": name or root.name,
            "path": str(root),
            "db": str(db),
            "created": time.time(),
            "last_opened": time.time(),
            "opens": 0,
        }
        legacy = root / "data" / "glasshouse.db"
        if legacy.is_file() and not db.exists():
            try:
                shutil.copy2(legacy, db)
                entry["migrated_from"] = str(legacy)
            except OSError:
                pass
        data["projects"].append(entry)
    else:
        if name:
            entry["name"] = name
        if not Path(entry["db"]).exists():
            Path(entry["db"]).parent.mkdir(parents=True, exist_ok=True)

    entry["last_opened"] = time.time()
    entry["opens"] = int(entry.get("opens") or 0) + 1
    save_registry(data)
    return entry


def remove_project(ref: str, delete_history: bool = False) -> dict[str, Any]:
    """Unregister a project. The folder itself is never touched.

    History is kept by default: it lives under the Glasshouse home, not inside
    the project, so re-adding the same path later finds the same events again.
    ``delete_history`` is only honoured when the database sits inside our own
    home directory — never somewhere else on the disk.
    """
    entry = get_project(ref)
    if entry is None:
        raise KeyError(f"no such project: {ref}")

    data = load_registry()
    before = len(data["projects"])
    data["projects"] = [p for p in data["projects"] if p.get("id") != entry.get("id")]
    if len(data["projects"]) == before:
        raise KeyError(f"no such project: {ref}")
    save_registry(data)

    deleted = None
    if delete_history:
        folder = Path(entry.get("db") or "").parent.resolve()
        base = (home() / "projects").resolve()
        if base in folder.parents:
            shutil.rmtree(folder, ignore_errors=True)
            deleted = str(folder)
    return {"removed": entry, "deleted": deleted}


def list_projects() -> list[dict[str, Any]]:
    data = load_registry()
    projects = [p for p in data["projects"] if isinstance(p, dict) and p.get("path")]
    return sorted(projects, key=lambda p: p.get("last_opened") or 0, reverse=True)


def get_project(ref: str | None) -> dict[str, Any] | None:
    if not ref:
        return None
    needle = str(ref).strip()
    projects = list_projects()
    if not needle:
        return None
    resolved = Path(needle).expanduser().resolve()
    for p in projects:
        if p.get("id") == needle or p.get("name") == needle:
            return p
        if p.get("path") and Path(p["path"]) == resolved:
            return p
    return None


def resolve(ref: str | None = None, create: bool = True) -> dict[str, Any]:
    """Pick the project for a command or a request.

    Order: explicit ref -> ``GLASSHOUSE_PROJECT`` -> most recently opened ->
    current folder. An explicit folder path is registered on the spot.
    """
    chosen = ref or os.environ.get("GLASSHOUSE_PROJECT")
    if chosen:
        found = get_project(chosen)
        if found:
            return found
        candidate = Path(chosen).expanduser()
        if candidate.exists():
            return add_project(candidate)
        raise FileNotFoundError(f"unknown project: {chosen}")

    projects = list_projects()

    # Working inside a registered folder selects that folder, even if another
    # project was opened more recently. This is what makes an agent's events
    # land in the repository it is actually editing.
    here = Path.cwd().resolve()
    best: dict[str, Any] | None = None
    for candidate in projects:
        root = Path(candidate.get("path") or "")
        if root and (here == root or root in here.parents):
            if best is None or len(str(root)) > len(str(Path(best["path"]))):
                best = candidate
    if best is not None:
        return best

    if projects:
        return projects[0]
    if create:
        return add_project(Path.cwd())
    raise FileNotFoundError("no projects registered")


def db_for(ref: str | None = None) -> Path:
    """Database for a project, or the explicit ``GLASSHOUSE_DB`` override."""
    override = os.environ.get("GLASSHOUSE_DB")
    if override:
        return Path(override).expanduser()
    return entry_db(resolve(ref))


def todo_for(ref: str | None = None) -> Path | None:
    """The project's TODO.md, if it has one. Never registers a folder as a side effect."""
    try:
        root = Path(resolve(ref, create=False)["path"])
    except (FileNotFoundError, KeyError):
        return None
    for candidate in (root / "TODO.md", root / "GLASSHOUSE-TODO.md"):
        if candidate.is_file():
            return candidate
    return None


def touch(entry: dict[str, Any]) -> None:
    data = load_registry()
    for p in data["projects"]:
        if p.get("id") == entry.get("id"):
            p["last_opened"] = time.time()
    save_registry(data)


# --------------------------------------------------------------------------- #
# server lifecycle
# --------------------------------------------------------------------------- #

def server_url(port: int | None = None) -> str:
    port = port or int(os.environ.get("GLASSHOUSE_PORT") or DEFAULT_PORT)
    return f"http://127.0.0.1:{port}"


def get_json(url: str, timeout: float = 2.0) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as res:
            return json.loads(res.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None


def server_up(port: int | None = None) -> bool:
    health = get_json(f"{server_url(port)}/healthz", timeout=0.6)
    return bool(health and health.get("ok"))


def start_server(port: int | None = None) -> bool:
    """Start a detached server if one is not already listening."""
    if server_up(port):
        return True
    port = port or int(os.environ.get("GLASSHOUSE_PORT") or DEFAULT_PORT)
    log = ensure_home() / "server.log"
    with log.open("ab") as handle:
        subprocess.Popen(
            [sys.executable, "-m", "glasshouse.cli", "serve", "--port", str(port)],
            cwd=str(Path(__file__).resolve().parent.parent),
            stdout=handle, stderr=handle, stdin=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
        )
    for _ in range(40):
        if server_up(port):
            return True
        time.sleep(0.15)
    return False


def open_url(url: str) -> bool:
    opener = shutil.which("xdg-open") or shutil.which("open") or shutil.which("gio")
    if not opener:
        return False
    try:
        if opener.endswith("gio"):
            subprocess.Popen([opener, "open", url], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        else:
            subprocess.Popen([opener, url], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# desktop integration — a launcher, not a terminal command
# --------------------------------------------------------------------------- #

def launcher_command() -> list[str]:
    """The command a desktop entry should run.

    Prefer the checkout's own interpreter when there is one, so a launcher
    never depends on what ``python3`` happens to be on the menu's PATH.
    """
    root = Path(__file__).resolve().parent.parent
    script = root / "bin" / "glass"
    venv = root / ".venv" / "bin" / "python"
    if script.is_file():
        if venv.is_file() and Path(sys.executable).resolve() != venv.resolve():
            return [str(venv), str(script)]
        return [str(script)]
    return [sys.executable, "-m", "glasshouse.cli"]


def _desktop_entry(exec_line: str, icon: str, comment: str) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Glasshouse\n"
        f"Comment={comment}\n"
        f"Exec={exec_line}\n"
        f"Icon={icon}\n"
        "Terminal=false\n"
        "Categories=Development;ProjectManagement;\n"
        "Keywords=agent;dashboard;project;\n"
        "StartupNotify=true\n"
    )


def _data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def desktop_dir() -> Path | None:
    """The folder a desktop environment draws icons from, if it has one.

    An application-menu entry is easy to miss; a double-clickable icon on the
    desktop is what a person actually looks for. LXQt, Xfce and most file
    managers draw ``*.desktop`` files from ``~/Desktop``.
    """
    override = os.environ.get("XDG_DESKTOP_DIR")
    if override:
        return Path(override).expanduser()
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "user-dirs.dirs"
    try:
        for line in config.read_text(encoding="utf-8").splitlines():
            if line.startswith("XDG_DESKTOP_DIR="):
                value = line.split("=", 1)[1].strip().strip('"')
                return Path(value.replace("$HOME", str(Path.home()))).expanduser()
    except OSError:
        pass
    fallback = Path.home() / "Desktop"
    return fallback if fallback.is_dir() else None


def _install_icons(written: list[str]) -> str:
    """Copy the logo into the icon theme and return the best Icon= value."""
    source_dir = Path(__file__).resolve().parent / "static"
    home_icon = ensure_home() / "icon.png"
    png = source_dir / "logo.png"
    svg = source_dir / "logo.svg"

    if png.is_file():
        home_icon.write_bytes(png.read_bytes())
        written.append(str(home_icon))
    elif svg.is_file():
        home_icon = ensure_home() / "icon.svg"
        home_icon.write_bytes(svg.read_bytes())
        written.append(str(home_icon))

    icons = _data_home() / "icons" / "hicolor"
    for sub, name, src in (("scalable/apps", "glasshouse.svg", svg),
                           ("256x256/apps", "glasshouse.png", png)):
        if not src.is_file():
            continue
        folder = icons / sub
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / name
        target.write_bytes(src.read_bytes())
        written.append(str(target))

    return str(home_icon)


def setup(autostart: bool = True, icon: bool = True, desktop_icon: bool = True) -> dict[str, Any]:
    """Make the dashboard reachable without a terminal. Idempotent."""
    ensure_home()
    written: list[str] = []
    cmd = launcher_command()
    exec_line = " ".join(f'"{c}"' if " " in c else c for c in cmd) + " menu"

    icon_path = _install_icons(written) if icon else str(ensure_home() / "icon.svg")

    applications = _data_home() / "applications"
    applications.mkdir(parents=True, exist_ok=True)
    entry = applications / "glasshouse.desktop"
    entry.write_text(_desktop_entry(exec_line, icon_path,
                                    "Open the Glasshouse project picker"), encoding="utf-8")
    entry.chmod(0o755)
    written.append(str(entry))

    if desktop_icon and os.environ.get("GLASSHOUSE_NO_DESKTOP") != "1":
        folder = desktop_dir()
        if folder and folder.is_dir():
            on_desktop = folder / "glasshouse.desktop"
            on_desktop.write_text(_desktop_entry(exec_line, icon_path,
                                                 "Open the Glasshouse project picker"),
                                  encoding="utf-8")
            on_desktop.chmod(0o755)
            written.append(str(on_desktop))

    if autostart:
        auto = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "autostart"
        auto.mkdir(parents=True, exist_ok=True)
        auto_entry = auto / "glasshouse.desktop"
        serve = " ".join(f'"{c}"' if " " in c else c for c in cmd) + " daemon"
        auto_entry.write_text(_desktop_entry(serve, icon_path,
                                             "Start the Glasshouse dashboard at login"),
                              encoding="utf-8")
        auto_entry.chmod(0o755)
        written.append(str(auto_entry))

    return {"written": written, "command": cmd, "home": str(home()),
            "desktop": str(desktop_dir() or "")}


def remove_setup() -> list[str]:
    removed: list[str] = []
    targets = [
        _data_home() / "applications" / "glasshouse.desktop",
        Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "autostart" / "glasshouse.desktop",
        _data_home() / "icons" / "hicolor" / "scalable" / "apps" / "glasshouse.svg",
        _data_home() / "icons" / "hicolor" / "256x256" / "apps" / "glasshouse.png",
    ]
    folder = desktop_dir()
    if folder:
        targets.append(folder / "glasshouse.desktop")
    for path in targets:
        if path.exists():
            path.unlink()
            removed.append(str(path))
    return removed


# --------------------------------------------------------------------------- #
# self-check — the command that answers "is my install actually working?"
# --------------------------------------------------------------------------- #

def _check(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": detail}


def doctor(port: int | None = None, start: bool = False) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    checks.append(_check("python", sys.version_info >= (3, 10),
                         f"{sys.version.split()[0]} (need >= 3.10)"))

    try:
        ensure_home()
        probe = home() / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        checks.append(_check("home", True, str(home())))
    except OSError as exc:
        checks.append(_check("home", False, f"{home()}: {exc}"))

    projects = list_projects()
    checks.append(_check("projects", bool(projects),
                         f"{len(projects)} registered" if projects else
                         "none yet — run: glass add ."))

    active = None
    if projects:
        active = projects[0]
        try:
            from . import store
            conn = store.connect(active["db"])
            tables = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            ok, message = store.verify_chain(conn)
            conn.close()
            checks.append(_check("database", "events" in tables,
                                 f"{active['name']}: {message}"))
            checks.append(_check("chain", ok, message))
        except Exception as exc:  # noqa: BLE001 - a doctor reports, never raises
            checks.append(_check("database", False, f"{type(exc).__name__}: {exc}"))
            checks.append(_check("chain", False, "database could not be opened"))

    if start and not server_up(port):
        start_server(port)

    up = server_up(port)
    checks.append(_check("server", up, f"{server_url(port)} {'listening' if up else 'not running'}"))

    if up:
        state = get_json(api_url("/api/state", port)) or {}
        checks.append(_check("api.state", bool(state.get("agents") is not None and state.get("integrity")),
                             f"{len(state.get('agents') or [])} agents, "
                             f"{(state.get('integrity') or {}).get('message', 'no integrity field')}"))
        verify = get_json(api_url("/api/verify", port)) or {}
        checks.append(_check("api.verify", bool(verify.get("ok")), str(verify.get("message"))))

        admin = _fetch_text(api_url("/?project=" + (state.get("project") or {}).get("id", ""), port))
        checks.append(_check("view.admin", bool(admin and "GLASSHOUSE" in admin),
                             "admin HTML served" if admin else "admin HTML not served"))
        race = _fetch_text(api_url("/ai", port))
        checks.append(_check("view.ai", bool(race and "race" in race.lower()),
                             "race HTML served" if race else "race HTML not served"))

    return {
        "ok": all(c["ok"] for c in checks),
        "checks": checks,
        "project": active,
        "url": server_url(port),
    }


def api_url(path: str, port: int | None = None) -> str:
    return server_url(port) + path


def _fetch_text(url: str) -> str:
    try:
        with urllib.request.urlopen(url, timeout=2.0) as res:
            return res.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError):
        return ""


# --------------------------------------------------------------------------- #
# public export — a clean tree with no personal data
# --------------------------------------------------------------------------- #

def privacy_scan(root: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Return every place a personal path, name, or secret appears."""
    findings: list[dict[str, Any]] = []
    hostname = socket.gethostname()
    names = sorted(private_names(), key=len, reverse=True)
    for path in sorted(Path(root).rglob("*")):
        if not path.is_file():
            continue
        if any(part in PRIVATE_DIRS for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for pattern, label in PRIVATE_PATTERNS:
                if pattern.search(line):
                    findings.append({"file": str(path.relative_to(root)), "line": lineno,
                                     "kind": label, "text": line.strip()[:160]})
            if hostname and len(hostname) > 4 and hostname in line:
                findings.append({"file": str(path.relative_to(root)), "line": lineno,
                                 "kind": "hostname", "text": line.strip()[:160]})
            for name in names:
                if name in line:
                    findings.append({"file": str(path.relative_to(root)), "line": lineno,
                                     "kind": "private name", "text": line.strip()[:160]})
                    break
    return findings


PUBLIC_FILES = [
    "glasshouse/__init__.py",
    "glasshouse/home.py",
    "glasshouse/store.py",
    "glasshouse/server.py",
    "glasshouse/cli.py",
    "glasshouse/static/app.css",
    "glasshouse/static/app.js",
    "glasshouse/static/layout.js",
    "glasshouse/static/shell.js",
    "glasshouse/static/admin.js",
    "glasshouse/static/ai.js",
    "glasshouse/static/index.html",
    "glasshouse/static/ai.html",
    "glasshouse/static/projects.html",
    "glasshouse/static/projects.js",
    "glasshouse/static/logo.svg",
    "glasshouse/static/logo.png",
    "bin/glass",
    "tests/test_glasshouse.py",
    "tests/test_glasshouse_home.py",
    "tests/test_glasshouse_cli.py",
    "tests/test_glasshouse_ui.py",
    ".opencode/plugins/glasshouse/index.ts",
]

# Source -> destination. Templates live under docs/ so the private repository
# can keep its own README and licence separate from the published package.
PUBLIC_TEMPLATES = {
    "docs/public/README.md": "README.md",
    "docs/public/pyproject.toml": "pyproject.toml",
    "docs/public/install.sh": "install.sh",
    "docs/public/LICENSE": "LICENSE",
    "docs/public/gitignore": ".gitignore",
    "docs/public/screenshots/admin.png": "docs/screenshots/admin.png",
    "docs/public/screenshots/ai.png": "docs/screenshots/ai.png",
    "docs/public/screenshots/projects.png": "docs/screenshots/projects.png",
}

EXECUTABLE = {"bin/glass", "install.sh"}


def export_public(out: str | os.PathLike[str], force: bool = False) -> dict[str, Any]:
    """Copy the standalone tool to ``out``, then refuse to ship private data."""
    source = Path(__file__).resolve().parent.parent
    target = Path(out).expanduser().resolve()
    if target.exists() and any(target.iterdir()) and not force:
        raise FileExistsError(f"{target} is not empty (use force to overwrite)")
    target.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    missing: list[str] = []
    for rel in PUBLIC_FILES:
        src = source / rel
        if not src.is_file():
            missing.append(rel)
            continue
        dest = target / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        copied.append(rel)

    for rel, dest_rel in PUBLIC_TEMPLATES.items():
        src = source / rel
        if not src.is_file():
            missing.append(rel)
            continue
        dest = target / dest_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        copied.append(dest_rel)

    for rel in EXECUTABLE:
        path = target / rel
        if path.is_file():
            path.chmod(0o755)

    findings = privacy_scan(target)
    return {
        "target": str(target),
        "copied": copied,
        "missing": missing,
        "findings": findings,
        "ok": bool(copied) and not findings,
    }
