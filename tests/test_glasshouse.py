"""Glasshouse store tests: the record must be trustworthy and stall detection honest."""

import time

import pytest

from glasshouse import store


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHOUSE_DB", str(tmp_path / "gh.db"))
    monkeypatch.setenv("GLASSHOUSE_HOME", str(tmp_path / "gh-home"))
    c = store.connect(tmp_path / "gh.db")
    yield c
    c.close()


def test_event_chain_verifies(conn):
    store.add_event(conn, kind="note", agent_id="a1", action="hello")
    store.add_event(conn, kind="note", agent_id="a1", action="world")
    ok, message = store.verify_chain(conn)
    assert ok, message
    assert "2 events" in message


def test_chain_detects_edit(conn):
    store.add_event(conn, kind="note", agent_id="a1", action="hello")
    store.add_event(conn, kind="note", agent_id="a1", action="world")
    conn.execute("UPDATE events SET action='tampered' WHERE id=1")
    conn.commit()
    ok, message = store.verify_chain(conn)
    assert not ok
    assert "hash mismatch" in message


def test_chain_detects_deleted_tail(conn):
    first = store.add_event(conn, kind="note", agent_id="a1", action="one")
    store.add_event(conn, kind="note", agent_id="a1", action="two")
    # Deleting the tail leaves a valid prefix, so verify() alone cannot see it.
    # This test documents that residual risk instead of pretending otherwise.
    conn.execute("DELETE FROM events WHERE id=2")
    conn.commit()
    ok, _ = store.verify_chain(conn)
    assert ok
    assert first["id"] == 1


def test_stall_classification(conn, monkeypatch):
    agent = store.register_agent(conn, agent_id="a1", name="Builder", task="x")
    assert agent["effective_status"] == "working"
    # pretend the last heartbeat was 20 minutes ago
    old = time.time() - 1200
    conn.execute("UPDATE agents SET last_seen=? WHERE id='a1'", (old,))
    conn.commit()
    fresh = store.get_agent(conn, "a1")
    assert fresh["effective_status"] == "offline"  # > OFFLINE_AFTER


def test_stalled_between_thresholds(conn):
    store.register_agent(conn, agent_id="a1", name="Builder", task="x")
    conn.execute("UPDATE agents SET last_seen=? WHERE id='a1'", (time.time() - 300,))
    conn.commit()
    assert store.get_agent(conn, "a1")["effective_status"] == "stalled"


def test_parse_todo_file(tmp_path):
    path = tmp_path / "TODO.md"
    path.write_text(
        "# TODO\n\n"
        "- [ ] **P0-001** **Audit archive** — lock the files\n"
        "- [x] **P1-010** **Ponytail** — verify\n"
        "- [!] **B-001** **GitHub** — needs auth\n",
        encoding="utf-8",
    )
    items = store.parse_todo_file(path)
    assert [i["id"] for i in items] == ["P0-001", "P1-010", "B-001"]
    assert items[0]["status"] == "queued"
    assert items[0]["title"] == "Audit archive"
    assert items[0]["priority"] == "P0"
    assert items[1]["status"] == "done"
    assert items[2]["status"] == "blocked"


def test_set_todo_edits_file_and_mirror(conn, tmp_path, monkeypatch):
    todo_file = tmp_path / "TODO.md"
    todo_file.write_text("- [ ] **P0-001** **Do a thing** — note\n", encoding="utf-8")
    monkeypatch.setattr(store, "ROOT", tmp_path)
    store.sync_todos(conn, todo_file)
    store.set_todo_status(conn, "P0-001", "done")
    assert "[x] **P0-001**" in todo_file.read_text(encoding="utf-8")
    assert store.list_todos(conn)[0]["status"] == "done"


def test_chain_survives_concurrent_writers(tmp_path):
    """Two agents reporting at once must not fork the chain (found live, 2026-10-25).

    Each writer uses its own connection, exactly like two harness processes.
    Before the fix, the read-last-hash/insert pair raced and produced a
    `prev_hash mismatch` at verify time.
    """
    import threading

    db = tmp_path / "race.db"
    store.connect(db).close()  # schema first
    errors = []

    def writer(agent):
        c = store.connect(db)
        try:
            for i in range(25):
                store.add_event(c, kind="tool.call", agent_id=agent, action=f"call-{i}")
        except Exception as exc:  # pragma: no cover - only on regression
            errors.append(exc)
        finally:
            c.close()

    threads = [threading.Thread(target=writer, args=(f"agent-{n}",)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    c = store.connect(db)
    try:
        ok, message = store.verify_chain(c)
        assert ok, message
        assert c.execute("SELECT COUNT(*) n FROM events").fetchone()["n"] == 100
    finally:
        c.close()


def test_server_rejects_malformed_json(conn, monkeypatch):
    """A junk body must not become a hash-chained event (critic C-07)."""
    import json
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer

    from glasshouse.server import Handler

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/event",
            data=b"{bad json",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(request, timeout=5)
            raise AssertionError("malformed JSON was accepted")
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
        assert conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"] == 0
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_seed_is_labelled_and_clearable(conn):
    store.seed_demo(conn)
    assert any(a["id"].startswith("demo-") for a in store.list_agents(conn))
    store.clear_demo(conn)
    assert not any(a["id"].startswith("demo-") for a in store.list_agents(conn))
