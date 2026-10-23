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

// ---------- invoice list, chasing switches, demo clock ----------

function chaseNote(r) {
  if (!OPEN_STATUSES.has(r.status)) {
    return r.chase_stage ? `${r.chase_stage} reminder${r.chase_stage === 1 ? "" : "s"} sent` : "";
  }
  const parts = [];
  if (r.days_overdue > 0) parts.push(`<span class="chase-note overdue">${r.days_overdue} day${r.days_overdue === 1 ? "" : "s"} overdue</span>`);
  if (r.chase_stage) parts.push(`${r.chase_stage} reminder${r.chase_stage === 1 ? "" : "s"} sent`);
  if (r.chasing === "on" && r.next_reminder) parts.push(`next: ${r.next_reminder.tone} reminder ${fmtDay(r.next_reminder.on)}`);
  else if (r.chasing && r.chasing !== "on") parts.push(`chasing ${r.chasing}`);
  return parts.join(" · ");
}

let gpState = null;  // last /api/getpaid/state
let clockBusy = false;

const openPanels = new Set();  // invoice ids whose Options panel is open
const panelData = {};          // id -> {draft, trail, partial, error}: survives auto-refresh

function seenLine(r) {
  const who = esc(r.client_name || "The client");
  if (r.viewed === 0) return `<div class="alert warn">${who} has never opened the invoice emails. The address may be wrong or unread: try reaching them another way.</div>`;
  if (r.viewed === 1) return `<div class="muted small">${who} has opened the invoice, so they know about it.</div>`;
  return "";
}

function panelHtml(r) {
  const d = panelData[r.id] || {};
  const owed = r.owed ?? r.amount;
  const mailto = d.draft ? `mailto:${encodeURIComponent(r.client_email)}?subject=${encodeURIComponent(`Invoice #${r.number}`)}&body=${encodeURIComponent(d.draft)}` : "";
  return `
    <div class="inv-panel">
      ${seenLine(r)}
      <div class="btn-row">
        <button class="btn ghost" data-act="draft">Draft a personal message</button>
        ${r.partial_allowed ? '<span class="muted small">Paying in parts is allowed.</span>'
          : '<button class="btn ghost" data-act="partial-open">Let them pay in parts</button>'}
        <button class="btn ghost" data-act="trail">Paper trail</button>
        <button class="btn ghost danger" data-act="writeoff">Write off</button>
      </div>
      ${d.error ? `<div class="alert error">${esc(d.error)}</div>` : ""}
      ${d.partial ? `
        <div class="btn-row"><label class="field inline"><span>Smallest payment they can make: $</span>
          <input class="amount-input" type="number" min="1" step="1" value="${Math.max(1, Math.round(owed / 4))}" data-role="minimum"></label>
          <button class="btn primary" data-act="partial">Allow and re-send</button></div>` : ""}
      ${d.draft ? `
        <div class="draft-box"><div class="muted small">For you to send yourself, from your own email or phone. Upstream won't send it.</div>
          <textarea readonly>${esc(d.draft)}</textarea>
          <div class="btn-row"><button class="btn ghost" data-act="copy-draft">Copy</button>
            <a class="btn ghost" href="${mailto}">Open in email</a></div></div>` : ""}
      ${d.trail ? `
        <div class="draft-box"><div class="muted small">Everything that happened with #${esc(r.number)}, for a formal demand, collections or small claims.</div>
          <pre class="trail">${esc(d.trail)}</pre>
          <div class="btn-row"><button class="btn ghost" data-act="copy-trail">Copy</button></div></div>` : ""}
    </div>`;
}

function trailText(t) {
  const i = t.invoice;
  const lines = [`Invoice #${i.number} from ${t.invoicer} to ${i.client_name || ""} <${i.client_email}>`,
    `Amount: $${i.amount.toFixed(2)} ${i.currency}; still owed: $${(i.owed ?? i.amount).toFixed(2)}`,
    `Issued: ${i.created_at.slice(0, 10)}; due: ${i.due_date}; status: ${i.status}`,
    `Client opened the invoice: ${i.viewed === 1 ? "yes" : i.viewed === 0 ? "no" : "unknown"}`, ""];
  for (const e of t.events) {
    lines.push(`${e.at.slice(0, 16).replace("T", " ")}  ${e.message}`);
    if (e.note) lines.push(`    "${e.note.replace(/\s+/g, " ")}"`);
  }
  return lines.join("\n");
}

