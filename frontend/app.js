"use strict";

const $ = (id) => document.getElementById(id);
const money = (n) => "$" + Number(n || 0).toFixed(2);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const FINAL_ITEM = new Set(["SUCCESS", "FAILED", "RETURNED", "BLOCKED", "REFUNDED", "REVERSED",
                            "DENIED", "UNCLAIMED"]);

let analysis = null;   // latest analysis from the server
let edits = {};        // package -> amount, unsaved
let grid = null;
let pollTimer = null;

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    let detail = body.detail ?? body.error ?? res.statusText;
    if (Array.isArray(detail)) detail = detail.map((d) => d.msg).join("; ");
    if (typeof detail === "object") detail = detail.message || JSON.stringify(detail);
    throw new Error(detail);
  }
  return body;
}

function show(el, on = true) { el.hidden = !on; }
function setError(el, msg) { el.textContent = msg || ""; show(el, !!msg); }

// ---------- status pills ----------

async function loadHealth() {
  try {
    const h = await api("/health");
    const pills = [
      [h.paypal_configured ? "PayPal sandbox" : "PayPal not configured", h.paypal_configured],
      [h.llm ? `AI: ${h.llm.split("/").pop()}` : "AI off (rules only)", !!h.llm],
      [h.webhook_verification ? "Webhooks verified" : "Webhooks not verified",
       h.webhook_verification],
    ];
    $("status-pills").innerHTML = pills.map(([label, on]) =>
      `<span class="pill ${on ? "on" : "off"}">${esc(label)}</span>`).join("");
  } catch { /* the page still works without the pills */ }
}

// ---------- analyze ----------

const PROGRESS = ["Reading dependencies…", "Scanning which files import each one…",
                  "Checking each project's health on GitHub…", "Scoring and splitting the budget…",
                  "AI is reviewing the split…"];

$("analyze-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  setError($("analyze-error"), "");
  const body = {
    repo: $("repo").value.trim(),
    budget: Number($("budget").value),
    max_deps: Number($("max-deps").value),
  };
  $("analyze-btn").disabled = true;
  show($("analyze-progress"));
  let step = 0;
  $("progress-text").textContent = PROGRESS[0];
  const ticker = setInterval(() => {
    step = Math.min(step + 1, PROGRESS.length - 1);
    $("progress-text").textContent = PROGRESS[step];
  }, 5000);
  try {
    const result = await api("/api/oss/analyze", { method: "POST", body: JSON.stringify(body) });
    setAnalysis({ ...result, payout: null });
    loadHistory();
  } catch (err) {
    setError($("analyze-error"), err.message);
  } finally {
    clearInterval(ticker);
    show($("analyze-progress"), false);
    $("analyze-btn").disabled = false;
  }
});

async function openAnalysis(id) {
  try {
    setAnalysis(await api(`/api/oss/analyses/${encodeURIComponent(id)}`));
  } catch (err) {
    setError($("analyze-error"), err.message);
  }
}

function setAnalysis(a) {
  analysis = a;
  edits = {};
  history.replaceState(null, "", `#a=${a.analysis_id}`);
  show($("result-card"));
  $("result-title").textContent = a.repo;
  const bits = [`${a.dependencies.length} dependencies from ${a.manifest}`,
                `version ${a.revision}`,
                a.explained_by === "llm"
                  ? `AI review (±${a.ai_adjust_limit_points} pts max)`
                  : "rule-based reasons (AI off)"];
  $("result-meta").textContent = bits.join(" · ");
  $("warnings").innerHTML = (a.warnings || []).map((w) =>
    `<div class="alert warn">${esc(w)}</div>`).join("");
  renderGrid();
  updateTotals();
  setError($("action-error"), "");
  if (a.payout) {
    showPayout(a.payout.batch_id);
  } else {
    show($("payout-card"), false);
    stopPolling();
  }
}

// ---------- grid ----------

function riskBadge(r) {
  if (r.error || !r.signals) return '<span class="badge none">No source</span>';
  if (r.risk >= 0.6) return '<span class="badge high">At risk</span>';
  if (r.risk >= 0.3) return '<span class="badge mid">Watch</span>';
  return '<span class="badge low">Healthy</span>';
}

function monthsAgo(iso) {
  if (!iso) return null;
  return Math.max(0, Math.floor((Date.now() - new Date(iso)) / (30.4 * 864e5)));
}

function isPaid() { return !!(analysis && analysis.payout); }

function currentAmount(r) {
  return r.package in edits ? edits[r.package] : r.amount;
}

