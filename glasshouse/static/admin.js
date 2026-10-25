/* Admin view: agents, live feed, agenda, files, integrity. */
function renderAdmin(state) {
  const agents = state.agents || [];
  const working = agents.filter((a) => statusOf(a) === "working").length;
  const stalled = agents.filter((a) => ["stalled", "paused"].includes(statusOf(a))).length;
  const events = Object.values(state.counts || {}).reduce((a, b) => a + b, 0);
  const files = state.files || [];

  $("k-working").textContent = working;
  $("k-stalled").textContent = stalled;
  $("k-agents").textContent = agents.length;
  $("k-events").textContent = events;
  $("k-files").textContent = files.length;
  $("agents-count").textContent = `${agents.length} registered`;

  const ok = state.integrity?.ok;
  const banner = $("integrity");
  banner.className = `banner ${ok ? "ok" : "bad"}`;
  banner.innerHTML = ok
    ? `Chain intact — ${esc(state.integrity.message)}. Every row below is append-only.`
    : `CHAIN BROKEN — ${esc(state.integrity?.message || "unknown")}. Treat this record as suspect.`;

  const box = $("agents");
  box.innerHTML = agents.length
    ? agents.map(renderAgent).join("")
    : '<div class="empty">No agent has registered yet. Run <code>glass register</code>.</div>';

  const feed = $("feed");
  renderFeed(feed, G.feed.slice().reverse(), 80);
  $("feed-count").textContent = `${events} total`;

  const fbox = $("files");
  fbox.innerHTML = files.length
    ? files.map((f) => `<div class="file"><span class="path">${esc(f.target)}</span>
        <span class="hits">${f.hits}× · ${fmtAge(state.generated_at - f.ts)} · ${esc(agentLabel(f.last_agent))}</span></div>`).join("")
    : '<div class="empty">No file edits recorded yet.</div>';
  $("files-count").textContent = `${files.length} paths`;

  const todos = state.todos || [];
  const open = todos.filter((t) => t.status !== "done").length;
  $("todo-count").textContent = `${open} open / ${todos.length}`;
  $("todos").innerHTML = todos.length
    ? todos.map((t) => `<div class="todo" data-s="${esc(t.status)}">
        <input type="checkbox" data-id="${esc(t.id)}" ${t.status === "done" ? "checked" : ""}>
        <div class="tid">${esc(t.id)}</div>
        <div><div class="ttitle">${esc(t.title)}</div>${t.note ? `<div class="tnote">${esc(t.note)}</div>` : ""}</div>
        <div class="pill">${esc(t.status)}</div>
      </div>`).join("")
    : '<div class="empty">TODO.md has no machine-readable items.</div>';

  document.querySelectorAll("#todos input[type=checkbox]").forEach((el) => {
    el.onchange = () => setTodo(el.dataset.id, el.checked ? "done" : "queued");
  });
}

function renderAgent(a) {
  const status = statusOf(a);
  const initial = (a.name || "?").slice(0, 1).toUpperCase();
  const meta = [];
  if (a.role) meta.push(`role ${a.role}`);
  if (a.harness) meta.push(a.harness);
  if (a.session_id) meta.push(`session ${String(a.session_id).slice(0, 12)}`);
  if (a.parent_id) meta.push(`child of ${agentLabel(a.parent_id)}`);
  return `<div class="agent" data-s="${esc(status)}">
    <div class="avatar">${esc(initial)}</div>
    <div>
      <div class="who">${esc(a.name || a.id)} <span class="model">${esc(a.model || "")}</span>
        <span class="badge ${statusClass(status)}">${esc(status)}</span></div>
      <div class="task">${esc(a.task || "no task reported")}</div>
      <div class="meta">${meta.map(esc).join(" · ")} · last beat ${fmtAge(a.age_seconds)}</div>
    </div>
    <div class="mono" style="color:var(--muted);text-align:right">
      ${a.age_seconds > 180 ? `<span class="badge stalled">silent</span>` : ""}
    </div>
  </div>`;
}

G.listeners.push(renderAdmin);
document.addEventListener("DOMContentLoaded", () => {
  wireTodoInput();
  startLive();
});
