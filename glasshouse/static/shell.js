/* Project chrome — which folder you are looking at, open any folder, and leave.
 *
 * Two complaints this fixes:
 *   "I cannot open any other project"  -> browse the disk, no path typing.
 *   "I cannot exit"                    -> Exit sets project=@none, which the
 *                                          server honours instead of silently
 *                                          falling back to the last opened folder.
 */
(function () {
  "use strict";

  const bar = document.querySelector(".topbar");
  if (!bar) return;
  const $ = (id) => document.getElementById(id);

  /* ------------------------------------------------------------ top bar */

  const group = document.createElement("div");
  group.className = "shellbtns";
  group.innerHTML =
    '<button type="button" class="btn shellbtn" id="open-folder" ' +
    'title="Browse to a folder and watch it">Open folder</button>' +
    '<button type="button" class="btn shellbtn" id="exit-project" ' +
    'title="Stop looking at a project">Exit</button>';

  const picker = $("project-picker");
  if (picker && picker.parentNode === bar) {
    bar.insertBefore(group, picker.nextSibling);
  } else {
    const spacer = bar.querySelector(".spacer");
    if (spacer) bar.insertBefore(group, spacer); else bar.append(group);
  }

  /* -------------------------------------------------------------- dialog */

  const dialog = document.createElement("dialog");
  dialog.className = "folders";
  dialog.id = "folder-dialog";
  dialog.innerHTML =
    '<div class="fd-head">' +
    "<strong>Open a folder</strong>" +
    '<span class="fd-sub">Each folder keeps its own history.</span>' +
    '<button type="button" class="btn shellbtn" id="fd-close" aria-label="Close">Close</button>' +
    "</div>" +
    '<div class="fd-path mono" id="fd-path" title="Current folder">…</div>' +
    '<div class="fd-actions">' +
    '<button type="button" class="btn" id="fd-up">Up one level</button>' +
    '<button type="button" class="btn" id="fd-home">Home</button>' +
    '<button type="button" class="btn primary" id="fd-use">Watch this folder</button>' +
    "</div>" +
    '<div class="fd-list" id="fd-list"></div>' +
    '<p class="statusline" id="fd-status" role="status" aria-live="polite"></p>';
  document.body.append(dialog);

  const state = { path: null, listing: null };

  const status = (text, kind) => {
    const el = $("fd-status");
    if (!el) return;
    el.className = "statusline" + (kind ? " " + kind : "");
    el.textContent = text || "";
  };

  function renderListing(data) {
    state.listing = data;
    state.path = data.path;
    const path = $("fd-path");
    if (path) {
      path.textContent = data.display || data.path;
      path.title = data.path;
    }
    const up = $("fd-up");
    if (up) up.disabled = !data.parent;
    const use = $("fd-use");
    if (use) {
      use.textContent = data.registered ? "Already watched — open it" : "Watch this folder";
    }

    const list = $("fd-list");
    if (!list) return;
    const rows = data.dirs.map((d) => (
      '<button type="button" class="fd-item" data-path="' + esc(d.path) + '">' +
      '<span class="fd-icon" aria-hidden="true">▸</span>' +
      '<span class="fd-name">' + esc(d.name) + "</span></button>"
    ));
    if (data.truncated) rows.push('<div class="empty">Only the first 400 folders are shown.</div>');
    if (!rows.length) rows.push('<div class="empty">No folders inside this one.</div>');
    list.innerHTML = rows.join("");
    list.scrollTop = 0;
  }

  async function load(path) {
    status("Reading…");
    try {
      const url = "/api/folders" + (path ? "?path=" + encodeURIComponent(path) : "");
      const res = await fetch(url, { cache: "no-store" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "HTTP " + res.status);
      renderListing(data);
      status("");
    } catch (err) {
      status("Could not read that folder: " + err.message, "bad");
    }
  }

  function open() {
    const start = (window.G && window.G.state && window.G.state.project && window.G.state.project.path) || null;
    if (!dialog.open) dialog.showModal();
    load(state.path || start);
  }

  async function watch() {
    if (!state.path) return;
    const use = $("fd-use");
    if (use) use.disabled = true;
    status("Registering " + state.path + "…");
    try {
      const res = await fetch("/api/projects", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: state.path }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "HTTP " + res.status);
      status("Opened " + data.project.name + ".", "ok");
      location.href = "/?project=" + encodeURIComponent(data.project.id);
    } catch (err) {
      status("Could not open that folder: " + err.message, "bad");
    } finally {
      if (use) use.disabled = false;
    }
  }

  dialog.addEventListener("click", (ev) => {
    const item = ev.target.closest(".fd-item");
    if (item) { load(item.dataset.path); return; }
    if (ev.target === dialog) dialog.close();   // click the backdrop
  });
  $("fd-close").addEventListener("click", () => dialog.close());
  $("fd-up").addEventListener("click", () => {
    if (state.listing && state.listing.parent) load(state.listing.parent);
  });
  $("fd-home").addEventListener("click", () => load(null));
  $("fd-use").addEventListener("click", watch);

  $("open-folder").addEventListener("click", open);
  $("exit-project").addEventListener("click", () => {
    location.href = "/projects?project=%40none";
  });

  // Esc, Ctrl/Cmd-O: keyboard routes, so the mouse is optional.
  document.addEventListener("keydown", (ev) => {
    if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "o") {
      ev.preventDefault();
      open();
    }
  });

  window.GlasshouseShell = { openFolder: open };
})();
