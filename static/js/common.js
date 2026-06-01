/* Shared helpers: API client, formatting, toast, read-only enforcement. */

const RO = window.STOCKBOOK.readonly;
const HIDE_AMT = window.STOCKBOOK.hideAmounts;

/* --- API --- */
async function api(method, path, body) {
  const opts = { method, headers: { "Content-Type": "application/json" } };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch (_) {}
  if (!res.ok) {
    const msg = (data && data.detail) ? detailText(data.detail) : `请求失败 (${res.status})`;
    throw new Error(msg);
  }
  return data;
}
function detailText(detail) {
  if (Array.isArray(detail)) return detail.map(d => d.msg || JSON.stringify(d)).join("; ");
  return typeof detail === "string" ? detail : JSON.stringify(detail);
}

/* --- Formatting --- */
const CNY = new Intl.NumberFormat("zh-CN", { style: "currency", currency: "CNY", maximumFractionDigits: 0 });

function money(v) {
  if (HIDE_AMT) return "•••••";
  if (v === null || v === undefined) return "—";
  return CNY.format(v);
}
function signOf(v) { return v > 0 ? "+" : (v < 0 ? "−" : ""); }

function moneySigned(v) {
  if (HIDE_AMT) return "•••••";
  if (v === null || v === undefined) return "—";
  return signOf(v) + CNY.format(Math.abs(v));
}
function pct(v, digits = 1) {
  if (v === null || v === undefined) return "—";
  return v.toFixed(digits) + "%";
}
function pctSigned(v, digits = 1) {
  if (v === null || v === undefined) return "—";
  return signOf(v) + Math.abs(v).toFixed(digits) + "%";
}
function colorVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || name;
}

// Soft validation (spec §7): band_low ≤ target ≤ band_high and
// 0 ≤ band_low < band_high ≤ 100. Returns a warning string, or null if OK.
// This is advisory only — callers still proceed with the save.
function bandWarn(low, target, high) {
  if (!(low < high)) return "提示:区间下限应小于上限";
  if (target < low || target > high) return "提示:目标权重建议落在区间内";
  if (low < 0 || high > 100) return "提示:区间应在 0–100% 之间";
  return null;
}

/* --- Toast --- */
let toastTimer = null;
function toast(msg, isErr = false) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.className = "show" + (isErr ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = ""; }, 2600);
}

/* --- Read-only enforcement: hide/disable write controls --- */
function applyReadonly() {
  if (!RO) return;
  document.querySelectorAll("[data-write]").forEach(el => {
    el.disabled = true;
    el.style.display = "none";
  });
}
