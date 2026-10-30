"use strict";
// "Pay your team": daily, weekly or monthly payroll for a repo you own. Uses $, api, esc, money, show and
// setError from app.js.

let teamPreview = null;  // last preview split, used to create the schedule
const editsByRun = {};   // run id -> {login: amount}, unsaved

const fmtDate = (iso) => new Date(iso).toLocaleDateString(undefined,
  { day: "numeric", month: "short", year: "numeric" });
const fmtWhen = (iso) => new Date(iso).toLocaleString(undefined,
  { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

// ---------- tabs ----------

function showTab(name) {
  document.querySelectorAll(".tab").forEach((b) => {
    const on = b.dataset.tab === name;
    b.classList.toggle("active", on);
    b.setAttribute("aria-selected", String(on));
  });
  document.querySelectorAll(".tab").forEach((b) => show($(`tab-${b.dataset.tab}`), b.dataset.tab === name));
  history.replaceState(null, "", name === "team" ? location.pathname : `#${name}`);
  if (name === "team") loadSchedules();
  if (name === "causes" && typeof loadCauses === "function") loadCauses();
  if (name === "getpaid" && typeof loadInvoices === "function") loadInvoices();
  if (name === "guide" && typeof loadGuide === "function") loadGuide();
}

document.querySelectorAll(".tab").forEach((b) =>
  b.addEventListener("click", () => showTab(b.dataset.tab)));

// ---------- people list (preview and runs) ----------

function personCard(r, { editable = false, edits = {} } = {}) {
  const v = r.ai_adjust_points || 0;
  const adj = v ? `<span class="${v > 0 ? "adj-up" : "adj-down"}">AI ${v > 0 ? "+" : ""}${v} pts</span>` : "";
  const amount = r.login in edits ? edits[r.login] : r.amount;
  const edited = r.login in edits || r.user_edited;
  const amountHtml = editable
    ? `<input class="amount-input${edited ? " edited" : ""}" type="number" min="0" step="0.01"
         inputmode="decimal" aria-label="Amount for ${esc(r.login)}" data-login="${esc(r.login)}"
         value="${Number(amount).toFixed(2)}">`
    : `<strong class="num">${money(amount)}</strong>`;
  const lines = r.lines_changed == null ? "" : ` · ${Number(r.lines_changed).toLocaleString()} lines changed`;
  return `
    <li class="dep-card">
      <div class="row">
        <div><a class="pkg-name" href="https://github.com/${encodeURIComponent(r.login)}"
                target="_blank" rel="noopener">@${esc(r.login)}</a>
          <span class="pkg-repo">${esc(r.commits)} commit${r.commits === 1 ? "" : "s"}${lines}</span></div>
        ${amountHtml}
      </div>
      <p class="why">${esc(r.explanation)}</p>
      ${r.ai_adjust_reason ? `<div class="muted small">AI: ${esc(r.ai_adjust_reason)} ${adj}</div>` : ""}
      <div class="facts">Paid to ${esc(r.email || "no PayPal email yet")}</div>
    </li>`;
}

// ---------- when to pay ----------

const TZ = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
$("t-tz-name").textContent = TZ;

function fmtTime(hhmm) {
  const [h, m] = hhmm.split(":").map(Number);
  return new Date(2000, 0, 1, h, m).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

function hoursText(h) {
  return h >= 24 && h % 24 === 0 ? `${h / 24} day${h === 24 ? "" : "s"}` : `${h} hour${h === 1 ? "" : "s"}`;
}

// "every day at 9:30 PM", "every Friday at 9:30 PM", "on day 1 of every month at 9:30 PM"
function whenText(freq, day, time) {
  const at = `at ${fmtTime(time)}`;
  if (freq === "daily") return `every day ${at}`;
  if (freq === "weekly") return `every ${WEEKDAYS[day]} ${at}`;
  return `on day ${day} of every month ${at}`;
}

function formWhen() {
  const freq = $("t-freq").value;
  return { freq, day: freq === "weekly" ? Number($("t-weekday").value) : Number($("t-day").value),
           time: $("t-time").value || "00:00", notice: Number($("t-notice").value) };
}

$("t-freq").addEventListener("change", () => {
  const freq = $("t-freq").value;
  show($("t-day-field"), freq === "monthly");
  show($("t-weekday-field"), freq === "weekly");
  $("t-day").required = freq === "monthly";
  if (freq === "daily" && Number($("t-notice").value) >= 24) $("t-notice").value = 1;
  if (teamPreview) renderPreview();
});

// ---------- preview + start ----------

$("team-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  setError($("t-error"), "");
  show($("t-preview"), false);
  $("t-preview-btn").disabled = true;
  show($("t-progress"));
  try {
    teamPreview = await api("/api/payroll/preview", {
      method: "POST",
      body: JSON.stringify({
        repo: $("t-repo").value.trim(), owner_login: $("t-owner").value.trim(),
        total: Number($("t-total").value), top_n: Number($("t-top").value), days: 30,
      }),
    });
    renderPreview();
  } catch (err) {
    setError($("t-error"), err.message);
  } finally {
    show($("t-progress"), false);
    $("t-preview-btn").disabled = false;
  }
});

function renderPreview() {
  const p = teamPreview;
  const w = formWhen();
  show($("t-preview"));
  $("t-preview-title").textContent = `${p.repo}: last 30 days`;
  $("t-preview-meta").textContent = [
    `${p.contributors.length} contributor${p.contributors.length === 1 ? "" : "s"} (excluding @${p.owner_login})`,
    p.explained_by === "llm" ? `AI review (±${p.ai_adjust_limit_points} pts max)` : "rule-based (AI off)",
    p.sandbox ? "sandbox stand-ins receive the money" : "",
  ].filter(Boolean).join(" · ");
  $("t-warnings").innerHTML = (p.warnings || []).map((w) =>
    `<div class="alert warn">${esc(w)}</div>`).join("");
  $("t-people").innerHTML = p.contributors.map((r) => personCard(r)).join("");
  $("t-approve-label").textContent =
    `I approve paying ${money(p.total)} ${whenText(w.freq, w.day, w.time)} (${TZ}) to my top ` +
    `${$("t-top").value} contributors, split by their work. ` +
    (w.notice ? `I'll get a heads-up ${hoursText(w.notice)} before and can skip or edit it.`
              : `I'll get a heads-up ${minEditMinutes} minutes before it pays, to skip or edit it.`);
  $("t-approve").checked = false;
  $("t-start-btn").disabled = true;
}

$("t-approve").addEventListener("change", () => {
  $("t-start-btn").disabled = !$("t-approve").checked || !teamPreview?.contributors.length;
});
["t-day", "t-weekday", "t-time", "t-notice", "t-top", "t-total"].forEach((id) =>
  $(id).addEventListener("input", () => { if (teamPreview) renderPreview(); }));

$("t-start-btn").addEventListener("click", async () => {
  setError($("t-error"), "");
  $("t-start-btn").disabled = true;
  try {
    await api("/api/payroll/schedules", {
      method: "POST",
      body: JSON.stringify({
        repo: teamPreview.repo, owner_login: teamPreview.owner_login,
        total: Number($("t-total").value), frequency: formWhen().freq, pay_day: formWhen().day,
        pay_time: formWhen().time, tz: TZ, top_n: Number($("t-top").value),
        notice_hours: formWhen().notice, approved: true,
      }),
    });
    show($("t-preview"), false);
    teamPreview = null;
    loadSchedules();
  } catch (err) {
    setError($("t-error"), err.message);
    $("t-start-btn").disabled = false;
  }
});

// ---------- schedules ----------

async function loadSchedules() {
  try {
    const list = await api("/api/payroll/schedules");
    $("t-schedules").innerHTML = list.length
      ? list.map(scheduleHtml).join("")
      : '<p class="muted">No schedules yet. Preview a split above to start one.</p>';
    refreshBatches();
  } catch (err) {
    $("t-schedules").innerHTML = `<div class="alert error">${esc(err.message)}</div>`;
  }
}

// ---------- per-person payout status for paid months ----------

const batchCache = {};  // batch id -> latest status from /api/payroll/batches/{id}
const PERSON_STATUS = {
  SUCCESS: ["Received ✅", "SUCCESS"], PENDING: ["Processing", "PENDING"],
  PROCESSING: ["Processing", "PENDING"], ONHOLD: ["On hold", "PENDING"],
  UNCLAIMED: ["Unclaimed", "UNCLAIMED"], RETURNED: ["Returned", "RETURNED"],
  FAILED: ["Failed", "FAILED"], BLOCKED: ["Blocked", "FAILED"], DENIED: ["Denied", "FAILED"],
  REFUNDED: ["Refunded", "RETURNED"], REVERSED: ["Reversed", "RETURNED"],
};
const SETTLED = new Set(["SUCCESS", "UNCLAIMED", "RETURNED", "FAILED", "BLOCKED", "DENIED",
                         "REFUNDED", "REVERSED"]);

function batchHtml(batchId) {
  const b = batchCache[batchId];
  if (!b) return '<li class="muted small">Checking with PayPal…</li>';
  return (b.items || []).map((i) => {
    const [label, cls] = PERSON_STATUS[i.status] || [i.status || "Processing", "PENDING"];
    return `<li><span>@${esc(i.sender_item_id)} <span class="muted small">→ ${esc(i.email)}</span></span>
      <span><span class="num">${money(i.amount)}</span> <span class="status ${cls}">${esc(label)}</span></span></li>`;
  }).join("");
}

let batchTimer = null;
async function refreshBatches() {
  clearTimeout(batchTimer);
  const lists = [...document.querySelectorAll(".people-status[data-batch]")];
  const ids = [...new Set(lists.map((el) => el.dataset.batch))]
    .filter((id) => !batchCache[id] || !batchCache[id].items.every((i) => SETTLED.has(i.status)));
  await Promise.all(ids.map(async (id) => {
    try { batchCache[id] = await api(`/api/payroll/batches/${encodeURIComponent(id)}`); } catch { /* retry later */ }
  }));
  document.querySelectorAll(".people-status[data-batch]").forEach((el) => {
    el.innerHTML = batchHtml(el.dataset.batch);
  });
  // Keep asking PayPal until every person's payment has settled
  const pending = lists.some((el) => {
    const b = batchCache[el.dataset.batch];
    return !b || !b.items.every((i) => SETTLED.has(i.status));
  });
  if (pending && !$("tab-team").hidden) batchTimer = setTimeout(refreshBatches, 8000);
}

function scheduleHtml(s) {
  const run = s.current_run;
  const edits = run ? (editsByRun[run.id] || {}) : {};
  const dirty = Object.keys(edits).length > 0;
  let runHtml = "";
  if (run) {
    const people = run.result.contributors.filter((r) => r.amount > 0 || r.login in edits);
    const total = people.reduce((sum, r) => sum + (r.login in edits ? edits[r.login] : r.amount), 0);
    runHtml = `
      <div class="run-box" data-run="${esc(run.id)}" data-revision="${run.revision}">
        <div class="row">
          <b>${run.status === "paying" ? "Paying now…" : `Heads-up: ${money(total)} pays on ${fmtWhen(run.pay_after)}`}</b>
          <span class="muted small">work from ${fmtDate(run.window_start)} to ${fmtDate(run.window_end)} · version ${run.revision}</span>
        </div>
        ${run.note ? `<div class="alert warn">${esc(run.note)}</div>` : ""}
        ${(run.result.warnings || []).map((w) => `<div class="muted small">${esc(w)}</div>`).join("")}
        <ul class="dep-cards report">${people.map((r) => personCard(r, { editable: run.status === "preview", edits })).join("")}</ul>
        ${run.status === "preview" ? `
        <div class="btn-row">
          <button class="btn" data-act="save" ${dirty ? "" : "disabled"}>Save changes</button>
          <button class="btn ghost" data-act="skip">Skip this payday</button>
          <label class="check small"><input type="checkbox" data-act="paynow-ok" ${dirty ? "disabled" : ""}>
            <span>Pay ${money(total)} now instead of waiting (optional)</span></label>
          <button class="btn primary" data-act="paynow" disabled>Pay now</button>
        </div>
        <div class="small countdown" data-until="${esc(run.pay_after)}">${countdownText(run.pay_after)}</div>` : ""}
      </div>`;
  }
  const past = s.runs.filter((r) => r.status !== "preview" && r.status !== "paying");
  return `
    <div class="schedule" data-schedule="${esc(s.id)}">
      <div class="row">
        <div><b>${esc(s.repo)}</b>
          <div class="muted small">${money(s.total)} ${whenText(s.frequency, s.pay_day, s.pay_time)}
            (${esc(s.tz)}) to the top ${s.top_n} contributors ·
            heads-up ${hoursText(s.notice_minutes != null ? s.notice_minutes / 60 : s.notice_days * 24)} before ·
            excludes @${esc(s.owner_login)}</div></div>
        <div class="btn-row">
          <span class="status ${s.active ? "SUCCESS" : "PENDING"}">${s.active ? "Active" : "Paused"}</span>
          <span class="muted small">Next payday ${fmtWhen(s.next_payday)}</span>
          <button class="btn ghost" data-act="${s.active ? "pause" : "resume"}">${s.active ? "Pause" : "Resume"}</button>
          <button class="btn ghost danger" data-act="delete">Delete</button>
        </div>
      </div>
      ${runHtml}
      ${past.length ? `<ul class="history">${past.map((r, i) => `
        <li class="history-month">
          <div class="row"><span><b>${esc(r.period)}</b></span>
            <span>${money(r.total)} · <span class="status ${r.status === "paid" ? "SUCCESS" : "PENDING"}">${esc(r.status)}</span>
            ${r.batch_id ? `<span class="muted small">PayPal batch ${esc(r.batch_id)}</span>` : ""}</span></div>
          ${r.batch_id && i < 3 ? `<ul class="people-status" data-batch="${esc(r.batch_id)}">${batchHtml(r.batch_id)}</ul>` : ""}
          ${r.note ? `<div class="muted small">${esc(r.note)}</div>` : ""}
        </li>`).join("")}</ul>` : ""}
    </div>`;
}

$("t-schedules").addEventListener("change", (e) => {
  const box = e.target.closest(".run-box");
  if (!box) return;
  const runId = box.dataset.run;
  if (e.target.matches(".amount-input")) {
    const v = Math.round(Number(e.target.value) * 100) / 100;
    if (!Number.isFinite(v) || v < 0) return loadSchedules();
    (editsByRun[runId] ||= {})[e.target.dataset.login] = v;
    loadSchedules();
  } else if (e.target.matches('[data-act="paynow-ok"]')) {
    box.querySelector('[data-act="paynow"]').disabled = !e.target.checked;
  }
});

$("t-schedules").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const sid = btn.closest("[data-schedule]")?.dataset.schedule;
  const box = btn.closest(".run-box");
  const runId = box?.dataset.run;
  btn.disabled = true;
  try {
    if (btn.dataset.act === "delete") {
      if (confirm("Delete this schedule and its history? Payments already made stay paid.")) {
        await api(`/api/payroll/schedules/${sid}`, { method: "DELETE" });
      }
    } else if (btn.dataset.act === "pause" || btn.dataset.act === "resume") {
      await api(`/api/payroll/schedules/${sid}/${btn.dataset.act}`, { method: "POST" });
    } else if (btn.dataset.act === "save") {
      await api(`/api/payroll/runs/${runId}/amounts`, {
        method: "PUT",
        body: JSON.stringify({ revision: Number(box.dataset.revision), amounts: editsByRun[runId] }),
      });
      delete editsByRun[runId];
    } else if (btn.dataset.act === "skip") {
      await api(`/api/payroll/runs/${runId}/skip`, { method: "POST" });
      delete editsByRun[runId];
    } else if (btn.dataset.act === "paynow") {
      await api(`/api/payroll/runs/${runId}/pay-now`, {
        method: "POST",
        body: JSON.stringify({ revision: Number(box.dataset.revision), approved: true }),
      });
    }
  } catch (err) {
    alert(err.message);
  }
  loadSchedules();
});

