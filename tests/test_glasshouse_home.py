"""Project registry, storage isolation, desktop setup, and privacy export."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from glasshouse import home, store


@pytest.fixture()
def gh(tmp_path, monkeypatch):
    root = tmp_path / "gh-home"
    monkeypatch.setenv("GLASSHOUSE_HOME", str(root))
    monkeypatch.delenv("GLASSHOUSE_DB", raising=False)
    monkeypatch.delenv("GLASSHOUSE_PROJECT", raising=False)
    return root


def test_add_project_is_idempotent_and_stable(gh, tmp_path):
    folder = tmp_path / "alpha"
    folder.mkdir()

    first = home.add_project(folder)
    second = home.add_project(folder)

    assert first["id"] == second["id"]
    assert len(home.list_projects()) == 1
    assert second["opens"] == 2
    assert Path(first["db"]).parent.is_dir()


def test_remove_project_keeps_history_and_the_folder(gh, tmp_path):
    """Remove means "forget it in the dashboard", not "delete my work"."""
    folder = tmp_path / "alpha"
    folder.mkdir()
    (folder / "notes.md").write_text("do not touch", encoding="utf-8")
    entry = home.add_project(folder)
    db = Path(entry["db"])
    assert db.is_file() or db.parent.is_dir()

    result = home.remove_project(entry["id"])

    assert result["deleted"] is None
    assert home.get_project(entry["id"]) is None
    assert folder.is_dir() and (folder / "notes.md").is_file()
    assert db.parent.is_dir(), "history is kept by default"


def test_remove_project_can_delete_its_own_history_only(gh, tmp_path):
    folder = tmp_path / "beta"
    folder.mkdir()
    entry = home.add_project(folder)
    db = Path(entry["db"])
    assert (gh / "projects") in db.parents

    home.remove_project(entry["id"], delete_history=True)
    assert not db.parent.exists()
    assert folder.is_dir(), "the project folder is never deleted"


def test_remove_unknown_project_raises(gh):
    with pytest.raises(KeyError):
        home.remove_project("nothing-00000000")


def test_project_histories_are_isolated(gh, tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    a = home.add_project(one)
    b = home.add_project(two)

    conn = store.connect(a["db"])
    store.add_event(conn, kind="tool.call", agent_id="agent-a", action="read")
    conn.close()

    conn = store.connect(b["db"])
    store.add_event(conn, kind="tool.call", agent_id="agent-b", action="write")
    conn.close()

    conn = store.connect(a["db"])
    assert conn.execute("SELECT COUNT(*) n FROM events").fetchone()["n"] == 1
    conn.close()
    conn = store.connect(b["db"])
    assert conn.execute("SELECT COUNT(*) n FROM events").fetchone()["n"] == 1
    conn.close()


def test_resolve_by_id_name_and_path(gh, tmp_path):
    folder = tmp_path / "charlie"
    folder.mkdir()
    entry = home.add_project(folder)

    assert home.resolve(entry["id"])["id"] == entry["id"]
    assert home.resolve("charlie")["id"] == entry["id"]
    assert home.resolve(str(folder))["id"] == entry["id"]


def test_resolve_registering_on_the_spot(gh, tmp_path):
    folder = tmp_path / "delta"
    folder.mkdir()
    entry = home.resolve(str(folder))
    assert entry["path"] == str(folder.resolve())
    assert entry["id"] in {p["id"] for p in home.list_projects()}


def test_legacy_database_is_adopted_once(gh, tmp_path):
    folder = tmp_path / "legacy"
    (folder / "data").mkdir(parents=True)
    db = folder / "data" / "glasshouse.db"
    conn = store.connect(db)
    store.add_event(conn, kind="note", agent_id="old", action="kept")
    conn.close()

    entry = home.add_project(folder)
    copied = store.connect(entry["db"])
    assert copied.execute("SELECT COUNT(*) n FROM events").fetchone()["n"] == 1
    copied.close()

    # A second registration must not overwrite the project's own history.
    home.add_project(folder)
    again = store.connect(entry["db"])
    assert again.execute("SELECT COUNT(*) n FROM events").fetchone()["n"] == 1
    again.close()


def test_todo_file_belongs_to_the_project(gh, tmp_path):
    folder = tmp_path / "todo-project"
    folder.mkdir()
    (folder / "TODO.md").write_text("- [ ] **T-001** **Do the thing**\n", encoding="utf-8")
    entry = home.add_project(folder)
    assert home.todo_for(entry["id"]) == folder / "TODO.md"


@pytest.fixture()
def fake_desktop(tmp_path, monkeypatch):
    """Point every XDG location at a throwaway tree (never the real Desktop)."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_DESKTOP_DIR", str(tmp_path / "Desktop"))
    (tmp_path / "Desktop").mkdir()
    return tmp_path


