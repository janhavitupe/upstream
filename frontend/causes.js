"use strict";
// "Verified causes": start a campaign from a hospital bill, see the checks, share the page.
// Uses $, api, esc, money and show from app.js.

const VERDICT = { verified: ["Verified: donations open", "SUCCESS"],
  needs_review: ["Needs review: donations closed", "PENDING"],
  unreadable: ["Bill unreadable: donations closed", "PENDING"] };

function checksHtml(checks) {
  return `<ul class="checks">${checks.map((ch) => `<li>${ch.ok ? "✅" : "⚠️"} <b>${esc(ch.label)}</b>
    <span class="muted small">${esc(ch.detail)}</span></li>`).join("")}</ul>`;
}

function campaignCard(c) {
  const [label, cls] = c.status === "closed" ? ["Closed", "CANCELLED"] : VERDICT[c.verdict];
  const pct = c.goal ? Math.min(100, Math.round(100 * c.raised / c.goal)) : 0;
  return `
    <li class="campaign" data-campaign="${esc(c.id)}">
      <div class="row"><div><b>${esc(c.title)}</b>
        <div class="muted small">For ${esc(c.patient_name)} · paid to ${esc(c.provider_name)} (${esc(c.provider_email)})</div></div>
        <span class="status ${cls}">${esc(label)}</span></div>
      <div class="progress-bar"><span style="width:${pct}%"></span></div>
      <div class="muted small">${money(c.raised)} raised of ${money(c.goal)} · ${c.donors} donation${c.donors === 1 ? "" : "s"}</div>
      <details ${c.verdict !== "verified" ? "open" : ""}><summary class="small">Bill checks</summary>${checksHtml(c.checks)}</details>
      <div class="btn-row">
        <a class="btn ghost" href="${esc(c.page_url)}" target="_blank" rel="noopener">Public page</a>
        <button class="btn ghost" data-cact="copy" data-url="${esc(c.page_url)}">Copy link</button>
        ${c.verdict !== "verified" && c.status === "open" && !c.donors ? `
          <label class="btn ghost">Upload a corrected bill<input type="file" accept="image/*,application/pdf" data-cact="rebill" hidden></label>` : ""}
        ${c.status === "open" ? '<button class="btn ghost danger" data-cact="close">Close</button>' : ""}
      </div>
    </li>`;
}

async function loadCauses() {
  try {
    const rows = await api("/api/causes");
    $("cause-list").innerHTML = rows.length ? rows.map(campaignCard).join("")
      : '<li class="muted">No campaigns yet.</li>';
  } catch { /* keep the last view */ }
}

$("cause-example").addEventListener("click", () => {
  const f = $("cause-form").elements;
  f.title.value = "Help Sam with an emergency surgery bill";
  f.story.value = "Sam needed emergency surgery and the hospital bill is more than the family can pay.";
  f.patient_name.value = "Sam Lee";
  f.provider_name.value = "City General Hospital";
  f.goal.value = 5000;
});

$("cause-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target;
  setError($("cause-error"), "");
  $("cause-submit").disabled = true;
  show($("cause-progress"));
  try {
    const res = await fetch("/api/causes", { method: "POST", body: new FormData(form) });
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || res.statusText);
    const [label] = VERDICT[body.verdict];
    $("cause-result").innerHTML = `<div class="alert ${body.verdict === "verified" ? "good" : "warn"}">
      <b>${esc(label)}.</b> ${body.verdict === "verified"
        ? `Share the <a href="${esc(body.page_url)}" target="_blank" rel="noopener">public page</a>: every donation goes straight to ${esc(body.provider_name)}.`
        : "Fix what's flagged below, then upload a corrected bill from the campaign list."}</div>${checksHtml(body.checks)}`;
    form.reset();
    $("cause-hospital-email").value = $("cause-hospital-email").dataset.default || "";
    loadCauses();
  } catch (err) {
    setError($("cause-error"), err.message);
  } finally {
    show($("cause-progress"), false);
    $("cause-submit").disabled = false;
  }
});

$("cause-list").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-cact]");
  if (!btn) return;
  const id = btn.closest("[data-campaign]").dataset.campaign;
  if (btn.dataset.cact === "copy") {
    await navigator.clipboard.writeText(btn.dataset.url).catch(() => {});
    btn.textContent = "Copied ✓";
  } else if (btn.dataset.cact === "close" && confirm("Close this campaign? It stops taking donations.")) {
    await api(`/api/causes/${encodeURIComponent(id)}/close`, { method: "POST" }).catch((err) => alert(err.message));
    loadCauses();
  }
});

$("cause-list").addEventListener("change", async (e) => {
  const input = e.target.closest("input[data-cact='rebill']");
  if (!input || !input.files.length) return;
  const id = input.closest("[data-campaign]").dataset.campaign;
  const data = new FormData();
  data.append("bill", input.files[0]);
  input.closest("label").firstChild.textContent = "Reading the bill… ";
  const res = await fetch(`/api/causes/${encodeURIComponent(id)}/bill`, { method: "POST", body: data });
  if (!res.ok) alert((await res.json()).detail);
  loadCauses();
});

loadCauses();

api("/health").then((h) => {
  const input = $("cause-hospital-email");
  input.dataset.default = h.demo_hospital || "";
  if (!input.value) input.value = input.dataset.default;
}).catch(() => {});
