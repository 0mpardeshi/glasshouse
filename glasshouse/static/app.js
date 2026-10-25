/* Glasshouse shared client: state, live stream, small renderers. No dependencies. */
const G = {
  state: null,
  feed: [],
  maxFeed: 400,
  listeners: [],
  // One server watches every project; a tab watches one project.
  project: new URLSearchParams(location.search).get("project") || null,
};

/** Append the selected project to an API path. */
function api(path) {
  if (!G.project) return path;
  return `${path}${path.includes("?") ? "&" : "?"}project=${encodeURIComponent(G.project)}`;
}

const $ = (id) => document.getElementById(id);

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function fmtTime(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function fmtAge(seconds) {
  if (seconds == null) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

function statusOf(agent) {
  return agent.effective_status || agent.status || "unknown";
}

function statusClass(status) {
  return ["working", "stalled", "paused", "offline", "done", "blocked"].includes(status)
    ? status : "offline";
}

function agentLabel(id) {
  const a = (G.state?.agents || []).find((x) => x.id === id);
  return a ? a.name : (id || "system");
}

async function loadState() {
  try {
    const res = await fetch(api("/api/state"), { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    G.state = await res.json();
    if (G.state.project?.id) G.project = G.state.project.id;
    if (!G.feed.length) G.feed = (G.state.events || []).slice().reverse();
    render();
  } catch (err) {
    const dot = $("live-dot");
    if (dot) dot.classList.add("off");
    const text = $("live-text");
    if (text) text.textContent = "server unreachable";
  }
}

function render() {
  if (!G.state) return;
  renderPicker();
  if (!G.state.project) {
    showNoProject();
    return;
  }
  document.body.classList.remove("noproject");
  G.listeners.forEach((fn) => {
    try { fn(G.state); } catch (err) { console.error(err); }
  });
  const stamp = $("stamp");
  if (stamp) stamp.textContent = `updated ${fmtTime(G.state.generated_at)}`;
}

/** Nothing is selected: say so where the integrity banner lives, and leave the
 *  blocks alone. The way back in is the folder button, not a dead end. */
function showNoProject() {
  document.body.classList.add("noproject");
  const banner = $("integrity") || $("hint");
  if (banner) {
    banner.className = "banner ok";
    banner.innerHTML = "No project selected. Use <b>Open folder</b> in the bar above, " +
      "or pick one from the dropdown.";
  }
  const stamp = $("stamp");
  if (stamp) stamp.textContent = "no project selected";
}

function startLive() {
  const dot = $("live-dot");
  const text = $("live-text");
  const es = new EventSource(api("/api/stream"));
  es.onopen = () => {
    dot?.classList.remove("off");
    if (text) text.textContent = "live";
  };
  es.onerror = () => {
    dot?.classList.add("off");
    if (text) text.textContent = "reconnecting…";
  };
  es.addEventListener("message", (ev) => {
    try {
      const event = JSON.parse(ev.data);
      if (G.project && event.project && event.project !== G.project) return;
      G.feed.push(event);
      if (G.feed.length > G.maxFeed) G.feed = G.feed.slice(-G.maxFeed);
      if (G.state) {
        G.state.events = G.feed.slice(-100).reverse();
        render();
      }
    } catch (err) { /* ignore malformed frame */ }
  });
  setInterval(() => { loadState(); }, 5000);
  loadState();
}

function renderEvent(event) {
  const status = event.status || "";
  const detail = event.detail && typeof event.detail === "object"
    ? Object.entries(event.detail).slice(0, 2).map(([k, v]) => `${k}=${v}`).join(" ")
    : (event.detail || "");
  return `<div class="ev">
    <div class="t">${fmtTime(event.ts)}</div>
    <div>
      <span class="k">${esc(event.kind)}</span>
      ${event.action ? `<span class="target"> · ${esc(event.action)}</span>` : ""}
      ${event.target ? `<div class="target">${esc(event.target)}</div>` : ""}
      <div class="mono" style="color:var(--muted)">
        <span class="agent-name">${esc(agentLabel(event.agent_id))}</span>
        ${status ? ` · <span class="status-${esc(statusClass(status))}">${esc(status)}</span>` : ""}
        ${event.duration_ms ? ` · ${event.duration_ms}ms` : ""}
        ${detail ? ` · ${esc(detail)}` : ""}
      </div>
    </div>
  </div>`;
}

function renderFeed(container, events, limit = 60) {
  if (!events || !events.length) {
    container.innerHTML = '<div class="empty">No events yet.</div>';
    return;
  }
  container.innerHTML = events.slice(0, limit).map(renderEvent).join("");
}

function renderPicker() {
  const picker = $("project-picker");
  if (!picker) return;
  const projects = G.state?.projects || [];
  const active = G.state?.project?.id || "";
  const options = projects.map((p) => (
    `<option value="${esc(p.id)}"${p.id === active ? " selected" : ""}>${esc(p.name)}</option>`
  ));
  // A project can always be left, even when only one is registered.
  options.push(`<option value="@none"${active ? "" : " selected"}>\u2014 no project \u2014</option>`);
  picker.innerHTML = options.join("");
  picker.dataset.ready = "1";
}

function wirePicker() {
  const picker = $("project-picker");
  if (!picker) return;
  picker.addEventListener("change", () => {
    if (!picker.value) return;
    if (picker.value === "@none") {
      location.href = "/projects?project=%40none";
      return;
    }
    const base = location.pathname === "/ai" ? "/ai" : "/";
    location.href = `${base}?project=${encodeURIComponent(picker.value)}`;
  });
}

async function setTodo(id, status) {
  await fetch(api("/api/todos"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id, status }),
  });
  loadState();
}

function wireTodoInput() {
  const form = $("todo-form");
  const input = $("new-todo");
  const toggle = $("todo-add");
  if (!form || !input) return;

  const open = (show) => {
    form.hidden = !show;
    if (toggle) toggle.setAttribute("aria-expanded", show ? "true" : "false");
    if (show) input.focus();
  };

  if (toggle) {
    toggle.addEventListener("click", () => open(form.hidden));
  }
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") open(false);
  });
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const title = input.value.trim();
    if (!title) return;
    const res = await fetch(api("/api/todos"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "add", title, priority: "P9" }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      input.setCustomValidity("");
      input.placeholder = body.error || "Could not add that item — is TODO.md writable?";
      input.reportValidity?.();
      return;
    }
    input.value = "";
    open(false);
    loadState();
  });
}

wirePicker();
