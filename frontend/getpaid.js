"use strict";
// "Get paid": billing chat + invoice list. Uses $, api, esc, money and show from app.js.

const chatHistory = [];  // [{role, content}] sent back to the server each turn
let chatPending = null;  // what the last reply was waiting for (e.g. a client's email)

const fmtDay = (iso) => new Date(`${iso}T00:00:00`).toLocaleDateString(undefined,
  { day: "numeric", month: "short" });
const todayIso = () => new Date().toISOString().slice(0, 10);
const OPEN_STATUSES = new Set(["SENT", "UNPAID", "PARTIALLY_PAID", "SCHEDULED"]);

function invStatus(inv) {
  if (OPEN_STATUSES.has(inv.status) && inv.due_date < todayIso()) return "OVERDUE";
  return inv.status;
}

function addMsg(role, html) {
  const el = document.createElement("div");
  el.className = `msg ${role}`;
  el.innerHTML = html;
  $("chat-log").appendChild(el);
  $("chat-log").scrollTop = $("chat-log").scrollHeight;
  return el;
}

function invoiceCard(d, type) {
  const cancelled = type === "invoice_cancelled" || d.status === "CANCELLED";
  const items = (d.items || []).map((i) =>
    `<li>${esc(i.name)}${i.quantity && i.quantity !== 1 ? ` × ${i.quantity}` : ""} · ${money(i.quantity * i.unit_price)}</li>`).join("");
  return `
    <div class="inv-card ${cancelled ? "cancelled" : ""}" data-invoice="${esc(d.id)}">
      <div class="row">
        <div><b>Invoice #${esc(d.number)}</b> · ${esc(d.client_name || d.client_email)}
          <div class="muted small">${esc(d.client_email)} · due ${fmtDay(d.due_date)}</div></div>
        <span class="amount-big">${esc(d.currency === "USD" ? "" : `${d.currency} `)}${money(d.amount)}</span>
      </div>
      ${items ? `<ul>${items}</ul>` : ""}
      <div class="btn-row">
        <span class="status ${cancelled ? "CANCELLED" : "SENT"}">${cancelled ? "Cancelled" : "Sent ✓"}</span>
        ${cancelled ? "" : `<button class="btn ghost" data-act="undo">Undo</button>`}
        ${d.payer_url && !cancelled ? `<a class="btn ghost" href="${esc(d.payer_url)}" target="_blank" rel="noopener">Client's pay page</a>` : ""}
      </div>
    </div>`;
}

async function sendChat(text) {
  text = text.trim();
  if (!text) return;
  addMsg("user", esc(text));
  $("chat-input").value = "";
  $("chat-send").disabled = true;
  const typing = addMsg("bot typing", "Working on it…");
  try {
    const res = await api("/api/getpaid/chat", {
      method: "POST",
      body: JSON.stringify({ message: text, history: chatHistory, pending: chatPending }),
    });
    chatPending = res.pending || null;
    typing.remove();
    chatHistory.push({ role: "user", content: text }, { role: "assistant", content: res.reply });
    addMsg("bot", esc(res.reply));
    for (const a of res.actions) {
      if (a.type === "invoice_sent" || a.type === "invoice_cancelled") {
        $("chat-log").insertAdjacentHTML("beforeend", invoiceCard(a.data, a.type));
      } else if (a.type === "client_saved") {
        addMsg("bot", `<span class="muted small">Saved ${esc(a.data.name)} (${esc(a.data.email)}) as a client.</span>`);
      } else if (a.type === "chasing_changed") {
        const who = a.data.client || "everyone";
        addMsg("bot", `<span class="muted small">Automatic reminders ${a.data.chasing ? "on" : "off"} for ${esc(who)}.</span>`);
      }
    }
    $("chat-log").scrollTop = $("chat-log").scrollHeight;
    if (res.actions.length) loadInvoices();
  } catch (err) {
    typing.remove();
    addMsg("bot", `Sorry, that failed: ${esc(err.message)}`);
  } finally {
    $("chat-send").disabled = false;
    $("chat-input").focus();
  }
}

$("chat-form").addEventListener("submit", (e) => { e.preventDefault(); sendChat($("chat-input").value); });
$("chat-chips").addEventListener("click", (e) => {
  const chip = e.target.closest(".chip");
  if (chip) sendChat(chip.textContent);
});

async function undo(invoiceId, btn) {
  btn.disabled = true;
  try {
    await api(`/api/getpaid/invoices/${encodeURIComponent(invoiceId)}/cancel`, { method: "POST" });
    document.querySelectorAll(`.inv-card[data-invoice="${CSS.escape(invoiceId)}"]`).forEach((c) => {
      c.classList.add("cancelled");
      c.querySelector(".status").className = "status CANCELLED";
      c.querySelector(".status").textContent = "Cancelled";
      c.querySelectorAll("[data-act='undo'], a.btn").forEach((x) => x.remove());
    });
    loadInvoices();
  } catch (err) {
    btn.disabled = false;
    alert(err.message);
  }
}

$("chat-log").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-act='undo']");
  if (btn) undo(btn.closest("[data-invoice]").dataset.invoice, btn);
});

// ---------- invoice list ----------

async function loadInvoices() {
  try {
    const rows = await api("/api/getpaid/invoices");
    const open = rows.filter((r) => OPEN_STATUSES.has(r.status));
    const owed = open.reduce((s, r) => s + r.amount, 0);
    $("inv-summary").textContent = open.length ? `${money(owed)} owed across ${open.length}` : "";
    $("inv-list").innerHTML = rows.length ? rows.map((r) => {
      const st = invStatus(r);
      const label = { OVERDUE: "Overdue", SENT: "Sent", UNPAID: "Unpaid", PAID: "Paid ✅",
                      MARKED_AS_PAID: "Paid ✅", CANCELLED: "Cancelled" }[st] || st;
      return `
        <li class="inv-row" data-invoice="${esc(r.id)}">
          <div class="row"><b>#${esc(r.number)} · ${esc(r.client_name || r.client_email)}</b>
            <span class="num">${money(r.amount)}</span></div>
          <div class="row"><span class="muted small">due ${fmtDay(r.due_date)}${r.chase_stage
            ? ` · ${r.chase_stage} reminder${r.chase_stage === 1 ? "" : "s"} sent` : ""}</span>
            <span class="status ${esc(st)}">${esc(label)}</span></div>
        </li>`;
    }).join("") : '<li class="muted">No invoices yet. Ask the chat to bill someone.</li>';
  } catch { /* keep the last list */ }
}
