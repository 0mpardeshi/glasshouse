/* Project picker: the "open folder" screen.
   Answers two questions honestly: which folder am I looking at, and is that
   folder's history actually being recorded (events, agents, chain state). */

const P = { projects: [], active: null, stats: {}, fresh: null };

function statLine(id) {
  const s = P.stats[id];
  if (s === undefined) return '<span>counting…</span>';
  if (s === null) return '<span>history unreadable</span>';
  const events = Object.values(s.counts || {}).reduce((a, b) => a + b, 0);
  const chain = s.integrity?.ok ? "chain ok" : "CHAIN BROKEN";
  const last = (s.events && s.events[0] && s.events[0].ts) || 0;
  const when = last ? fmtAge((Date.now() / 1000) - last) : "no events yet";
  return `<span><b>${events}</b> events</span>
    <span><b>${(s.agents || []).length}</b> agents</span>
    <span>${esc(chain)}</span>
    <span>last ${esc(when)}</span>`;
}

function projectCard(p) {
  const opened = p.last_opened ? fmtAge((Date.now() / 1000) - p.last_opened) : "never";
  const isCurrent = P.active && p.id === P.active;
  const isFresh = P.fresh === p.id;
  return `<div class="project${isCurrent ? " current" : ""}" data-id="${esc(p.id)}">
    <div class="name">${esc(p.name)}
      ${isCurrent ? '<span class="badge current">current</span>' : ""}
      ${isFresh ? '<span class="badge">just added</span>' : ""}
    </div>
    <div class="path mono" title="${esc(p.path)}">${esc(p.display || p.path)}</div>
    <div class="stats">${statLine(p.id)}</div>
    <div class="meta">
      <span>opened ${esc(opened)}</span>
      <span>${esc(p.opens || 0)}×</span>
      <span class="mono">${esc(p.id)}</span>
    </div>
    <div class="actions">
      <a class="btn primary" href="/?project=${encodeURIComponent(p.id)}">Open admin</a>
      <a class="btn" href="/ai?project=${encodeURIComponent(p.id)}">AI view</a>
      <button type="button" class="btn danger" data-remove="${esc(p.id)}"
        title="Forget this project in the dashboard; its history stays on this machine">Remove</button>
    </div>
  </div>`;
}

function renderProjects() {
  const box = $("projects");
  const count = $("projects-count");
  if (count) count.textContent = `${P.projects.length} project${P.projects.length === 1 ? "" : "s"}`;
  if (!box) return;
  if (!P.projects.length) {
    box.innerHTML = `<div class="empty">No projects yet. Add a folder above — that is the project
      the dashboard will watch, and its history will be kept from then on.</div>`;
    return;
  }
  box.innerHTML = P.projects.map(projectCard).join("");
}

function renderCurrent() {
  const bar = $("currentbar");
  if (!bar) return;
  const project = P.projects.find((p) => p.id === P.active);
  const path = $("current-path");
  const badge = $("current-badge");
  const open = $("current-open");
  if (project) {
    if (path) {
      path.textContent = project.display || project.path;
      path.title = project.path;
    }
    if (badge) badge.hidden = false;
    if (open) {
      open.hidden = false;
      open.href = `/?project=${encodeURIComponent(project.id)}`;
    }
  } else if (path) {
    path.textContent = P.projects.length
      ? "none selected — pick one below"
      : "none yet — add the folder you are working in";
    if (badge) badge.hidden = true;
    if (open) open.hidden = true;
  }
}

async function loadStats(projects) {
  const selected = projects.slice(0, 12);
  await Promise.all(selected.map(async (p) => {
    try {
      const res = await fetch(`/api/state?project=${encodeURIComponent(p.id)}`, { cache: "no-store" });
      const data = await res.json();
      P.stats[p.id] = data && !data.error ? data : null;
    } catch (err) {
      P.stats[p.id] = null;
    }
    renderProjects();
  }));
}

async function loadProjects() {
  const hint = $("add-hint");
  try {
    // api() forwards ?project=, so "exit" (@none) is not silently replaced by
    // the last folder that happened to be opened.
    const res = await fetch(api("/api/projects"), { cache: "no-store" });
    const data = await res.json();
    P.projects = data.projects || [];
    P.active = (data.active && data.active.id) || null;
    renderCurrent();
    renderProjects();
    const stamp = $("stamp");
    if (stamp) stamp.textContent = `updated ${fmtTime(Date.now() / 1000)}`;
    loadStats(P.projects);
  } catch (err) {
    const box = $("projects");
    if (box) box.innerHTML = '<div class="empty">Server unreachable. Is Glasshouse running?</div>';
    if (hint) {
      hint.className = "statusline bad";
      hint.textContent = "Cannot reach the server. Run: glass doctor";
    }
  }
}

async function addProject() {
  const input = $("project-path");
  const hint = $("add-hint");
  const button = $("add-project");
  if (!input || !input.value.trim()) return;

  const folder = input.value.trim();
  if (hint) {
    hint.className = "statusline";
    hint.textContent = `Checking ${folder}…`;
  }
  if (button) button.disabled = true;
  try {
    const res = await fetch("/api/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: folder }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    input.value = "";
    P.fresh = data.project.id;
    if (hint) {
      hint.className = "statusline ok";
      hint.textContent = `Added “${data.project.name}”. Its history starts empty and fills as agents report.`;
    }
    await loadProjects();
  } catch (err) {
    if (hint) {
      hint.className = "statusline bad";
      hint.textContent = `Could not add that folder: ${err.message}`;
    }
  } finally {
    if (button) button.disabled = false;
  }
}

async function removeProject(id) {
  const project = P.projects.find((p) => p.id === id);
  const name = project ? project.name : id;
  const sure = window.confirm(
    `Remove “${name}” from the dashboard?\n\n` +
    "The folder is not touched. Its recorded history stays on this machine, so " +
    "adding the same folder again brings it back."
  );
  if (!sure) return;
  const hint = $("add-hint");
  try {
    const res = await fetch("/api/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ remove: id }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    if (P.active === id) P.active = null;
    P.stats = {};
    if (hint) {
      hint.className = "statusline ok";
      hint.textContent = `Removed “${name}” from the list. Its history is still on disk.`;
    }
    await loadProjects();
  } catch (err) {
    if (hint) {
      hint.className = "statusline bad";
      hint.textContent = `Could not remove that project: ${err.message}`;
    }
  }
}

function wireProjects() {
  const button = $("add-project");
  if (button) button.addEventListener("click", addProject);
  const input = $("project-path");
  if (input) {
    input.addEventListener("keydown", (ev) => { if (ev.key === "Enter") addProject(); });
  }
  const refresh = $("refresh");
  if (refresh) refresh.addEventListener("click", loadProjects);
  // Delegated: the cards are re-rendered after every change, so a listener per
  // button would disappear with them.
  const list = $("projects");
  if (list) {
    list.addEventListener("click", (ev) => {
      const btn = ev.target.closest("[data-remove]");
      if (btn) removeProject(btn.dataset.remove);
    });
  }
}

wireProjects();
loadProjects();