// ---------- live countdown + auto-refresh ----------

function countdownText(iso) {
  const ms = new Date(iso) - Date.now();
  if (ms <= 0) return "Paying now…";
  const s = Math.floor(ms / 1000), d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600);
  const m = Math.floor(s % 3600 / 60), sec = s % 60;
  const left = d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}:${String(sec).padStart(2, "0")}`;
  return `Pays automatically in ${left}. Edit or skip before then.`;
}

const nudged = new Set();  // runs we've already asked the server to check
setInterval(() => {
  let due = false;
  document.querySelectorAll(".countdown").forEach((el) => {
    el.textContent = countdownText(el.dataset.until);
    if (new Date(el.dataset.until) <= Date.now()) {
      due = true;
      const runId = el.closest(".run-box")?.dataset.run;
      // The server's timer would pay within a minute anyway; with the page open, ask it to
      // check right away. Safe: the check never pays early or twice.
      if (runId && !nudged.has(runId)) {
        nudged.add(runId);
        api("/api/payroll/tick", { method: "POST" }).then(refreshSoon).catch(() => {});
      }
    }
  });
  if (due && !$("tab-team").hidden) refreshSoon();
}, 1000);

// Keep the list current (the server pays on its own); don't redraw while someone is typing
let refreshTimer = null;
function refreshSoon() {
  if (refreshTimer) return;
  refreshTimer = setTimeout(() => {
    refreshTimer = null;
    if (!document.activeElement?.matches(".amount-input")) loadSchedules();
  }, 3000);
}
setInterval(() => { if (!$("tab-team").hidden) refreshSoon(); }, 15000);

let minEditMinutes = 5;
api("/health").then((h) => { minEditMinutes = h.payroll_min_edit_minutes ?? 5; }).catch(() => {});

showTab(["#getpaid", "#causes", "#guide"].includes(location.hash) ? location.hash.slice(1) : "team");
