"""UI contracts: blocks scroll inside themselves, and the agenda adds from the header.

These are the three complaints from the owner, turned into checks:

1. "the whole page scrolls" -> the page must not scroll; each block must.
2. "I have to go to the bottom to add an agenda item" -> the "+" lives in the
   panel header and the item is written to the project's TODO.md.
3. "I add a folder and I do not know whether it worked" -> the projects page
   states the working folder and reports success or failure in words.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path

import pytest

from glasshouse import home, store
from glasshouse.server import Server

STATIC = Path(__file__).resolve().parents[1] / "glasshouse" / "static"


# --------------------------------------------------------------------------- #
# static contracts (no server needed)
# --------------------------------------------------------------------------- #

def test_pages_do_not_scroll_and_blocks_do():
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert "overflow: hidden" in css, "the document must not scroll"
    assert ".panel > .body { flex: 1 1 auto; min-height: 0; overflow: auto; }" in css, \
        "each block must own its scrollbar"
    assert ".board {" in css and "grid-template-columns: repeat(12" in css, \
        "the board must be a 12-column grid"
    assert "grid-auto-rows" in css, "rows must be a unit, so height resizing means something"


def test_every_block_can_be_sized_and_moved():
    """Every block on every page carries its own default span, and the board is
    the only container — so the layout engine applies to all of them."""
    for name in ("index.html", "ai.html", "projects.html"):
        html = (STATIC / name).read_text(encoding="utf-8")
        assert 'id="board"' in html, f"{name}: no board"
        assert "/static/layout.js" in html, f"{name}: layout engine not loaded"
        blocks = html[html.index('id="board"'):]
        count = blocks.count('class="panel"')
        assert count >= 1, f"{name}: no blocks found"
        assert blocks.count("data-block=") == count, f"{name}: a block cannot be moved"
        assert blocks.count("data-w=") == count, f"{name}: a block has no default width"
        assert blocks.count("data-h=") == count, f"{name}: a block has no default height"
        assert "<div class=\"grid\"" not in html, f"{name}: leftover old grid"


def test_the_layout_engine_saves_and_reflows():
    js = (STATIC / "layout.js").read_text(encoding="utf-8")
    assert "localStorage.setItem" in js and "localStorage.getItem" in js, "layout must survive a reload"
    assert 'el.style.gridColumn = "span "' in js, "width must be a column span, not a pixel width"
    assert 'el.style.gridRow = "span "' in js, "height must be a row span"
    assert "moveTo" in js and "startResize" in js, "drag and resize must both exist"
    for preset in ("column", "row", "two"):
        assert preset + ":" in js, f"missing {preset} preset"


def test_resize_has_no_hidden_mode():
    """The owner's words: "I am still not able to edit the size of blocks".
    Cause found: the handle only existed inside a mode nobody discovered, and a
    1100px breakpoint hid it again. Neither may come back."""
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    js = (STATIC / "layout.js").read_text(encoding="utf-8")

    handle = css[css.index(".rz {"):]
    handle = handle[:handle.index("}")]
    assert "display: block" in handle, "the resize handle must always be visible"
    assert "display: none" not in handle

    assert "arranging" not in js, "resizing must not wait for a mode"
    assert "arranging" not in css, "resizing must not wait for a mode"

    narrow = css[css.index("@media (max-width: 1100px)"):]
    narrow = narrow[:narrow.index("\n}")]
    assert ".board { display: block" not in narrow, "the grid must survive a narrow window"
    assert ".rz" not in narrow, "a narrow window must not hide the handles"

    assert "renderSizes" in js and "data-axis" in js, "there must be click-only − / + sizing"


def test_projects_page_can_remove_a_project():
    js = (STATIC / "projects.js").read_text(encoding="utf-8")
    assert "data-remove=" in js, "every project card needs a Remove button"
    assert "window.confirm" in js, "removing must ask first"
    assert 'remove: id' in js, "the button must call the API"
    assert "history is still on disk" in js, "say what happened to the history"


def test_agenda_plus_button_sits_in_the_header():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    agenda = html[html.index("<h2>Agenda</h2>"):]
    agenda = agenda[:agenda.index("</section>")]
    assert 'id="todo-add"' in agenda, "the + button belongs in the Agenda header"
    assert 'id="todo-form"' in agenda, "the inline form belongs in the same panel"
    assert agenda.index('id="todo-add"') < agenda.index('id="todos"'), \
        "the button must come before the list, so nobody scrolls to the bottom"
    assert "new-todo" in agenda
    # the old bottom input is gone, not duplicated
    assert html.count('id="new-todo"') == 1


def test_projects_page_explains_the_working_folder():
    html = (STATIC / "projects.html").read_text(encoding="utf-8")
    for needed in ('id="currentbar"', 'id="current-path"', 'id="add-hint"', 'role="status"'):
        assert needed in html, needed
    js = (STATIC / "projects.js").read_text(encoding="utf-8")
    assert "Could not add that folder" in js, "a failure must be said out loud"
    assert "just added" in js, "a new folder must be visibly new"


def test_the_plugin_reports_the_session_folder_not_the_process_cwd():
    """B-006: process.cwd() is the harness binary's folder, not the project."""
    plugin = (Path(__file__).resolve().parents[1]
              / ".opencode/plugins/glasshouse/index.ts").read_text(encoding="utf-8")
    assert "cwd: process.cwd()" not in plugin, \
        "a session must register with the folder it is working in"
    for call in ("/api/register", "/api/event", "/api/heartbeat"):
        block = plugin[plugin.index(call):]
        block = block[:block.index("})")]
        assert "cwd" in block, f"{call} must say which folder it is about"


