/* AI race view: who is where, what they are doing, what happens if they stop. */
const RACE_ORDER = ["working", "paused", "stalled", "blocked", "done", "offline"];

function lapsFor(agentId) {
  return G.feed.filter((e) => e.agent_id === agentId && e.kind !== "heartbeat").length;
}

function lastActionFor(agentId) {
  for (let i = G.feed.length - 1; i >= 0; i -= 1) {
    const e = G.feed[i];
    if (e.agent_id === agentId && e.kind !== "heartbeat") {
      return `${e.kind}${e.action ? ` · ${e.action}` : ""}${e.target ? ` · ${e.target}` : ""}`;
    }
  }
  return "no action recorded";
}

function renderRace(state) {
  const agents = (state.agents || []).slice();
  const rank = (a) => RACE_ORDER.indexOf(statusOf(a));
  agents.sort((a, b) => {
    const d = rank(a) - rank(b);
    if (d !== 0) return d;
    return lapsFor(b.id) - lapsFor(a.id);
  });

  const working = agents.filter((a) => statusOf(a) === "working").length;
  const pits = agents.filter((a) => ["stalled", "paused", "offline"].includes(statusOf(a))).length;
  const laps = G.feed.filter((e) => e.kind !== "heartbeat").length;
  const todos = state.todos || [];
  const next = todos.find((t) => t.status === "active") || todos.find((t) => t.status === "queued");
  const stale = agents.filter((a) => a.age_seconds > 180).length;

  $("k-working").textContent = working;
  $("k-pits").textContent = pits;
  $("k-laps").textContent = laps;
  $("k-next").textContent = next ? `${next.id} ${next.title}` : "nothing queued";
  $("k-risk").textContent = stale ? `${stale} stale` : "low";
  $("pos-count").textContent = `${agents.length} racers`;

  const maxLaps = Math.max(1, ...agents.map((a) => lapsFor(a.id)));
  const race = $("race");
  race.innerHTML = agents.length
    ? agents.map((a, i) => {
        const status = statusOf(a);
        const mine = lapsFor(a.id);
        const pct = Math.max(4, Math.round((mine / maxLaps) * 100));
        const leader = lapsFor(agents[0]?.id || "");
        const gap = i === 0 ? "leader" : `−${leader - mine} actions`;
        return `<div class="lane" data-s="${esc(status)}">
          <div class="row">
            <div class="rank">${i + 1}</div>
            <div>
              <div class="name">${esc(a.name || a.id)}</div>
              <div class="sub">${esc(a.role || "agent")} · ${esc(a.model || "unknown model")} · ${esc(a.harness || "harness?")}</div>
            </div>
            <span class="badge ${statusClass(status)}">${esc(status)}</span>
            <div class="gap">${gap}</div>
          </div>
          <div class="task">${esc(a.task || "no task reported")}</div>
          <div class="track"><div class="bar" style="width:${pct}%"></div></div>
          <div class="foot">
            <span>${mine} actions</span>
            <span>last beat ${fmtAge(a.age_seconds)}</span>
            <span>last: ${esc(lastActionFor(a.id))}</span>
          </div>
        </div>`;
      }).join("")
    : '<div class="empty">No racers yet. Start an agent and it will appear here.</div>';

  const handoff = $("handoff");
  const active = agents.filter((a) => statusOf(a) === "working");
  handoff.innerHTML = active.length
    ? active.map((a) => `<div class="handoff">
        <b>If ${esc(a.name)} stops now</b><br>
        task: ${esc(a.task || "not reported")}<br>
        last: ${esc(lastActionFor(a.id))}<br>
        resume: <code>glass heartbeat --agent ${esc(a.id)} --task "…"</code>
      </div>`).join("")
    : '<div class="empty">No agent is currently working.</div>';

  const problem = agents.filter((a) => ["stalled", "paused", "offline", "blocked"].includes(statusOf(a)));
  const blockedTodos = todos.filter((t) => t.status === "blocked");
  $("help").innerHTML = (problem.length || blockedTodos.length)
    ? problem.map((a) => `<div class="help"><span class="badge ${statusClass(statusOf(a))}">${esc(statusOf(a))}</span>
        <div><b>${esc(a.name)}</b> · ${esc(a.task || "no task")}
        <div class="why">silent ${fmtAge(a.age_seconds)} · last: ${esc(lastActionFor(a.id))}</div></div></div>`).join("")
      + blockedTodos.map((t) => `<div class="help"><span class="badge blocked">blocked</span>
        <div><b>${esc(t.id)}</b> ${esc(t.title)}<div class="why">${esc(t.note || "needs a decision")}</div></div></div>`).join("")
    : '<div class="empty">Nobody is blocked. The track is clear.</div>';

  renderFeed($("feed"), G.feed.slice().reverse(), 30);
}

G.listeners.push(renderRace);
document.addEventListener("DOMContentLoaded", startLive);
