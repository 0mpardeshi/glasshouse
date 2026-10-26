/**
 * Glasshouse auto-telemetry for OpenCode (V2 plugin API).
 *
 * Loaded automatically from `.opencode/plugins/`. Every session — including a
 * temporary subagent — registers itself, streams tool calls and file edits, and
 * sends a heartbeat, so the owner's dashboard never depends on an agent
 * remembering to report.
 *
 * Design rules:
 *   - telemetry must never block or break a session: every call is wrapped,
 *     time-boxed, and failures are swallowed;
 *   - no prompts or file contents are sent, only paths, tool names, and the
 *     first line of the task;
 *   - `GLASSHOUSE_DISABLE=1` turns it off; `GLASSHOUSE_URL` moves the server.
 *
 * V2 shape: default export `{ id, setup(ctx) }`. Hooks are registered on their
 * domain (`ctx.tool.hook`, `ctx.session.hook`) and events arrive through
 * `ctx.event.subscribe`. Older/partial contexts are tolerated: a missing domain
 * disables only that channel, never the plugin.
 */

const BASE = (process.env.GLASSHOUSE_URL || "http://127.0.0.1:7077").replace(/\/$/, "")
const ENABLED = process.env.GLASSHOUSE_DISABLE !== "1"
const MODEL_FALLBACK = process.env.GLASSHOUSE_MODEL || "unknown"

const sessions = new Map()
const starts = new Map()

function short(value) {
  const text = String(value ?? "").replace(/\s+/g, " ").trim()
  return text.length > 180 ? `${text.slice(0, 177)}…` : text
}

function firstName(text) {
  return short(text).split(/[.!?\n]/)[0].slice(0, 120)
}