async function loadInvoices() {
  if (clockBusy) return;  // the clock request will refresh when it's done
  // Don't redraw while someone is typing an amount or selecting draft text
  if (document.activeElement?.matches?.(".inv-panel input, .inv-panel textarea")) return;
  try {
    const [rows, st] = await Promise.all([api("/api/getpaid/invoices"), api("/api/getpaid/state")]);
    const open = rows.filter((r) => OPEN_STATUSES.has(r.status));
    const owedTotal = open.reduce((s, r) => s + (r.owed ?? r.amount), 0);
    const needs = open.filter((r) => r.needs_you).length;
    $("inv-summary").textContent = open.length
      ? `${money(owedTotal)} owed across ${open.length}${needs ? ` · ${needs} need${needs === 1 ? "s" : ""} you` : ""}` : "";
    $("inv-list").innerHTML = rows.length ? rows.map((r) => {
      const st2 = r.needs_you ? "NEEDS_YOU" : r.days_overdue > 0 ? "OVERDUE" : r.status;
      const label = { NEEDS_YOU: "Needs you", OVERDUE: "Overdue", SENT: "Sent", UNPAID: "Unpaid",
                      PARTIALLY_PAID: "Part paid", PAID: "Paid ✅", MARKED_AS_PAID: "Paid ✅",
                      CANCELLED: r.written_off ? "Written off" : "Cancelled",
                      WRITTEN_OFF: "Written off" }[st2] || st2;
      const amount = r.owed != null && r.owed < r.amount && OPEN_STATUSES.has(r.status)
        ? `${money(r.owed)} <span class="muted small">of ${money(r.amount)}</span>` : money(r.amount);
      const canAct = OPEN_STATUSES.has(r.status) && r.days_overdue > 0;
      return `
        <li class="inv-row ${r.needs_you ? "needs-you" : ""}" data-invoice="${esc(r.id)}">
          <div class="row"><b>#${esc(r.number)} · ${esc(r.client_name || r.client_email)}</b>
            <span class="num">${amount}</span></div>
          <div class="row"><span class="muted small">due ${fmtDay(r.due_date)}</span>
            <span class="status ${esc(st2)}">${esc(label)}</span></div>
          <div class="chase-note">${chaseNote(r)}</div>
          ${canAct ? `<button class="link options-toggle" data-act="toggle">${openPanels.has(r.id) ? "Hide options" : "Options"}</button>` : ""}
          ${canAct && openPanels.has(r.id) ? panelHtml(r) : ""}
        </li>`;
    }).join("") : '<li class="muted">No invoices yet. Ask the chat to bill someone.</li>';

    gpState = st;
    $("chase-toggle").checked = !st.chasing_paused;
    if (document.activeElement !== $("followup-days")) $("followup-days").value = st.followup_days;
    show($("demo-clock"), st.sandbox);
    $("clock-text").textContent = st.demo_offset_days
      ? `pretending today is ${fmtDay(st.today)} (+${st.demo_offset_days} day${st.demo_offset_days === 1 ? "" : "s"})`
      : `today (${fmtDay(st.today)})`;
    const cls = (t) => t === "UPSTREAM.INVOICE.PAID" || t === "UPSTREAM.INVOICE.PARTIAL_PAYMENT" ? "log-paid"
      : t === "UPSTREAM.INVOICE.NEEDS_YOU" ? "log-needs" : "";
    $("chase-log").innerHTML = st.log.length ? st.log.map((e) => `
      <li class="log-item ${cls(e.event_type)}">
        <span>${esc(e.message)}</span>
        ${e.note ? `<blockquote class="log-note">${esc(e.note)}</blockquote>` : ""}</li>`).join("")
      : '<li class="muted">Nothing yet. Overdue invoices are chased here automatically.</li>';
  } catch { /* keep the last view */ }
}

$("inv-list").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-act]");
  if (!btn) return;
  const row = btn.closest("[data-invoice]");
  const id = row.dataset.invoice;
  const d = (panelData[id] ||= {});
  const act = btn.dataset.act;
  d.error = null;
  if (act === "toggle") {
    if (openPanels.has(id)) openPanels.delete(id); else openPanels.add(id);
    return loadInvoices();
  }
  if (act === "copy-draft" || act === "copy-trail") {
    await navigator.clipboard.writeText(act === "copy-draft" ? d.draft : d.trail).catch(() => {});
    btn.textContent = "Copied ✓";
    return;
  }
  if (act === "partial-open") { d.partial = true; return loadInvoices(); }
  if (act === "writeoff" && !confirm("Write this invoice off? Upstream stops chasing it and it's no longer counted as owed.")) return;
  btn.disabled = true;
  btn.textContent = { draft: "Drafting…", trail: "Loading…", partial: "Updating PayPal…", writeoff: "Writing off…" }[act] || btn.textContent;
  try {
    if (act === "draft") {
      d.draft = (await api(`/api/getpaid/invoices/${encodeURIComponent(id)}/draft`, { method: "POST" })).draft;
    } else if (act === "trail") {
      d.trail = trailText(await api(`/api/getpaid/invoices/${encodeURIComponent(id)}/trail`));
    } else if (act === "partial") {
      const minimum = Number(row.querySelector('[data-role="minimum"]').value);
      await api(`/api/getpaid/invoices/${encodeURIComponent(id)}/allow-partial`, {
        method: "POST", body: JSON.stringify({ minimum }) });
      d.partial = false;
    } else if (act === "writeoff") {
      await api(`/api/getpaid/invoices/${encodeURIComponent(id)}/write-off`, { method: "POST" });
      openPanels.delete(id);
    }
  } catch (err) {
    d.error = err.message;
  }
  document.activeElement?.blur?.();
  loadInvoices();
});