def test_setup_writes_launcher_icon_and_desktop_shortcut(fake_desktop):
    result = home.setup()
    written = {Path(p).name for p in result["written"]}
    assert "glasshouse.desktop" in written
    assert "icon.png" in written
    assert "glasshouse.png" in written

    launcher = fake_desktop / "xdg-data" / "applications" / "glasshouse.desktop"
    autostart = fake_desktop / "xdg-config" / "autostart" / "glasshouse.desktop"
    on_desktop = fake_desktop / "Desktop" / "glasshouse.desktop"
    assert launcher.is_file() and autostart.is_file()
    assert on_desktop.is_file(), "a person needs something visible to double-click"
    assert on_desktop.stat().st_mode & 0o111, "an icon must be executable to launch"

    text = launcher.read_text(encoding="utf-8")
    assert "menu" in text and "Terminal=false" in text
    assert "Icon=/" in text, "an absolute icon path works in every file manager"
    assert "daemon" in autostart.read_text(encoding="utf-8")

    removed = home.remove_setup()
    assert launcher.exists() is False
    assert on_desktop.exists() is False
    assert str(on_desktop) in removed


def test_desktop_icon_can_be_refused(fake_desktop, monkeypatch):
    monkeypatch.setenv("GLASSHOUSE_NO_DESKTOP", "1")
    home.setup()
    assert not (fake_desktop / "Desktop" / "glasshouse.desktop").exists()
    assert (fake_desktop / "xdg-data" / "applications" / "glasshouse.desktop").is_file()


def test_privacy_scanner_finds_private_text(tmp_path):
    evil = tmp_path / "evil"
    evil.mkdir()
    home_path = "/" + "home/alice/secret"
    (evil / "notes.txt").write_text(f"see {home_path}\n", encoding="utf-8")
    (evil / "key.txt").write_text("ghp_" + "a" * 24 + "\n", encoding="utf-8")

    kinds = {f["kind"] for f in home.privacy_scan(evil)}
    assert "absolute home path" in kinds
    assert "GitHub token" in kinds


def test_export_is_clean_and_complete(gh, tmp_path):
    """The real tree must be publishable: this is the privacy guarantee."""
    if not (Path(__file__).resolve().parents[1] / "docs" / "public" / "README.md").is_file():
        pytest.skip("template sources are only present in the development checkout")
    out = tmp_path / "public"
    result = home.export_public(out)

    assert result["ok"], result
    assert not result["findings"]
    assert not result["missing"]

    for required in ("README.md", "LICENSE", "install.sh", "pyproject.toml",
                     "glasshouse/cli.py", "glasshouse/static/logo.svg", "bin/glass"):
        assert (out / required).is_file(), required

    # Private material never ships.
    for forbidden in ("data", "memory", "audit", "TODO.md", ".glasshouse"):
        assert not (out / forbidden).exists(), forbidden

    # Every script and stylesheet the pages ask for must be in the package.
    # Otherwise the published dashboard loads and silently does nothing, which
    # is worse than failing here.
    pages = sorted((out / "glasshouse" / "static").glob("*.html"))
    assert pages
    missing_assets = [
        (page.name, ref)
        for page in pages
        for ref in re.findall(r'(?:src|href)="(/static/[^"]+)"', page.read_text(encoding="utf-8"))
        if not (out / "glasshouse" / ref.lstrip("/")).is_file()
    ]
    assert not missing_assets, missing_assets


def test_cwd_selects_the_project_even_if_another_was_opened_later(gh, tmp_path, monkeypatch):
    """An agent working in a folder must report to that folder, not to whatever
    the owner looked at most recently. Found live: events were landing in the
    wrong history after a second project was opened."""
    inside = tmp_path / "repo-a"
    other = tmp_path / "repo-b"
    inside.mkdir()
    other.mkdir()
    a = home.add_project(inside)
    home.add_project(other)  # most recently opened

    monkeypatch.chdir(inside)
    assert home.resolve()["id"] == a["id"]

    conn = store.connect()
    store.add_event(conn, kind="tool.call", agent_id="agent-in-a", action="edit")
    conn.close()
    check = store.connect(a["db"])
    assert check.execute("SELECT COUNT(*) n FROM events").fetchone()["n"] == 1
    check.close()


def test_private_project_names_are_configurable(gh, tmp_path, monkeypatch):
    """The tool ships publicly, so private names come from config, not code."""
    monkeypatch.setenv("GLASSHOUSE_PRIVATE_NAMES", "secret-workspace")
    tree = tmp_path / "scan"
    tree.mkdir()
    (tree / "notes.txt").write_text("built for secret-workspace\n", encoding="utf-8")
    kinds = {f["kind"] for f in home.privacy_scan(tree)}
    assert "private name" in kinds


def test_display_path_hides_the_home_prefix(monkeypatch):
    """The dashboard shows ~/code/app, not the full home path."""
    # Built from pieces: this file ships publicly, so it must not contain a
    # literal home path — the privacy scanner would (correctly) refuse it.
    base = "/" + "home/somebody"
    monkeypatch.setenv("HOME", base)
    assert home.display_path(base) == "~"
    assert home.display_path(base + "/code/app") == "~/code/app"
    assert home.display_path("/opt/shared/app") == "/opt/shared/app"