function columnDefs() {
  // What matters first (who, how healthy, how much, why); supporting detail after
  return [
    {
      headerName: "Package", field: "package", width: 135,
      pinned: matchMedia("(min-width: 721px)").matches ? "left" : null,
      cellRenderer: (p) => {
        const r = p.data;
        const repo = r.signals?.html_url
          ? `<a class="pkg-repo" href="${esc(r.signals.html_url)}" target="_blank" rel="noopener">${esc(r.source_repo)}</a>`
          : `<span class="pkg-repo">${esc(r.error || "")}</span>`;
        return `<span class="pkg-name">${esc(r.package)}</span>${repo}`;
      },
      wrapText: true, autoHeight: true,
    },
    { headerName: "Health", width: 100, valueGetter: (p) => p.data.risk ?? -1,
      cellRenderer: (p) => riskBadge(p.data),
      tooltipValueGetter: (p) => p.data.signals
        ? `Risk score ${p.data.risk.toFixed(2)} of 1.00: ${(p.data.health_notes || []).join("; ") || "no risk signals"}`
        : null },
    {
      headerName: "Amount", width: 95, type: "numericColumn",
      editable: (p) => !isPaid() && !!p.data.signals,
      valueGetter: (p) => currentAmount(p.data),
      valueSetter: (p) => {
        const v = Math.round(Number(p.newValue) * 100) / 100;
        if (!Number.isFinite(v) || v < 0) return false;
        if (v === p.data.amount) delete edits[p.data.package];
        else edits[p.data.package] = v;
        return true;
      },
      cellEditor: "agNumberCellEditor",
      cellEditorParams: { min: 0, precision: 2, step: 1 },
      cellRenderer: (p) => `<span class="amount num">${money(p.value)}</span>`,
      cellClass: (p) => [
        !isPaid() && p.data.signals ? "editable" : "",
        p.data.package in edits || p.data.user_edited ? "edited" : "",
      ],
    },
    {
      headerName: "AI change", width: 105, type: "numericColumn",
      valueGetter: (p) => p.data.ai_adjust_points ?? 0,
      cellRenderer: (p) => {
        const v = p.value;
        if (!v) return '<span class="muted">0</span>';
        return `<span class="${v > 0 ? "adj-up" : "adj-down"}">${v > 0 ? "+" : ""}${v} pts</span>`;
      },
      tooltipValueGetter: (p) => p.data.ai_adjust_reason || null,
    },
    {
      headerName: "Why", field: "explanation", flex: 1, minWidth: 300,
      wrapText: true, autoHeight: true,
      cellRenderer: (p) => {
        const reason = p.data.ai_adjust_reason
          ? `<div class="muted small">AI: ${esc(p.data.ai_adjust_reason)}</div>` : "";
        return `${esc(p.value)}${reason}`;
      },
    },
    {
      headerName: "Work (1y)", width: 165,
      valueGetter: (p) => p.data.work_score ?? null,
      valueFormatter: (p) => {
        const s = p.data.signals;
        if (!s) return "–";
        const c = `${s.human_commits_last_year ?? 0}${s.commits_capped ? "+" : ""}`;
        return `${c} commits · ${s.releases_last_year ?? 0} rel.`;
      },
    },
    {
      headerName: "Code size", width: 110, type: "numericColumn",
      valueGetter: (p) => p.data.signals?.code_kb ?? null,
      valueFormatter: (p) => p.value == null ? "–" : `${Math.round(p.value).toLocaleString()} KB`,
    },
    {
      headerName: "Committers (1y)", width: 135, type: "numericColumn",
      valueGetter: (p) => p.data.signals?.active_maintainers ?? null,
      valueFormatter: (p) => p.value == null ? "–"
        : (p.data.signals.commits_capped ? `${p.value}+` : String(p.value)),
    },
    {
      headerName: "Last release", width: 120,
      valueGetter: (p) => monthsAgo(p.data.signals?.last_release),
      valueFormatter: (p) => p.value == null ? "none" : `${p.value} mo ago`,
    },
    { headerName: "Used in files", field: "import_count", width: 115, type: "numericColumn" },
    {
      headerName: "Funding", width: 170,
      valueGetter: (p) => (p.data.signals?.funding_platforms || []).join(", "),
      valueFormatter: (p) => p.value || (p.data.signals ? "none found" : "–"),
      tooltipValueGetter: (p) => p.data.signals?.funding_source === "org"
        ? "From the organization-wide FUNDING.yml" : null,
    },
    {
      headerName: "Rule split", width: 105, type: "numericColumn",
      valueGetter: (p) => p.data.rule_amount ?? p.data.amount,
      valueFormatter: (p) => money(p.value),
    },
    {
      headerName: "Pay to", field: "suggested_email", width: 240,
      valueFormatter: (p) => currentAmount(p.data) > 0 ? (p.value || "needs email") : "–",
    },
  ];
}