async function post(path, body) {
  if (!ENABLED) return
  try {
    await fetch(`${BASE}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(1500),
    })
  } catch {
    /* dashboard offline — never disturb the agent */
  }
}

function sessionAgent(sessionID, patch = {}, cwd) {
  if (!sessionID) return null
  const key = String(sessionID)
  const current = sessions.get(key) || {
    id: `oc-${key.slice(-12)}`,
    name: `agent-${key.slice(-4)}`,
    role: "builder",
    model: MODEL_FALLBACK,
    task: "",
  }
  Object.assign(current, Object.fromEntries(Object.entries(patch).filter(([, v]) => v)))
  // The folder the session works in, never process.cwd(): the harness binary is
  // started wherever the owner was, which may be another project entirely.
  if (cwd) current.cwd = String(cwd)
  sessions.set(key, current)

  if (!current.registered) {
    current.registered = true
    post("/api/register", {
      agent_id: current.id,
      name: current.name,
      role: current.role,
      harness: "opencode",
      model: current.model,
      session_id: key,
      parent_id: current.parent_id || null,
      task: current.task || null,
      pid: process.pid,
      cwd: current.cwd || process.cwd(),
      meta: { source: "opencode-plugin" },
    })
  }
  return current
}

function emit(sessionID, kind, action, target, status, detail, durationMs, cwd) {
  const agent = sessionAgent(sessionID, {}, cwd)
  if (!agent) return
  post("/api/event", {
    kind,
    agent_id: agent.id,
    session_id: String(sessionID),
    action: action || null,
    target: target ? short(target) : null,
    status: status || "ok",
    duration_ms: durationMs ?? null,
    detail: detail ?? null,
    cwd: agent.cwd || cwd || process.cwd(),
  })
}

function targetFromArgs(args) {
  if (!args || typeof args !== "object") return ""
  return (
    args.filePath || args.path || args.url || args.pattern || args.description ||
    args.command || args.query || args.name || ""
  )
}

function describeEvent(event) {
  const props = event?.properties || event?.data || {}
  return { type: event?.type, props }
}

export default {
  id: "glasshouse.telemetry",

  async setup(ctx) {
    if (!ENABLED) return

    const directory = ctx?.location?.directory || process.cwd()
    const harnessId = `oc-harness-${process.pid}`
    // Every call says which folder it is about, so the server files it there.
    const where = (sessionID) =>
      sessions.get(String(sessionID || ""))?.cwd || directory
    const report = (sessionID, kind, action, target, status, detail, ms) =>
      emit(sessionID, kind, action, target, status, detail, ms, where(sessionID))

    // 1. The harness itself is an agent: if it is alive, the dashboard is honest.
    await post("/api/register", {
      agent_id: harnessId,
      name: "OpenCode",
      role: "harness",
      harness: "opencode",
      model: "harness",
      task: `watching ${directory}`,
      pid: process.pid,
      cwd: directory,
      meta: { source: "opencode-plugin", version: ctx?.app?.version || null },
    })

    // 2. Heartbeats keep a live session — and the harness — out of "stalled".
    const beat = setInterval(() => {
      post("/api/heartbeat", {
        agent_id: harnessId,
        status: "working",
        task: `watching ${directory}`,
        cwd: directory,
      })
      for (const [sessionID, agent] of sessions) {
        post("/api/heartbeat", {
          agent_id: agent.id,
          session_id: sessionID,
          status: "working",
          task: agent.task || null,
          cwd: agent.cwd || directory,
        })
      }
    }, 30_000)
    if (typeof beat.unref === "function") beat.unref()

    // 3. Tool calls: before/after, with duration.
    if (typeof ctx?.tool?.hook === "function") {
      await ctx.tool.hook("execute.before", (event) => {
        try {
          const key = event?.callID || event?.id || `${event?.tool}-${Date.now()}`
          starts.set(key, Date.now())
          const agent = sessionAgent(event?.sessionID, {}, directory)
          const target = targetFromArgs(event?.input ?? event?.args)
          if (agent && /^(edit|write|patch|multiedit)$/i.test(event?.tool || "") && target) {
            agent.task = `editing ${short(target)}`
          }
          report(event?.sessionID, "tool.call", event?.tool, target, "started")
        } catch {
          /* ignore */
        }
      })

      await ctx.tool.hook("execute.after", (event) => {
        try {
          const key = event?.callID || event?.id
          const started = (key && starts.get(key)) || 0
          if (key) starts.delete(key)
          const duration = started ? Date.now() - started : undefined
          const target = targetFromArgs(event?.input ?? event?.args)
          const failed = event?.status === "error" || Boolean(event?.error)
          report(event?.sessionID, "tool.call", event?.tool, target, failed ? "error" : "ok", null, duration)
          if (/^(edit|write|patch|multiedit)$/i.test(event?.tool || "") && target) {
            report(event?.sessionID, "file.edit", event.tool, target, "ok", null, duration)
          }
        } catch {
          /* ignore */
        }
      })
    }

    // 4. A new user prompt tells us the task in plain words.
    if (typeof ctx?.session?.hook === "function") {
      await ctx.session.hook("prompt", (event) => {
        try {
          const text = event?.prompt?.text || event?.text || ""
          const agentName = event?.agent || event?.agentID
          sessionAgent(event?.sessionID, {
            name: agentName ? String(agentName) : undefined,
            role: agentName && /critic|review/i.test(String(agentName)) ? "critic" : "builder",
            task: text ? firstName(text) : undefined,
          }, directory)
        } catch {
          /* ignore */
        }
      })

      // The model is only known when the request is assembled. Capture it here
      // so the owner sees which model is doing the work, including subagents.
      await ctx.session.hook("context", (event) => {
        try {
          const model = event?.model
          const id = typeof model === "string" ? model : model?.id || model?.modelID
          const agentName = event?.agent || event?.agentID
          if (event?.sessionID && (id || agentName)) {
            const agent = sessionAgent(event.sessionID, {
              model: id ? String(id) : undefined,
              name: agentName ? String(agentName) : undefined,
            }, directory)
            if (agent && id) {
              // sessionAgent only registers once; push the late-known model.
              post("/api/heartbeat", {
                agent_id: agent.id,
                session_id: event.sessionID,
                model: String(id),
                cwd: where(event.sessionID),
              })
            }
          }
        } catch {
          /* ignore */
        }
      })
    }

    // 5. Server events: file edits, busy/idle, errors, permission pauses.
    const controller = new AbortController()
    if (typeof ctx?.event?.subscribe === "function") {
      void (async () => {
        try {
          for await (const event of ctx.event.subscribe({ signal: controller.signal })) {
            try {
              const { type, props } = describeEvent(event)
              if (type === "file.edited" && props.file) {
                report(props.sessionID, "file.edit", "changed", props.file, "ok")
              } else if (type === "session.status") {
                const kind = props.status?.type || props.status
                const status = kind === "busy" ? "working" : kind === "idle" ? "paused" : "working"
                const agent = sessionAgent(props.sessionID, {}, directory)
                if (agent) {
                  post("/api/heartbeat", {
                    agent_id: agent.id,
                    session_id: props.sessionID,
                    status,
                    cwd: where(props.sessionID),
                  })
                }
              } else if (type === "session.error") {
                report(props.sessionID, "status", "error", null, "error",
                  short(props.error?.name || "session error"))
              } else if (type === "permission.updated") {
                report(props.sessionID, "status", "permission",
                  short(props.title || props.type || ""), "paused",
                  "waiting for a human decision")
              } else if (type === "session.deleted") {
                const sessionID = props.info?.id || props.sessionID
                const agent = sessionAgent(sessionID, {}, directory)
                if (agent) post("/api/status",
                  { agent_id: agent.id, status: "done", cwd: where(sessionID) })
              }
            } catch {
              /* one bad event must not stop the stream */
            }
          }
        } catch {
          /* subscription closed or aborted */
        }
      })()
    }

    return () => {
      clearInterval(beat)
      controller.abort()
    }
  },
}
