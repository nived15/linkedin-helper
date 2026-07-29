// Iframe renderer for the roadmap canvas.
//
// Serves a static shell; all state arrives via GET /api/state and live updates
// arrive over SSE at /events. Colours and type come from the host theme tokens
// with literal fallbacks so the canvas still looks right if a token is missing.

export function renderHtml() {
    return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Roadmap</title>
<style>
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--background-color-default, #ffffff);
    color: var(--text-color-default, #1f2328);
    font-family: var(--font-sans, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif);
    font-size: var(--text-body-medium, 14px);
    line-height: var(--leading-body-medium, 20px);
  }
  .wrap { padding: 16px 18px 48px; max-width: 1100px; margin: 0 auto; }
  h1 {
    font-family: var(--font-sans-display, var(--font-sans, inherit));
    font-size: var(--text-title-large, 24px);
    font-weight: var(--font-weight-semibold, 600);
    line-height: var(--leading-title-large, 30px);
    margin: 0 0 2px;
  }
  h2 {
    font-size: var(--text-title-small, 16px);
    font-weight: var(--font-weight-semibold, 600);
    margin: 26px 0 10px;
  }
  .sub { color: var(--text-color-muted, #59636e); font-size: var(--text-body-small, 12px); }
  code, .mono { font-family: var(--font-mono, "SFMono-Regular", Consolas, monospace); font-size: var(--text-code-inline, 12px); }

  /* ---- header ---- */
  header { border-bottom: 1px solid var(--border-color-default, #d1d9e0); padding-bottom: 14px; }
  .stats { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
  .stat {
    border: 1px solid var(--border-color-default, #d1d9e0);
    border-radius: 8px; padding: 7px 11px; min-width: 84px;
  }
  .stat b { display: block; font-size: 18px; font-weight: var(--font-weight-semibold, 600); line-height: 1.2; }
  .stat span { color: var(--text-color-muted, #59636e); font-size: 11px; text-transform: uppercase; letter-spacing: .04em; }
  .bar { height: 7px; border-radius: 4px; background: var(--border-color-default, #d1d9e0); overflow: hidden; margin-top: 12px; display: flex; }
  .bar i { display: block; height: 100%; }
  .bar .b-done { background: var(--true-color-green, #1a7f37); }
  .bar .b-prog { background: var(--true-color-blue, #0969da); }
  .bar .b-block { background: var(--true-color-red, #cf222e); }

  /* ---- tabs ---- */
  nav { display: flex; gap: 2px; margin: 16px 0 4px; border-bottom: 1px solid var(--border-color-default, #d1d9e0); }
  nav button {
    background: none; border: none; border-bottom: 2px solid transparent;
    padding: 8px 13px; cursor: pointer; color: var(--text-color-muted, #59636e);
    font: inherit; border-radius: 6px 6px 0 0;
  }
  nav button:hover { background: var(--background-color-muted, rgba(0,0,0,.04)); color: var(--text-color-default, #1f2328); }
  nav button[aria-selected="true"] { color: var(--text-color-default, #1f2328); border-bottom-color: var(--true-color-blue, #0969da); font-weight: var(--font-weight-semibold, 600); }
  nav button:focus-visible { outline: 2px solid var(--color-focus-outline, #0969da); outline-offset: -2px; }
  .panel[hidden] { display: none; }

  /* ---- cards ---- */
  .card {
    border: 1px solid var(--border-color-default, #d1d9e0);
    border-radius: 10px; padding: 12px 14px; margin-bottom: 10px;
    border-left: 3px solid var(--border-color-default, #d1d9e0);
  }
  .card.s-done      { border-left-color: var(--true-color-green, #1a7f37); }
  .card.s-inprogress{ border-left-color: var(--true-color-blue, #0969da); }
  .card.s-blocked   { border-left-color: var(--true-color-red, #cf222e); }
  .card.s-deferred  { opacity: .62; border-left-style: dashed; }
  .card-head { display: flex; align-items: flex-start; gap: 10px; }
  .card-head .id {
    font-family: var(--font-mono, monospace); font-size: 11px; font-weight: 600;
    background: var(--background-color-muted, rgba(0,0,0,.05));
    border: 1px solid var(--border-color-default, #d1d9e0);
    border-radius: 5px; padding: 2px 6px; white-space: nowrap; flex: none;
  }
  .card-head .t { flex: 1; font-weight: var(--font-weight-semibold, 600); }
  .desc { color: var(--text-color-muted, #59636e); margin: 7px 0 0; font-size: var(--text-body-small, 12px); line-height: 18px; }
  .meta { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin-top: 9px; }
  .pill {
    font-size: 11px; padding: 2px 7px; border-radius: 20px;
    border: 1px solid var(--border-color-default, #d1d9e0);
    color: var(--text-color-muted, #59636e);
  }
  .pill.audit { border-color: var(--true-color-blue-muted, #54aeff); color: var(--true-color-blue, #0969da); }
  .pill.dep-open { border-color: var(--true-color-red-muted, #ff8182); color: var(--true-color-red, #cf222e); }
  .pill.dep-ok { border-color: var(--true-color-green, #1a7f37); color: var(--true-color-green, #1a7f37); }

  select, textarea, button.act {
    font: inherit; color: inherit;
    background: var(--background-color-default, #fff);
    border: 1px solid var(--border-color-default, #d1d9e0);
    border-radius: 6px; padding: 4px 7px;
  }
  select { font-size: 12px; cursor: pointer; }
  select:focus-visible, textarea:focus-visible, button:focus-visible { outline: 2px solid var(--color-focus-outline, #0969da); outline-offset: 1px; }
  textarea { width: 100%; margin-top: 8px; resize: vertical; min-height: 34px; font-size: 12px; padding: 6px 8px; }
  .notes-row { display: none; }
  .notes-row.open { display: block; }
  button.act { cursor: pointer; font-size: 12px; padding: 3px 9px; }
  button.act:hover { background: var(--background-color-muted, rgba(0,0,0,.04)); }

  /* ---- phase / flow ---- */
  .phase { border: 1px solid var(--border-color-default, #d1d9e0); border-radius: 10px; padding: 13px 15px; margin-bottom: 12px; }
  .phase-head { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
  .phase-head h3 { margin: 0; font-size: 15px; font-weight: var(--font-weight-semibold, 600); }
  .exit { margin-top: 8px; padding: 8px 10px; border-radius: 7px; background: var(--background-color-muted, rgba(0,0,0,.04)); font-size: 12px; }
  .exit b { font-weight: var(--font-weight-semibold, 600); }
  .flow { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
  .node {
    font-family: var(--font-mono, monospace); font-size: 11px;
    border: 1px solid var(--border-color-default, #d1d9e0); border-radius: 6px;
    padding: 4px 8px; cursor: pointer; background: none; color: inherit;
  }
  .node.s-done { border-color: var(--true-color-green, #1a7f37); color: var(--true-color-green, #1a7f37); }
  .node.s-inprogress { border-color: var(--true-color-blue, #0969da); color: var(--true-color-blue, #0969da); }
  .node.s-blocked { border-color: var(--true-color-red, #cf222e); color: var(--true-color-red, #cf222e); }
  .node.s-deferred { opacity: .5; border-style: dashed; }
  .node.hl { box-shadow: 0 0 0 2px var(--true-color-blue-muted, #54aeff); }
  .arrow { color: var(--text-color-muted, #59636e); align-self: center; }

  .legend { display: flex; gap: 12px; flex-wrap: wrap; font-size: 11px; color: var(--text-color-muted, #59636e); margin-top: 6px; }
  .legend i { display: inline-block; width: 9px; height: 9px; border-radius: 2px; margin-right: 4px; vertical-align: middle; }
  .foot { margin-top: 26px; padding-top: 12px; border-top: 1px solid var(--border-color-default, #d1d9e0); font-size: 11px; color: var(--text-color-muted, #59636e); }
  .empty { color: var(--text-color-muted, #59636e); font-size: 12px; font-style: italic; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1 id="title">Roadmap</h1>
    <div class="sub" id="subtitle"></div>
    <div class="bar" id="bar"></div>
    <div class="stats" id="stats"></div>
    <div class="legend">
      <span><i style="background:var(--true-color-green,#1a7f37)"></i>Done</span>
      <span><i style="background:var(--true-color-blue,#0969da)"></i>In progress</span>
      <span><i style="background:var(--true-color-red,#cf222e)"></i>Blocked</span>
      <span><i style="background:var(--border-color-default,#d1d9e0)"></i>Pending</span>
      <span><i style="background:var(--border-color-default,#d1d9e0);opacity:.5"></i>Deferred</span>
    </div>
  </header>

  <nav role="tablist">
    <button role="tab" data-tab="overview" aria-selected="true">Requirements</button>
    <button role="tab" data-tab="modules" aria-selected="false">Modules</button>
    <button role="tab" data-tab="roadmap" aria-selected="false">Roadmap &amp; flow</button>
    <button role="tab" data-tab="validation" aria-selected="false">Validation</button>
  </nav>

  <section class="panel" id="p-overview"></section>
  <section class="panel" id="p-modules" hidden></section>
  <section class="panel" id="p-roadmap" hidden></section>
  <section class="panel" id="p-validation" hidden></section>

  <div class="foot" id="foot"></div>
</div>

<script>
(function () {
  var STATUSES = ["pending", "in-progress", "done", "blocked", "deferred"];
  var LABELS = { "pending": "Pending", "in-progress": "In progress", "done": "Done", "blocked": "Blocked", "deferred": "Deferred" };
  var state = null, summary = null, tab = "overview", highlight = null;

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function cls(status) { return "s-" + String(status).replace("-", ""); }
  function allTasks() {
    var out = [];
    state.modules.forEach(function (m) { m.tasks.forEach(function (t) { out.push(t); }); });
    return out;
  }
  function taskById(id) {
    var found = null;
    allTasks().forEach(function (t) { if (t.id === id) found = t; });
    return found;
  }
  function openDeps(t) {
    return (t.deps || []).filter(function (d) {
      var dep = taskById(d);
      return dep && dep.status !== "done" && dep.status !== "deferred";
    });
  }

  function selectHtml(kind, id, status) {
    var o = STATUSES.map(function (s) {
      return '<option value="' + s + '"' + (s === status ? " selected" : "") + ">" + LABELS[s] + "</option>";
    }).join("");
    return '<select data-kind="' + kind + '" data-id="' + esc(id) + '" aria-label="Status for ' + esc(id) + '">' + o + "</select>";
  }

  function renderHeader() {
    document.getElementById("title").textContent = state.title;
    document.getElementById("subtitle").innerHTML =
      esc(state.repo) + " &middot; " + summary.done + " of " + summary.totalTasks +
      " work items complete (" + summary.percentComplete + "%) &middot; " +
      summary.validationsPassed + "/" + summary.validationsTotal + " validations passing" +
      (summary.deferred ? " &middot; " + summary.deferred + " deferred" : "");

    var tot = summary.totalTasks || 1;
    document.getElementById("bar").innerHTML =
      '<i class="b-done" style="width:' + (summary.done / tot * 100) + '%"></i>' +
      '<i class="b-prog" style="width:' + (summary.inProgress / tot * 100) + '%"></i>' +
      '<i class="b-block" style="width:' + (summary.blocked / tot * 100) + '%"></i>';

    var cells = [
      ["Done", summary.done], ["In progress", summary.inProgress],
      ["Blocked", summary.blocked], ["Pending", summary.pending],
      ["Ready now", summary.ready.length], ["Deferred", summary.deferred]
    ];
    document.getElementById("stats").innerHTML = cells.map(function (c) {
      return '<div class="stat"><b>' + c[1] + "</b><span>" + c[0] + "</span></div>";
    }).join("");

    document.getElementById("foot").textContent =
      "State: docs/roadmap.json" + (state.updatedAt ? " \\u00b7 last updated " + new Date(state.updatedAt).toLocaleString() : " \\u00b7 not yet written");
  }

  function renderOverview() {
    var h = "<h2>Section 1 &middot; System requirements &amp; safety constraints</h2>" +
      '<p class="sub">Non-negotiable. Each one needs a concrete artefact in the repo before it counts as met.</p>';
    h += state.constraints.map(function (c) {
      return '<div class="card ' + cls(c.status) + '">' +
        '<div class="card-head"><span class="id">' + esc(c.id) + '</span><span class="t">' + esc(c.title) + "</span>" +
        selectHtml("constraint", c.id, c.status) + "</div>" +
        '<p class="desc">' + esc(c.detail) + "</p>" +
        '<div class="meta"><span class="pill">Evidence: ' + esc(c.evidence) + "</span></div></div>";
    }).join("");

    h += "<h2>Ready to start now</h2>";
    if (!summary.ready.length) {
      h += '<p class="empty">Nothing unblocked. Every pending item is waiting on a dependency.</p>';
    } else {
      h += '<div class="flow">' + summary.ready.map(function (r) {
        return '<button class="node" data-goto="' + esc(r.id) + '">' + esc(r.id) + " &middot; " + esc(r.title) + "</button>";
      }).join("") + "</div>";
    }

    h += "<h2>Progress by module</h2>";
    h += summary.byModule.map(function (m) {
      var pct = m.total ? Math.round(m.done / m.total * 100) : 0;
      return '<div class="card"><div class="card-head"><span class="id">' + esc(m.id) + '</span>' +
        '<span class="t">' + esc(m.name) + '</span><span class="pill">' + m.done + "/" + m.total + " &middot; " + pct + "%</span></div>" +
        '<div class="bar"><i class="b-done" style="width:' + (m.total ? m.done / m.total * 100 : 0) + '%"></i>' +
        '<i class="b-prog" style="width:' + (m.total ? m.inProgress / m.total * 100 : 0) + '%"></i>' +
        '<i class="b-block" style="width:' + (m.total ? m.blocked / m.total * 100 : 0) + '%"></i></div></div>';
    }).join("");
    document.getElementById("p-overview").innerHTML = h;
  }

  function taskCard(t) {
    var open = openDeps(t);
    var depPill = "";
    if ((t.deps || []).length) {
      depPill = '<span class="pill ' + (open.length ? "dep-open" : "dep-ok") + '">' +
        (open.length ? "Waiting on " + open.join(", ") : "Deps met: " + t.deps.join(", ")) + "</span>";
    } else {
      depPill = '<span class="pill dep-ok">No dependencies</span>';
    }
    var refs = (t.planRefs || []).length ? '<span class="pill">plan: ' + esc(t.planRefs.join(", ")) + "</span>" : "";
    var origin = t.origin === "audit" ? '<span class="pill audit">Added from audit</span>' : "";
    return '<div class="card ' + cls(t.status) + '" id="card-' + esc(t.id) + '">' +
      '<div class="card-head"><span class="id">' + esc(t.id) + '</span><span class="t">' + esc(t.title) + "</span>" +
      selectHtml("task", t.id, t.status) + "</div>" +
      '<p class="desc">' + esc(t.description) + "</p>" +
      '<div class="meta"><span class="pill">' + esc(t.phase) + "</span>" + depPill + origin + refs +
      '<button class="act" data-notes="' + esc(t.id) + '">' + (t.notes ? "Notes \\u2713" : "Add note") + "</button></div>" +
      '<div class="notes-row' + (t.notes ? " open" : "") + '" data-notes-for="' + esc(t.id) + '">' +
      '<textarea data-note-input="' + esc(t.id) + '" placeholder="Implementation notes, blockers, decisions...">' + esc(t.notes || "") + "</textarea>" +
      '<button class="act" data-save-note="' + esc(t.id) + '">Save note</button></div></div>';
  }

  function renderModules() {
    var h = "<h2>Section 2 &middot; Architectural modules &amp; workflow matrix</h2>" +
      '<p class="sub">Change a status and it is written straight to docs/roadmap.json.</p>';
    h += state.modules.map(function (m) {
      var live = m.tasks.filter(function (t) { return t.status !== "deferred"; });
      var done = live.filter(function (t) { return t.status === "done"; }).length;
      return "<h2>" + esc(m.name) + ' <span class="pill">' + done + "/" + live.length + "</span></h2>" +
        '<p class="sub">' + esc(m.blurb) + "</p>" +
        m.tasks.map(taskCard).join("");
    }).join("");
    document.getElementById("p-modules").innerHTML = h;
  }

  function renderRoadmap() {
    var h = "<h2>Section 3 &middot; Phased roadmap &amp; dependency graph</h2>" +
      '<p class="sub">Click any node to jump to its card. Nodes are coloured by live status.</p>';
    h += state.phases.map(function (p) {
      var s = null;
      summary.byPhase.forEach(function (x) { if (x.id === p.id) s = x; });
      var pct = s && s.total ? Math.round(s.done / s.total * 100) : 0;
      var nodes = p.taskIds.map(function (id, i) {
        var t = taskById(id);
        if (!t) return "";
        var arrow = i > 0 ? '<span class="arrow">&rarr;</span>' : "";
        return arrow + '<button class="node ' + cls(t.status) + (highlight === id ? " hl" : "") +
          '" data-goto="' + esc(id) + '" title="' + esc(t.title) + '">' + esc(id) + "</button>";
      }).join("");
      return '<div class="phase"><div class="phase-head"><h3>' + esc(p.name) + "</h3>" +
        '<span class="pill">' + (s ? s.done + "/" + s.total : "") + " &middot; " + pct + "%</span>" +
        (s && s.deferred ? '<span class="pill">' + s.deferred + " deferred</span>" : "") + "</div>" +
        '<p class="desc">' + esc(p.goal) + "</p>" +
        '<div class="flow">' + nodes + "</div>" +
        '<div class="exit"><b>Exit criteria:</b> ' + esc(p.exit) + "</div></div>";
    }).join("");

    h += "<h2>Dependency edges</h2>";
    var edges = [];
    allTasks().forEach(function (t) {
      (t.deps || []).forEach(function (d) { edges.push([d, t.id]); });
    });
    if (!edges.length) {
      h += '<p class="empty">No dependencies declared.</p>';
    } else {
      h += '<div class="flow">' + edges.map(function (e) {
        var from = taskById(e[0]), to = taskById(e[1]);
        return '<span class="pill"><button class="node ' + cls(from ? from.status : "pending") + '" data-goto="' + esc(e[0]) + '">' + esc(e[0]) +
          "</button> &rarr; " + '<button class="node ' + cls(to ? to.status : "pending") + '" data-goto="' + esc(e[1]) + '">' + esc(e[1]) + "</button></span>";
      }).join("") + "</div>";
    }
    document.getElementById("p-roadmap").innerHTML = h;
  }

  function renderValidation() {
    var h = "<h2>Section 4 &middot; System validation &amp; suite criteria</h2>" +
      '<p class="sub">Mark Done only when the criterion has actually been executed and observed.</p>';
    h += state.validations.map(function (v) {
      return '<div class="card ' + cls(v.status) + '">' +
        '<div class="card-head"><span class="id">' + esc(v.id) + '</span><span class="t">' + esc(v.title) + "</span>" +
        selectHtml("validation", v.id, v.status) + "</div>" +
        '<p class="desc">' + esc(v.criteria) + "</p>" +
        '<div class="meta"><span class="pill">Covers ' + esc((v.covers || []).join(", ")) + "</span>" +
        '<button class="act" data-notes="' + esc(v.id) + '">' + (v.result ? "Result \\u2713" : "Record result") + "</button></div>" +
        '<div class="notes-row' + (v.result ? " open" : "") + '" data-notes-for="' + esc(v.id) + '">' +
        '<textarea data-note-input="' + esc(v.id) + '" placeholder="Observed output, date run, follow-ups...">' + esc(v.result || "") + "</textarea>" +
        '<button class="act" data-save-note="' + esc(v.id) + '" data-kind="validation">Save result</button></div></div>';
    }).join("");
    document.getElementById("p-validation").innerHTML = h;
  }

  function renderAll() {
    renderHeader(); renderOverview(); renderModules(); renderRoadmap(); renderValidation();
  }

  function showTab(name) {
    tab = name;
    ["overview", "modules", "roadmap", "validation"].forEach(function (n) {
      document.getElementById("p-" + n).hidden = n !== name;
    });
    Array.prototype.forEach.call(document.querySelectorAll("nav button"), function (b) {
      b.setAttribute("aria-selected", String(b.dataset.tab === name));
    });
  }

  async function load() {
    var r = await fetch("/api/state");
    var d = await r.json();
    state = d.state; summary = d.summary;
    renderAll(); showTab(tab);
  }

  async function post(path, body) {
    await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    await load();
  }

  document.addEventListener("click", function (e) {
    var nav = e.target.closest("nav button");
    if (nav) { showTab(nav.dataset.tab); return; }

    var goto = e.target.closest("[data-goto]");
    if (goto) {
      var id = goto.dataset.goto;
      highlight = id; showTab("modules");
      var el = document.getElementById("card-" + id);
      if (el) { el.scrollIntoView({ behavior: "smooth", block: "center" }); el.style.boxShadow = "0 0 0 2px var(--true-color-blue-muted, #54aeff)"; setTimeout(function () { el.style.boxShadow = ""; }, 1600); }
      return;
    }

    var toggle = e.target.closest("[data-notes]");
    if (toggle) {
      var row = document.querySelector('[data-notes-for="' + toggle.dataset.notes + '"]');
      if (row) row.classList.toggle("open");
      return;
    }

    var save = e.target.closest("[data-save-note]");
    if (save) {
      var sid = save.dataset.saveNote;
      var input = document.querySelector('[data-note-input="' + sid + '"]');
      if (!input) return;
      if (save.dataset.kind === "validation") post("/api/validation", { id: sid, result: input.value });
      else post("/api/task", { id: sid, notes: input.value });
      return;
    }
  });

  document.addEventListener("change", function (e) {
    var sel = e.target.closest("select[data-kind]");
    if (!sel) return;
    var kind = sel.dataset.kind, id = sel.dataset.id, status = sel.value;
    if (kind === "task") post("/api/task", { id: id, status: status });
    else if (kind === "constraint") post("/api/constraint", { id: id, status: status });
    else if (kind === "validation") post("/api/validation", { id: id, status: status });
  });

  try {
    var es = new EventSource("/events");
    es.addEventListener("changed", function () { load(); });
  } catch (err) { /* SSE unavailable; manual refresh still works */ }

  load();
})();
</script>
</body>
</html>`;
}
