"use strict";
// Shared helpers for every tab, and the status pills in the header.

const $ = (id) => document.getElementById(id);
const money = (n) => "$" + Number(n || 0).toLocaleString("en-US",
  { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

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

loadHealth();