$("followup-days").addEventListener("change", async (e) => {
  const days = Math.min(30, Math.max(3, Number(e.target.value) || 7));
  e.target.value = days;
  await api("/api/getpaid/followup", { method: "PUT", body: JSON.stringify({ days }) }).catch(() => {});
  e.target.blur();
  loadInvoices();
});

$("chase-toggle").addEventListener("change", async (e) => {
  await api("/api/getpaid/chasing", { method: "POST", body: JSON.stringify({ enabled: e.target.checked }) })
    .catch((err) => alert(err.message));
  loadInvoices();
});

$("demo-clock").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-days]");
  if (!btn || clockBusy) return;
  const step = Number(btn.dataset.days);
  const days = step === 0 ? 0 : (gpState?.demo_offset_days || 0) + step;
  clockBusy = true;
  document.querySelectorAll("#demo-clock button").forEach((b) => { b.disabled = true; });
  $("clock-text").textContent = "moving the clock and running chasing…";
  try {
    await api("/api/getpaid/demo-clock", { method: "POST", body: JSON.stringify({ days }) });
  } catch (err) {
    $("clock-text").textContent = `couldn't move the clock: ${err.message}`;
    clockBusy = false;
    document.querySelectorAll("#demo-clock button").forEach((b) => { b.disabled = false; });
    return;
  } finally {
    clockBusy = false;
    await loadInvoices();
    document.querySelectorAll("#demo-clock button").forEach((b) => { b.disabled = false; });
  }
});

// Payments and reminders happen on the server on their own; keep the view current
setInterval(() => { if (!$("tab-getpaid").hidden) loadInvoices(); }, 20000);

// ---------- your writing style ----------

const VOICE_MAX = 5;
const VOICE_PLACEHOLDERS = [
  "e.g. hey sam! shipped the new login flow this morning, have a look when you get a sec. — Jan",
  "e.g. Hi team, quick update: staging is back up. Let me know if anything looks off. Best, Jan",
  "e.g. Thanks so much for the feedback! I'll fold it into tomorrow's build.",
];

function voiceBox(value = "", i = 0) {
  const ta = document.createElement("textarea");
  ta.value = value;
  ta.placeholder = VOICE_PLACEHOLDERS[i % VOICE_PLACEHOLDERS.length];
  ta.setAttribute("aria-label", `Sample message ${i + 1}`);
  return ta;
}

function voiceSamples() {
  return [...document.querySelectorAll("#voice-samples textarea")].map((t) => t.value.trim()).filter(Boolean);
}

function setVoiceStatus(saved) {
  $("voice-status").className = `status ${saved ? "SUCCESS" : "PENDING"}`;
  $("voice-status").textContent = saved
    ? `Reminders use your style (${saved} sample${saved === 1 ? "" : "s"})`
    : "Not set: reminders use a neutral voice";
}

async function loadVoice() {
  try {
    const { samples } = await api("/api/getpaid/voice");
    const box = $("voice-samples");
    box.innerHTML = "";
    const n = Math.max(samples.length, 3);
    for (let i = 0; i < n; i++) box.appendChild(voiceBox(samples[i] || "", i));
    setVoiceStatus(samples.length);
  } catch { /* leave it */ }
}

$("voice-add").addEventListener("click", () => {
  const box = $("voice-samples");
  if (box.children.length < VOICE_MAX) box.appendChild(voiceBox("", box.children.length));
  $("voice-add").disabled = box.children.length >= VOICE_MAX;
});

$("voice-preview").addEventListener("click", async () => {
  const samples = voiceSamples();
  if (!samples.length) { $("voice-note").textContent = "Paste at least one message first."; return; }
  $("voice-preview").disabled = true;
  $("voice-note").textContent = "Writing both versions…";
  try {
    const r = await api("/api/getpaid/voice/preview", {
      method: "POST", body: JSON.stringify({ tone: $("voice-tone").value, samples }),
    });
    $("voice-generic").textContent = r.generic;
    $("voice-yours").textContent = r.yours;
    show($("voice-compare"));
    $("voice-note").textContent = `A ${r.tone} reminder for #${r.invoice.number} (${r.invoice.client_name}). Nothing was sent.`;
  } catch (err) {
    $("voice-note").textContent = err.message;
  } finally {
    $("voice-preview").disabled = false;
  }
});

$("voice-save").addEventListener("click", async () => {
  $("voice-save").disabled = true;
  try {
    const { samples } = await api("/api/getpaid/voice", {
      method: "PUT", body: JSON.stringify({ samples: voiceSamples() }),
    });
    setVoiceStatus(samples.length);
    $("voice-note").textContent = samples.length
      ? "Saved. Every reminder from now on is written in your style."
      : "Cleared. Reminders use a neutral voice.";
  } catch (err) {
    $("voice-note").textContent = err.message;
  } finally {
    $("voice-save").disabled = false;
  }
});

loadVoice();
