/* Board layout — every block is yours to size and place.
 *
 * How it works, in one paragraph: the board is a 12-column grid with rows of
 * equal height. A block stores only three things — how many columns it spans,
 * how many rows it spans, and its order. The browser then does the arithmetic:
 * making a block wider takes the space from its neighbour, making it taller
 * pushes the blocks below it down, and moving it re-flows everything after it.
 * No hand-placed coordinates, no overlaps to repair, nothing to keep in sync.
 *
 * Two ways to change a block, both always available, no mode to find first:
 * drag the handle in its bottom-right corner (or press the arrow keys while
 * that handle has focus), or open Layout and use the − / + buttons next to the
 * block's name. Drag a block's title onto another block to move it there.
 * Everything is remembered per page, in this browser only.
 */
(function () {
  "use strict";

  const COLS = 12;
  const MAXROWS = 12;
  const board = document.getElementById("board");
  if (!board) return;

  const page = board.dataset.page || "page";
  const KEY = "glasshouse.layout." + page;
  const clamp = (n, lo, hi) => Math.max(lo, Math.min(hi, n));
  const panels = () => Array.from(board.children).filter((el) => el.classList.contains("panel"));
  const keyOf = (el, i) => el.dataset.block || "block-" + i;

  /* ---------------------------------------------------------------- state */

  function defaults() {
    return panels().map((el, i) => ({
      key: keyOf(el, i),
      w: clamp(parseInt(el.dataset.w, 10) || COLS, 1, COLS),
      h: clamp(parseInt(el.dataset.h, 10) || 1, 1, MAXROWS),
    }));
  }

  function load() {
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem(KEY) || "null"); } catch (e) { saved = null; }
    const base = defaults();
    if (!saved || !Array.isArray(saved.blocks)) {
      // A narrow window cannot show three blocks side by side without making
      // each one unreadable, so the first visit stacks them instead. Once a
      // layout is saved, it is honoured at every width.
      if (window.innerWidth < 1100) return base.map((b) => ({ ...b, w: COLS }));
      return base;
    }

    // Keep the stored order, drop blocks that no longer exist, and give any new
    // block its HTML default at the end. A stale layout must never hide a block.
    const out = [];
    for (const item of saved.blocks) {
      if (!item || !base.some((b) => b.key === item.key)) continue;
      if (out.some((b) => b.key === item.key)) continue;
      out.push({
        key: item.key,
        w: clamp(parseInt(item.w, 10) || COLS, 1, COLS),
        h: clamp(parseInt(item.h, 10) || 1, 1, MAXROWS),
      });
    }
    for (const b of base) if (!out.some((x) => x.key === b.key)) out.push(b);
    return out;
  }

  let layout = load();

  function save() {
    try { localStorage.setItem(KEY, JSON.stringify({ blocks: layout })); } catch (e) { /* private mode */ }
  }

  function apply() {
    const byKey = new Map(panels().map((el, i) => [keyOf(el, i), el]));
    layout.forEach((b, i) => {
      const el = byKey.get(b.key);
      if (!el) return;
      el.style.order = String(i);
      el.style.gridColumn = "span " + clamp(b.w, 1, COLS);
      el.style.gridRow = "span " + clamp(b.h, 1, MAXROWS);
    });
  }

  /* ------------------------------------------------------------- neighbours
   * A width change is a trade, not an absolute: widening a block takes columns
   * from the block next to it *in the same row*, so the row always still adds
   * up to 12 and nothing gets pushed onto a new line behind your back. That is
   * the behaviour the owner described: make one block wider and its neighbour
   * gets narrower.
   */

  function rowMates(key) {
    const info = [];
    panels().forEach((el, i) => {
      const rect = el.getBoundingClientRect();
      info.push({ key: keyOf(el, i), top: Math.round(rect.top), left: Math.round(rect.left) });
    });
    const me = info.find((x) => x.key === key);
    if (!me) return { mates: [], donor: null };
    const mates = info
      .filter((x) => Math.abs(x.top - me.top) <= 2)
      .sort((a, b) => a.left - b.left);
    const pos = mates.findIndex((x) => x.key === key);
    const neighbour = mates[pos + 1] || mates[pos - 1] || null;
    return {
      mates,
      donor: neighbour ? layout.find((b) => b.key === neighbour.key) || null : null,
    };
  }

  /** Move `item` to `target` columns, taking them from `donor` when there is one. */
  function resizeTo(item, target, donor) {
    const want = clamp(target, 1, COLS);
    let delta = want - item.w;
    if (!delta) return false;
    if (donor && donor !== item) {
      const room = delta > 0 ? donor.w - 1 : COLS - donor.w;
      delta = delta > 0 ? Math.min(delta, room) : Math.max(delta, -room);
      if (!delta) return false;
      donor.w -= delta;
    }
    item.w += delta;
    return true;
  }

  /* ---------------------------------------------------------------- resize */

  function startResize(ev, el, key) {
    ev.preventDefault();
    ev.stopPropagation();
    const item = layout.find((b) => b.key === key);
    if (!item) return;

    const rect = el.getBoundingClientRect();
    const perCol = rect.width / Math.max(1, item.w);
    const perRow = rect.height / Math.max(1, item.h);
    const x0 = ev.clientX;
    const y0 = ev.clientY;
    const w0 = item.w;
    const h0 = item.h;
    const donor = rowMates(key).donor;
    el.classList.add("resizing");

    const move = (e) => {
      const wantW = w0 + Math.round((e.clientX - x0) / perCol);
      const wantH = clamp(h0 + Math.round((e.clientY - y0) / perRow), 1, MAXROWS);
      const widthChanged = resizeTo(item, wantW, donor);
      const heightChanged = wantH !== item.h;
      if (heightChanged) item.h = wantH;
      if (widthChanged || heightChanged) apply();
    };
    const stop = () => {
      el.classList.remove("resizing");
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", stop);
      window.removeEventListener("pointercancel", stop);
      save();
      announce();
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop);
    window.addEventListener("pointercancel", stop);
  }

  function nudge(ev, el, key) {
    const item = layout.find((b) => b.key === key);
    if (!item) return;
    const step = { ArrowLeft: ["w", -1], ArrowRight: ["w", 1], ArrowUp: ["h", -1], ArrowDown: ["h", 1] }[ev.key];
    if (!step) return;
    ev.preventDefault();
    const [field, delta] = step;
    if (field === "w") {
      resizeTo(item, item.w + delta, rowMates(key).donor);
    } else {
      item.h = clamp(item.h + delta, 1, MAXROWS);
    }
    apply();
    save();
    announce();
  }

  /* ------------------------------------------------------------------ drag */

  let dragging = null;

  function moveTo(fromKey, toKey) {
    const from = layout.findIndex((b) => b.key === fromKey);
    const to = layout.findIndex((b) => b.key === toKey);
    if (from < 0 || to < 0 || from === to) return;
    const [item] = layout.splice(from, 1);
    layout.splice(to, 0, item);
    apply();
    save();
    announce();
  }

  function wire(el, key) {
    const header = el.querySelector(":scope > header");

    if (header) {
      header.draggable = true;
      header.title = "Drag onto another block to move this one there";
      header.addEventListener("dragstart", (ev) => {
        // A drag that starts on the header's own buttons is a click, not a move.
        if (ev.target.closest("button, a, input")) { ev.preventDefault(); return; }
        dragging = key;
        el.classList.add("dragging");
        try {
          ev.dataTransfer.setData("text/plain", key);
          ev.dataTransfer.effectAllowed = "move";
        } catch (e) { /* older browsers */ }
      });
      header.addEventListener("dragend", () => {
        dragging = null;
        el.classList.remove("dragging");
        panels().forEach((p) => p.classList.remove("drop-target"));
      });
    }

    el.addEventListener("dragover", (ev) => {
      if (!dragging || dragging === key) return;
      ev.preventDefault();
      el.classList.add("drop-target");
    });
    el.addEventListener("dragleave", () => el.classList.remove("drop-target"));
    el.addEventListener("drop", (ev) => {
      ev.preventDefault();
      el.classList.remove("drop-target");
      if (!dragging || dragging === key) return;
      moveTo(dragging, key);
      dragging = null;
    });

    let handle = el.querySelector(":scope > .rz");
    if (!handle) {
      handle = document.createElement("div");
      handle.className = "rz";
      handle.tabIndex = 0;
      handle.setAttribute("role", "separator");
      handle.setAttribute("aria-label", "Resize this block: drag, or use the arrow keys");
      handle.title = "Drag to resize (arrow keys also work)";
      handle.innerHTML = "<span></span>";
      el.append(handle);
    }
    handle.addEventListener("pointerdown", (ev) => startResize(ev, el, key));
    handle.addEventListener("keydown", (ev) => nudge(ev, el, key));
  }

  /* -------------------------------------------------------------- toolbar */

  const PRESETS = {
    default: () => defaults(),
    column: () => layout.map((b) => ({ key: b.key, w: COLS, h: 1 })),
    row: () => {
      const w = Math.max(1, Math.floor(COLS / Math.max(1, layout.length)));
      return layout.map((b) => ({ key: b.key, w, h: 1 }));
    },
    two: () => layout.map((b) => ({ key: b.key, w: Math.ceil(COLS / 2), h: 1 })),
  };

  function announce() {
    const note = document.getElementById("layout-note");
    if (!note) return;
    note.textContent = "Saved in this browser. Or drag a block\u2019s bottom-right corner " +
      "to resize it in one motion.";
  }

  /* -------------------------------------------------------- click-to-size
   * The corner handle needs a steady hand; this is the same change with two
   * buttons. It also makes the width rule visible: widening a block by one
   * narrows its neighbour in the same row by one, so the row still adds to 12.
   */

  function blockName(el, key) {
    const h = el.querySelector(":scope > header h2");
    return ((h && h.textContent.trim()) || key).slice(0, 30);
  }

  function sizeBtn(key, axis, d, text, title) {
    return '<button type="button" class="szbtn" data-key="' + esc(key) + '" data-axis="' + axis +
      '" data-d="' + d + '" title="' + esc(title) + '" aria-label="' + esc(title) + '">' +
      text + "</button>";
  }

  function renderSizes() {
    const box = document.getElementById("layout-blocks");
    if (!box) return;
    const byKey = new Map(panels().map((el, i) => [keyOf(el, i), el]));
    box.innerHTML = layout.map((b) => {
      const el = byKey.get(b.key);
      if (!el) return "";
      return '<div class="szrow">' +
        '<span class="szname" title="' + esc(blockName(el, b.key)) + '">' + esc(blockName(el, b.key)) + "</span>" +
        '<span class="szval">' + b.w + "\u00d7" + b.h + "</span>" +
        '<span class="szgroup"><span class="szlbl">W</span>' +
          sizeBtn(b.key, "w", -1, "\u2212", "One column narrower") +
          sizeBtn(b.key, "w", 1, "+", "One column wider") +
        "</span>" +
        '<span class="szgroup"><span class="szlbl">H</span>' +
          sizeBtn(b.key, "h", -1, "\u2212", "One row shorter") +
          sizeBtn(b.key, "h", 1, "+", "One row taller") +
        "</span>" +
        "</div>";
    }).join("");
  }

  function stepSize(key, axis, step) {
    const item = layout.find((b) => b.key === key);
    if (!item) return;
    if (axis === "w") {
      resizeTo(item, item.w + step, rowMates(key).donor);
    } else {
      item.h = clamp(item.h + step, 1, MAXROWS);
    }
    apply();
    save();
    announce();
    renderSizes();
  }

  function menuButton(text, act) {
    return '<button type="button" class="menu-item" data-act="' + act + '">' + text + "</button>";
  }

  function buildMenu() {
    const bar = document.querySelector(".topbar");
    if (!bar) return;

    const wrap = document.createElement("div");
    wrap.className = "layoutctl";
    wrap.innerHTML =
      '<button type="button" class="btn shellbtn" id="layout-btn" aria-expanded="false" ' +
      'aria-controls="layout-menu" title="Size, order and arrangement of the blocks">Layout</button>' +
      '<div class="menu" id="layout-menu" hidden>' +
      '<div class="menu-title">Each block \u2014 click to resize</div>' +
      '<div id="layout-blocks"></div>' +
      '<p class="menu-note" id="layout-note"></p>' +
      '<div class="menu-sep"></div>' +
      '<div class="menu-title">Arrangement</div>' +
      menuButton("Default (2 up, 3 down)", "default") +
      menuButton("One column \u2014 one under another", "column") +
      menuButton("One row \u2014 side by side", "row") +
      menuButton("Two per row", "two") +
      '<div class="menu-sep"></div>' +
      menuButton("Reset every block", "reset") +
      "</div>";

    const live = bar.querySelector(".live");
    if (live) bar.insertBefore(wrap, live); else bar.append(wrap);

    const btn = wrap.querySelector("#layout-btn");
    const menu = wrap.querySelector("#layout-menu");

    const close = () => {
      menu.hidden = true;
      btn.setAttribute("aria-expanded", "false");
    };
    btn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      menu.hidden = !menu.hidden;
      btn.setAttribute("aria-expanded", menu.hidden ? "false" : "true");
      if (!menu.hidden) { announce(); renderSizes(); }
    });
    document.addEventListener("click", (ev) => {
      if (!wrap.contains(ev.target)) close();
    });
    document.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape") close();
    });

    // One click on − / + must not close the menu: resizing is meant to be
    // repeated until the block looks right.
    menu.addEventListener("click", (ev) => {
      const step = ev.target.closest("[data-key][data-axis]");
      if (step) {
        ev.stopPropagation();
        stepSize(step.dataset.key, step.dataset.axis, parseInt(step.dataset.d, 10) || 0);
        return;
      }
      const target = ev.target.closest("[data-act]");
      if (!target) return;
      const act = target.dataset.act;
      if (act === "reset") {
        try { localStorage.removeItem(KEY); } catch (e) { /* private mode */ }
        layout = defaults();
      } else if (PRESETS[act]) {
        layout = PRESETS[act]();
      }
      apply();
      save();
      announce();
      renderSizes();
      close();
    });
  }

  /* ------------------------------------------------------------------ boot */

  panels().forEach((el, i) => wire(el, keyOf(el, i)));
  apply();
  buildMenu();
  announce();
})();