function gridTheme() {
  const dark = document.documentElement.dataset.theme === "dark" ||
    (!document.documentElement.dataset.theme &&
     matchMedia("(prefers-color-scheme: dark)").matches);
  $("grid").className = `grid ${dark ? "ag-theme-quartz-dark" : "ag-theme-quartz"}`;
}

function renderCards() {
  const paid = isPaid();
  $("cards").innerHTML = analysis.dependencies.map((r) => {
    const v = r.ai_adjust_points || 0;
    const adj = v ? `<span class="${v > 0 ? "adj-up" : "adj-down"}">AI ${v > 0 ? "+" : ""}${v} pts</span>` : "";
    const edited = r.package in edits || r.user_edited;
    const s = r.signals || {};
    const facts = r.signals ? [
      `${s.human_commits_last_year ?? 0}${s.commits_capped ? "+" : ""} commits · ${s.releases_last_year ?? 0} releases (1y)`,
      `${Math.round(s.code_kb || 0).toLocaleString()} KB of code`,
      `${s.active_maintainers}${s.commits_capped ? "+" : ""} committers`,
      `used in ${r.import_count} file${r.import_count === 1 ? "" : "s"}`,
    ].join(" · ") : esc(r.error || "");
    return `
      <li class="dep-card">
        <div class="row">
          <div><span class="pkg-name">${esc(r.package)}</span>
            <span class="pkg-repo">${esc(r.source_repo || "")}</span></div>
          <input class="amount-input${edited ? " edited" : ""}" type="number" min="0" step="0.01"
                 inputmode="decimal" aria-label="Amount for ${esc(r.package)}"
                 data-pkg="${esc(r.package)}" value="${currentAmount(r).toFixed(2)}"
                 ${paid || !r.signals ? "disabled" : ""}>
        </div>
        <div class="row" style="margin-top:6px">${riskBadge(r)} ${adj}</div>
        <p class="why">${esc(r.explanation)}</p>
        ${r.ai_adjust_reason ? `<div class="muted small">AI: ${esc(r.ai_adjust_reason)}</div>` : ""}
        <div class="facts">${facts}</div>
      </li>`;
  }).join("");
}

$("cards").addEventListener("change", (e) => {
  const input = e.target.closest(".amount-input");
  if (!input) return;
  const dep = analysis.dependencies.find((d) => d.package === input.dataset.pkg);
  const v = Math.round(Number(input.value) * 100) / 100;
  if (!Number.isFinite(v) || v < 0) { input.value = currentAmount(dep).toFixed(2); return; }
  if (v === dep.amount) delete edits[dep.package];
  else edits[dep.package] = v;
  grid?.refreshCells({ force: true });
  renderCards();
  updateTotals();
});

function renderGrid() {
  gridTheme();
  renderCards();
  const rows = analysis.dependencies.map((d) => ({ ...d }));
  if (grid) {
    grid.setGridOption("rowData", rows);
    grid.refreshCells({ force: true });
    return;
  }
  grid = agGrid.createGrid($("grid"), {
    columnDefs: columnDefs(),
    rowData: rows,
    domLayout: "autoHeight",
    defaultColDef: { sortable: true, resizable: true, suppressMovable: true },
    singleClickEdit: true,
    // autoHeight needs every column rendered to measure rows correctly
    suppressColumnVirtualisation: true,
    stopEditingWhenCellsLoseFocus: true,
    tooltipShowDelay: 200,
    getRowId: (p) => p.data.package,
    onCellValueChanged: () => { grid.refreshCells({ force: true }); renderCards(); updateTotals(); },
  });
}

matchMedia("(prefers-color-scheme: dark)").addEventListener("change", gridTheme);

// ---------- totals, save, approve ----------

function payable() {
  return analysis.dependencies
    .map((d) => ({ ...d, amount: currentAmount(d) }))
    .filter((d) => d.amount > 0);
}

function updateTotals() {
  const lines = payable();
  const total = lines.reduce((s, d) => s + d.amount, 0);
  const dirty = Object.keys(edits).length > 0;
  $("total-amount").textContent = money(total);
  $("total-note").textContent = Math.abs(total - analysis.budget) > 0.004
    ? `budget was ${money(analysis.budget)}` : "";
  $("save-btn").disabled = !dirty || isPaid();
  $("discard-btn").disabled = !dirty || isPaid();
  $("edit-status").textContent = dirty
    ? `${Object.keys(edits).length} unsaved change(s). Save before approving.` : "";
  $("approve-label").textContent =
    `I approve sending ${money(total)} to ${lines.length} maintainer${lines.length === 1 ? "" : "s"} via PayPal.`;
  show($("approve-box"), !isPaid());
  $("approve-check").disabled = dirty;
  if (dirty) $("approve-check").checked = false;
  $("approve-btn").disabled = dirty || !$("approve-check").checked || !lines.length;
}

