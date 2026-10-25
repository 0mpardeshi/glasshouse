"""CLI and self-check: a live server is asked for the admin and AI views.

This is the "connect my dashboard with yours" test: it starts a real Glasshouse
on a spare port and checks the pages a human would open, not just the store.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from glasshouse import cli, home, store
from glasshouse.server import Server


@pytest.fixture()
def live(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHOUSE_HOME", str(tmp_path / "gh-home"))
    monkeypatch.delenv("GLASSHOUSE_DB", raising=False)
    monkeypatch.delenv("GLASSHOUSE_PROJECT", raising=False)

    folder = tmp_path / "watched"
    folder.mkdir()
    entry = home.add_project(folder)

    conn = store.connect(entry["db"])
    store.register_agent(conn, agent_id="tester", name="tester", role="builder",
                         harness="pytest", model="none", task="self-check")
    store.add_event(conn, kind="tool.call", agent_id="tester", action="read",
                    target="README.md")
    conn.close()

    server = Server(port=0)
    port = server.httpd.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield entry, port
    server.httpd.shutdown()
    server.httpd.server_close()


def test_doctor_sees_every_layer(live):
    entry, port = live
    result = home.doctor(port)

    by_name = {c["name"]: c for c in result["checks"]}
    assert result["ok"], result
    assert by_name["server"]["ok"]
    assert by_name["api.state"]["ok"]
    assert by_name["api.verify"]["ok"]
    assert by_name["view.admin"]["ok"], "admin HTML did not serve"
    assert by_name["view.ai"]["ok"], "AI view did not serve"
    state = home.get_json(f"{home.server_url(port)}/api/state?project={entry['id']}") or {}
    assert state.get("project", {}).get("id") == entry["id"]


def test_state_is_scoped_to_the_selected_project(live, tmp_path):
    entry, port = live
    second = tmp_path / "second"
    second.mkdir()
    other = home.add_project(second)
    store.connect(other["db"]).close()

    scoped = home.get_json(f"{home.server_url(port)}/api/state?project={entry['id']}") or {}
    assert scoped["project"]["id"] == entry["id"]
    assert len(scoped["agents"]) == 1

    separate = home.get_json(f"{home.server_url(port)}/api/state?project={other['id']}") or {}
    assert separate["project"]["id"] == other["id"]
    assert separate["agents"] == []


def test_http_can_register_a_project(live):
    entry, port = live
    import json
    import urllib.request

    new_folder = Path(entry["path"]).parent / "added-over-http"
    new_folder.mkdir()
    body = json.dumps({"path": str(new_folder)}).encode()
    request = urllib.request.Request(
        f"{home.server_url(port)}/api/projects", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=5) as res:
        payload = json.loads(res.read())
    assert payload["ok"]
    assert payload["project"]["path"] == str(new_folder.resolve())


def test_cli_parser_exposes_the_new_commands():
    parser = cli.build_parser()
    for command in ("projects", "add", "open", "menu", "daemon", "doctor", "setup", "export"):
        args = parser.parse_args([command, *([] if command != "export" else ["--out", "/tmp/x"])])
        assert hasattr(args, "func")


def test_doctor_starts_nothing_when_the_server_is_down(gh_only):
    # No live fixture here: a missing server must be reported as a failure, not hidden.
    home.add_project(gh_only)
    result = home.doctor(port=1)  # nothing listens on port 1
    by_name = {c["name"]: c for c in result["checks"]}
    assert by_name["server"]["ok"] is False
    assert result["ok"] is False


@pytest.fixture()
def gh_only(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHOUSE_HOME", str(tmp_path / "gh-home"))
    monkeypatch.delenv("GLASSHOUSE_DB", raising=False)
    folder = tmp_path / "plain"
    folder.mkdir()
    return folder


def test_register_routes_by_agent_cwd(live, tmp_path):
    """HTTP registration uses the agent's cwd, not the last opened project."""
    entry, port = live
    other = tmp_path / "other-repo"
    other.mkdir()
    home.add_project(other)  # becomes the most recent

    import json
    import urllib.request

    body = json.dumps({
        "agent_id": "cwd-agent", "name": "cwd-agent", "session_id": "s-cwd",
        "cwd": entry["path"], "harness": "pytest", "model": "none",
    }).encode()
    request = urllib.request.Request(f"{home.server_url(port)}/api/register", data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(request, timeout=5).read()

    state = home.get_json(f"{home.server_url(port)}/api/state?project={entry['id']}") or {}
    assert any(a["id"] == "cwd-agent" for a in state["agents"])
