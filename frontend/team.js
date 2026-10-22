"use strict";
// "Pay your team": monthly payroll for a repo you own. Uses $, api, esc, money, show and
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
  show($("tab-fund"), name === "fund");
  show($("tab-team"), name === "team");
  if (name === "team") {
    history.replaceState(null, "", "#team");
    loadSchedules();
  } else if (location.hash === "#team") {
    history.replaceState(null, "", location.pathname);
  }
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
  const day = Number($("t-day").value), notice = Number($("t-notice").value);
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
    `I approve paying ${money(p.total)} on day ${day} of every month to my top ` +
    `${$("t-top").value} contributors, split by their work. ` +
    (notice ? `I'll get a heads-up ${notice} day${notice === 1 ? "" : "s"} before and can skip or edit it.`
            : "There's no heads-up, but I can still skip or edit before it pays.");
  $("t-approve").checked = false;
  $("t-start-btn").disabled = true;
}

$("t-approve").addEventListener("change", () => {
  $("t-start-btn").disabled = !$("t-approve").checked || !teamPreview?.contributors.length;
});
["t-day", "t-notice", "t-top", "t-total"].forEach((id) =>
  $(id).addEventListener("input", () => { if (teamPreview) renderPreview(); }));

$("t-start-btn").addEventListener("click", async () => {
  setError($("t-error"), "");
  $("t-start-btn").disabled = true;
  try {
    await api("/api/payroll/schedules", {
      method: "POST",
      body: JSON.stringify({
        repo: teamPreview.repo, owner_login: teamPreview.owner_login,
        total: Number($("t-total").value), pay_day: Number($("t-day").value),
        top_n: Number($("t-top").value), notice_days: Number($("t-notice").value),
        approved: true,
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
  } catch (err) {
    $("t-schedules").innerHTML = `<div class="alert error">${esc(err.message)}</div>`;
  }
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
          <b>${run.status === "paying" ? "Paying now…" : `Heads-up: pays ${money(total)} on ${fmtWhen(run.pay_after)}`}</b>
          <span class="muted small">work from ${fmtDate(run.window_start)} to ${fmtDate(run.window_end)} · version ${run.revision}</span>
        </div>
        ${run.note ? `<div class="alert warn">${esc(run.note)}</div>` : ""}
        ${(run.result.warnings || []).map((w) => `<div class="muted small">${esc(w)}</div>`).join("")}
        <ul class="dep-cards report">${people.map((r) => personCard(r, { editable: run.status === "preview", edits })).join("")}</ul>
        ${run.status === "preview" ? `
        <div class="btn-row">
          <button class="btn" data-act="save" ${dirty ? "" : "disabled"}>Save changes</button>
          <button class="btn ghost" data-act="skip">Skip this month</button>
          <label class="check small"><input type="checkbox" data-act="paynow-ok" ${dirty ? "disabled" : ""}>
            <span>Pay ${money(total)} now instead of waiting</span></label>
          <button class="btn primary" data-act="paynow" disabled>Pay now</button>
        </div>
        <div class="muted small">If you do nothing, it pays automatically on ${fmtWhen(run.pay_after)}.</div>` : ""}
      </div>`;
  }
  const past = s.runs.filter((r) => r.status !== "preview" && r.status !== "paying");
  return `
    <div class="schedule" data-schedule="${esc(s.id)}">
      <div class="row">
        <div><b>${esc(s.repo)}</b>
          <div class="muted small">${money(s.total)} a month to the top ${s.top_n} contributors ·
            day ${s.pay_day} · heads-up ${s.notice_days} day${s.notice_days === 1 ? "" : "s"} before ·
            excludes @${esc(s.owner_login)}</div></div>
        <div class="btn-row">
          <span class="status ${s.active ? "SUCCESS" : "PENDING"}">${s.active ? "Active" : "Paused"}</span>
          <span class="muted small">Next payday ${fmtDate(s.next_payday)}</span>
          <button class="btn ghost" data-act="${s.active ? "pause" : "resume"}">${s.active ? "Pause" : "Resume"}</button>
        </div>
      </div>
      ${runHtml}
      ${past.length ? `<ul class="history">${past.map((r) => `
        <li><span>${esc(r.period)}</span>
          <span>${money(r.total)} · <span class="status ${r.status === "paid" ? "SUCCESS" : "PENDING"}">${esc(r.status)}</span>
          ${r.batch_id ? `<span class="muted small">batch ${esc(r.batch_id)}</span>` : ""}</span></li>`).join("")}</ul>` : ""}
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
    if (btn.dataset.act === "pause" || btn.dataset.act === "resume") {
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
  loadEvents();
});

$("t-tick-btn").addEventListener("click", async () => {
  $("t-tick-btn").disabled = true;
  try {
    const res = await api("/api/payroll/tick", { method: "POST" });
    $("t-tick-note").textContent = res.actions.length
      ? `Done: ${res.actions.join("; ")}.`
      : "Checked: nothing is due today. (This check also runs automatically every day.)";
    loadSchedules();
    loadEvents();
  } catch (err) {
    $("t-tick-note").textContent = err.message;
  } finally {
    $("t-tick-btn").disabled = false;
  }
});

if (location.hash === "#team") showTab("team");