# --------------------------------------------------------------------------- #
# live behaviour
# --------------------------------------------------------------------------- #

@pytest.fixture()
def live(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHOUSE_HOME", str(tmp_path / "gh-home"))
    monkeypatch.delenv("GLASSHOUSE_DB", raising=False)
    monkeypatch.delenv("GLASSHOUSE_PROJECT", raising=False)

    folder = tmp_path / "watched"
    folder.mkdir()
    (folder / "TODO.md").write_text("# TODO\n\n- [ ] **P9-001** **First item**\n", encoding="utf-8")
    entry = home.add_project(folder)

    conn = store.connect(entry["db"])
    store.sync_todos(conn, folder / "TODO.md")
    conn.close()

    server = Server(port=0)
    port = server.httpd.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield entry, port, folder
    server.httpd.shutdown()
    server.httpd.server_close()


def _post(url: str, payload: dict) -> tuple[int, dict]:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=5) as res:
            return res.status, json.loads(res.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def test_agenda_add_writes_to_the_project_todo_and_returns_it(live):
    entry, port, folder = live
    status, body = _post(f"{home.server_url(port)}/api/todos?project={entry['id']}",
                         {"action": "add", "title": "Wire the plus button", "priority": "P9"})
    assert status == 200, body

    text = (folder / "TODO.md").read_text(encoding="utf-8")
    assert "Wire the plus button" in text

    state = home.get_json(f"{home.server_url(port)}/api/state?project={entry['id']}") or {}
    titles = {t["title"] for t in state["todos"]}
    assert "Wire the plus button" in titles
    assert "First item" in titles


def test_an_event_is_filed_under_the_folder_that_sent_it(live, tmp_path):
    """A harness reports from its own process cwd; the folder in the body decides.

    B-006: every event from an OpenCode session landed in the project the
    *server* last opened, so the board of the folder being worked in stayed
    empty while another project's board filled up.
    """
    entry, port, folder = live
    other = tmp_path / "other"
    other.mkdir()
    second = home.add_project(other)
    home.add_project(folder)          # the live project opened most recently

    status, body = _post(f"{home.server_url(port)}/api/event",
                         {"kind": "tool.call", "agent_id": "a1", "action": "read",
                          "target": "x.py", "cwd": str(other)})
    assert status == 200, body

    mine = home.get_json(f"{home.server_url(port)}/api/state?project={second['id']}") or {}
    assert [e["kind"] for e in mine["events"]] == ["tool.call"]
    elsewhere = home.get_json(f"{home.server_url(port)}/api/state?project={entry['id']}") or {}
    assert elsewhere["events"] == [], "an event must not be copied into another project"


def test_an_event_from_an_unregistered_folder_falls_back_to_the_active_project(live, tmp_path):
    """An unknown cwd must not 400 or invent a project."""
    entry, port, folder = live
    active = (home.get_json(f"{home.server_url(port)}/api/projects") or {})["active"]

    status, body = _post(f"{home.server_url(port)}/api/event",
                         {"kind": "note", "agent_id": "a1", "target": "hello",
                          "cwd": str(tmp_path / "never-registered")})
    assert status == 200, body

    state = home.get_json(f"{home.server_url(port)}/api/state?project={active['id']}") or {}
    assert [e["kind"] for e in state["events"]] == ["note"]
    assert {p["id"] for p in home.list_projects()} == {entry["id"]}, \
        "reporting an event must never register a folder"


def test_a_bad_folder_is_refused_with_a_reason(live):
    entry, port, folder = live
    status, body = _post(f"{home.server_url(port)}/api/projects",
                         {"path": str(folder / "does-not-exist")})
    assert status == 400
    assert "no such project folder" in body["error"]


def test_projects_api_names_the_active_project(live):
    entry, port, folder = live
    data = home.get_json(f"{home.server_url(port)}/api/projects") or {}
    assert data["active"]["id"] == entry["id"]
    assert data["active"]["path"] == str(folder.resolve())


def test_no_project_is_a_real_state(live):
    """Exit has to mean exit: @none must not fall back to the last opened folder."""
    entry, port, folder = live
    data = home.get_json(f"{home.server_url(port)}/api/state?project=%40none") or {}
    assert data["project"] is None
    assert data["error"] == "no project selected"
    assert data["projects"], "the picker must still list projects, or there is no way back"


def test_folder_browser_lists_folders_and_never_files(live):
    """Open any folder, without typing a path from memory."""
    entry, port, folder = live
    (folder / "subdir").mkdir()
    (folder / "notes.txt").write_text("x", encoding="utf-8")
    data = home.get_json(f"{home.server_url(port)}/api/folders?path={folder}") or {}
    names = [d["name"] for d in data["dirs"]]
    assert "subdir" in names
    assert "notes.txt" not in names, "this is a folder picker, not a file reader"
    assert data["path"] == str(folder.resolve())
    assert data["registered"] == entry["id"]
    assert data["display"].startswith("~") or not data["display"].startswith("/home/"), \
        "the UI should not shout a full home path"


def test_folder_browser_refuses_a_file(live):
    entry, port, folder = live
    target = folder / "a-file.txt"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(
            f"{home.server_url(port)}/api/folders?path={target}", timeout=5)
    assert err.value.code == 400


# --------------------------------------------------------------------------- #
# removing a project
# --------------------------------------------------------------------------- #

def test_a_project_can_be_removed_and_its_history_is_kept(live):
    entry, port, folder = live
    db = Path(entry["db"])
    status, body = _post(f"{home.server_url(port)}/api/projects", {"remove": entry["id"]})
    assert status == 200, body
    assert entry["id"] not in {p["id"] for p in body["projects"]}
    assert db.is_file(), "history stays unless deletion is asked for explicitly"
    assert home.get_project(entry["id"]) is None


def test_removing_the_last_project_does_not_re_add_the_servers_folder(live):
    """Removal must stick: looking at the dashboard afterwards must not quietly
    register whatever directory the server happens to run in."""
    entry, port, folder = live
    status, _ = _post(f"{home.server_url(port)}/api/projects", {"remove": entry["id"]})
    assert status == 200
    data = home.get_json(f"{home.server_url(port)}/api/projects") or {}
    assert data["projects"] == []
    assert data["active"] is None


def test_removing_an_unknown_project_says_so(live):
    entry, port, folder = live
    status, body = _post(f"{home.server_url(port)}/api/projects", {"remove": "nothing-00000000"})
    assert status == 404
    assert "no such project" in body["error"]
