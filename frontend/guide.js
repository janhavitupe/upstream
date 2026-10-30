"use strict";
// "Testing guide": sandbox logins and live ticks for each task. Uses $, api, esc and show from app.js.

function loginRow(a) {
  return `
    <li class="login">
      <div><b>${esc(a.role)}</b><div class="muted small">${esc(a.check)}</div></div>
      <div class="login-value"><code>${esc(a.email)}</code>
        <button class="btn ghost" data-copy="${esc(a.email)}" type="button">Copy</button></div>
      <div class="login-value"><code>${esc(a.password)}</code>
        <button class="btn ghost" data-copy="${esc(a.password)}" type="button">Copy</button></div>
    </li>`;
}

async function loadGuide() {
  try {
    const g = await api("/api/guide");
    $("guide-logins").innerHTML = g.logins.length ? g.logins.map(loginRow).join("")
      : '<li class="muted">Logins are only shown in the sandbox.</li>';
    const tasks = [...document.querySelectorAll("[data-task]")];
    tasks.forEach((li) => li.classList.toggle("done", !!g.tasks[li.dataset.task]));
    const done = tasks.filter((li) => li.classList.contains("done")).length;
    $("guide-count").textContent = `${done} of ${tasks.length} done`;
    $("guide-count").className = `status ${done === tasks.length ? "SUCCESS" : "PENDING"}`;
    $("guide-bar").style.width = `${Math.round(100 * done / tasks.length)}%`;
  } catch { /* keep the last view */ }
}

$("tab-guide").addEventListener("click", async (e) => {
  const copy = e.target.closest("[data-copy]");
  if (copy) {
    await navigator.clipboard.writeText(copy.dataset.copy).catch(() => {});
    copy.textContent = "Copied ✓";
    setTimeout(() => { copy.textContent = "Copy"; }, 1500);
  }
  const link = e.target.closest("[data-goto]");
  if (link) showTab(link.dataset.goto);
});

setInterval(() => { if (!$("tab-guide").hidden) loadGuide(); }, 15000);