$("approve-check").addEventListener("change", updateTotals);

$("discard-btn").addEventListener("click", () => {
  edits = {};
  renderGrid();
  updateTotals();
});

$("save-btn").addEventListener("click", async () => {
  setError($("action-error"), "");
  $("save-btn").disabled = true;
  try {
    const saved = await api(`/api/oss/analyses/${analysis.analysis_id}/allocation`, {
      method: "PUT",
      body: JSON.stringify({ revision: analysis.revision, amounts: edits }),
    });
    setAnalysis(saved);
    $("edit-status").textContent = `Saved as version ${saved.revision}.`;
    loadHistory();
  } catch (err) {
    setError($("action-error"), err.message);
    updateTotals();
  }
});

$("approve-btn").addEventListener("click", async () => {
  setError($("action-error"), "");
  $("approve-btn").disabled = true;
  $("approve-btn").textContent = "Sending…";
  try {
    const res = await api("/api/oss/payout", {
      method: "POST",
      body: JSON.stringify({
        analysis_id: analysis.analysis_id, revision: analysis.revision, approved: true,
      }),
    });
    analysis.payout = { batch_id: res.batch.payout_batch_id };
    renderGrid();
    updateTotals();
    showPayout(res.batch.payout_batch_id);
    loadHistory();
  } catch (err) {
    setError($("action-error"), err.message);
    updateTotals();
  } finally {
    $("approve-btn").textContent = "Approve & pay";
    $("approve-check").checked = false;
  }
});

// ---------- payout status ----------

function stopPolling() { clearTimeout(pollTimer); pollTimer = null; }

async function showPayout(batchId) {
  show($("payout-card"));
  stopPolling();
  try {
    const b = await api(`/api/oss/payout/${encodeURIComponent(batchId)}`);
    $("payout-meta").textContent =
      `Batch ${b.batch_id} · ${b.status || "?"} · ${money(b.total ?? 0)}`;
    $("payout-items").innerHTML = (b.items || []).map((i) => `
      <li>
        <div class="ellipsis"><b>${esc(i.sender_item_id)}</b>
          <span class="muted small">${esc(i.email)}</span></div>
        <div><span class="num">${money(i.amount)}</span>
          <span class="status ${esc(i.status || "PENDING")}">${esc(i.status || "PENDING")}</span></div>
      </li>`).join("");
    const done = (b.items || []).every((i) => FINAL_ITEM.has(i.status));
    if (!done) pollTimer = setTimeout(() => showPayout(batchId), 8000);
  } catch (err) {
    $("payout-meta").textContent = `Couldn't load status: ${err.message}`;
  }
}

$("refresh-btn").addEventListener("click", () => {
  if (analysis?.payout) showPayout(analysis.payout.batch_id);
  loadEvents();
});

// ---------- history + events ----------

async function loadHistory() {
  try {
    const rows = await api("/api/oss/analyses");
    if (!rows.length) return;
    $("history").innerHTML = rows.map((r) => `
      <li>
        <button class="link ellipsis" data-id="${esc(r.analysis_id)}">${esc(r.repo)}</button>
        <span class="muted small">${money(r.budget)} ·
          ${r.batch_id ? `<span class="status ${esc(r.batch_status)}">${esc(r.batch_status || "PAID")}</span>`
                       : "not paid"}</span>
      </li>`).join("");
  } catch { /* ignore */ }
}

$("history").addEventListener("click", (e) => {
  const id = e.target.closest("button[data-id]")?.dataset.id;
  if (id) openAnalysis(id);
});

async function loadEvents() {
  try {
    const rows = await api("/api/events");
    if (!rows.length) return;
    $("events").innerHTML = rows.slice(0, 8).map((ev) => `
      <li>
        <div class="ellipsis">${esc(ev.message || ev.event_type)}
          <span class="muted small">${esc(ev.event_type)}</span></div>
        <span class="muted small">${ev.verified === true ? "verified" : ev.verified === false ? "unverified" : ""}</span>
      </li>`).join("");
  } catch { /* ignore */ }
}

// ---------- boot ----------

loadHealth();
loadHistory();
loadEvents();
setInterval(loadEvents, 15000);
const fromHash = new URLSearchParams(location.hash.slice(1)).get("a");
if (fromHash) openAnalysis(fromHash);
