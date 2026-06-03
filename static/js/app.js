/* «衡» single-page app: in-page tabs, dashboard, entry, asset-class modal.
   Interaction model = spec §4 unallocated pool, but sliders move freely:
   the pool may go negative; saving is blocked until it returns to 0. */

let DASH = null;       // last dashboard payload (shared by both tabs)
let SLIDERS = [];      // [{id, name, band, color, value}] — live target state
let AXIS = 40;         // band-track horizontal axis max (%)
let acEditId = null;   // asset-class id being edited in the modal (null = create)
let rebMode = "target";    // 'target' | 'edge' — pull back to exact target or band edge
let rebCashOnly = false;   // only show 加仓 (rebalance with new money, no selling)
let rebIgnoreSmall = false;// hide tiny adjustments
const SMALL_THRESHOLD = 1000;  // ¥ — "零碎" cutoff

const STATUS_CLASS = { ok: "in", over: "over", under: "under", na: "na" };
const STATUS_TEXT = { ok: "● 在区间内", over: "↓ 需减仓", under: "↑ 需加仓", na: "待估值" };

document.addEventListener("DOMContentLoaded", () => {
  initTabs();
  bind();
  // Render cached prices first (fast), then pull live quotes and re-render.
  load().then(() => {
    if (!RO && window.STOCKBOOK.autoRefresh) refreshPrices(true);
  });
});

async function load() {
  try {
    DASH = await api("GET", "/api/dashboard");
    renderDashboard();
    renderHoldingsTab();
    applyReadonly();
  } catch (e) { toast(e.message, true); }
}

/* ---------- tabs ---------- */
function initTabs() {
  document.querySelectorAll(".tab").forEach(btn => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  });
  // Deep-link / reload-persist the active tab via the URL hash.
  const hash = (location.hash || "").replace("#", "");
  switchTab(["holdings", "records"].includes(hash) ? hash : "dashboard");
}
const TABS = ["dashboard", "holdings", "records"];
function switchTab(name) {
  document.querySelectorAll(".tab").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
  TABS.forEach(t => { document.getElementById(`panel-${t}`).hidden = name !== t; });
  if (history.replaceState) history.replaceState(null, "", name === "dashboard" ? location.pathname + location.search : `#${name}`);
  if (name === "records") renderRecords();
}

/* ---------- event wiring ---------- */
function bind() {
  byId("save-targets")?.addEventListener("click", saveTargets);
  byId("reset-default")?.addEventListener("click", resetDefault);
  byId("add-class")?.addEventListener("click", openCreateClass);
  byId("recolor")?.addEventListener("click", recolorClasses);
  byId("m-save")?.addEventListener("click", saveClass);
  byId("m-cancel")?.addEventListener("click", closeModal);
  byId("m-delete")?.addEventListener("click", deleteClass);
  byId("ac-modal")?.addEventListener("click", e => { if (e.target.id === "ac-modal") closeModal(); });
  byId("add-trade")?.addEventListener("click", openTradeModal);
  byId("t-save")?.addEventListener("click", saveTrade);
  byId("t-cancel")?.addEventListener("click", closeTradeModal);
  byId("trade-modal")?.addEventListener("click", e => { if (e.target.id === "trade-modal") closeTradeModal(); });
  byId("sell-save")?.addEventListener("click", saveSell);
  byId("sell-cancel")?.addEventListener("click", () => byId("sell-modal").classList.remove("show"));
  byId("sell-modal")?.addEventListener("click", e => { if (e.target.id === "sell-modal") byId("sell-modal").classList.remove("show"); });
  byId("add-cash")?.addEventListener("click", openCashModal);
  byId("cf-save")?.addEventListener("click", saveCashFlow);
  byId("cf-cancel")?.addEventListener("click", () => byId("cash-modal").classList.remove("show"));
  byId("cash-modal")?.addEventListener("click", e => { if (e.target.id === "cash-modal") byId("cash-modal").classList.remove("show"); });
  byId("refresh-prices")?.addEventListener("click", () => refreshPrices(false));
  byId("backup-db")?.addEventListener("click", openBackupModal);
  byId("bk-now")?.addEventListener("click", doBackup);
  byId("bk-verify")?.addEventListener("click", verifyBackups);
  byId("bk-close")?.addEventListener("click", () => byId("backup-modal").classList.remove("show"));
  byId("backup-modal")?.addEventListener("click", e => { if (e.target.id === "backup-modal") byId("backup-modal").classList.remove("show"); });
}

/* ---------- backup / restore ---------- */

// Per-file verified state (populated by verifyBackups); key = file, val = 'ok'|'mismatch'|'unavailable'
let _bkVerified = {};

function _bkBadge(file) {
  const s = _bkVerified[file];
  if (s === "ok")          return '<span class="bk-badge ok">✓ 已校验</span>';
  if (s === "mismatch")    return '<span class="bk-badge warn">⚠ 不一致</span>';
  if (s === "unavailable") return '<span class="bk-badge muted">☁ 暂不可验</span>';
  return '<span class="bk-badge muted">… 未校验</span>';
}

function _destBadges(destinations) {
  return (destinations || []).map(d => `<span class="bk-badge muted">${d}</span>`).join(" ");
}

function _bkSummary(list) {
  if (!list.length) return "";
  const newest = list.reduce((a, b) => (a.modified > b.modified ? a : b));
  const ts = newest.modified.slice(0, 19).replace("T", " ");
  const offsite = list.filter(b => (b.destinations || []).includes("offsite"));
  const hasOffsite = offsite.length > 0;
  // Current posture = whether the MOST-RECENT offsite backup is encrypted. The folder
  // can hold a mix (old encrypted + new plaintext after a passphrase is removed), so
  // "any backup encrypted" would wrongly keep showing 已加密 with no passphrase set.
  const encNow = hasOffsite && offsite.reduce((a, b) => (a.modified > b.modified ? a : b)).encrypted;
  let offsiteLabel;
  if (hasOffsite && encNow) {
    offsiteLabel = '<span>异地:<strong>已加密</strong> 🔒</span>';
  } else if (hasOffsite) {
    offsiteLabel = '<span>异地:<strong>已配置</strong><span style="color:var(--accent);margin-left:2px" title="异地副本未加密">⚠</span><span style="color:var(--ink-3)">(未加密)</span></span>';
  } else {
    offsiteLabel = '<span>异地:<span style="color:var(--ink-3)">未配置</span></span>';
  }
  return `<div class="bk-info"><span>上次备份:${ts}</span>${offsiteLabel}</div>`;
}

async function openBackupModal() {
  byId("backup-modal").classList.add("show");
  await renderBackupList();
}

const BK_PAGE_SIZE = 8;
let _bkAll = [];     // full backup list (fetched once)
let _bkPage = 0;     // current page index

async function renderBackupList() {
  const box = byId("bk-list");
  try {
    _bkAll = await api("GET", "/api/backups");
    _bkPage = 0;
    _renderBackupPage();
  } catch (e) { box.innerHTML = `<div class="bk-empty">${e.message}</div>`; }
}

function _renderBackupPage() {
  const box = byId("bk-list");
  const list = _bkAll;
  if (!list.length) { box.innerHTML = `<div class="bk-empty">还没有备份。点「立即备份」创建一个。</div>`; return; }
  const pages = Math.ceil(list.length / BK_PAGE_SIZE);
  _bkPage = Math.min(Math.max(_bkPage, 0), pages - 1);
  const slice = list.slice(_bkPage * BK_PAGE_SIZE, (_bkPage + 1) * BK_PAGE_SIZE);
  const rows = slice.map(b => {
    const dests = b.destinations || [];
    const hasOffsite = dests.includes("offsite");
    // Restore button: if offsite available show a destination selector, else just restore
    const restoreBtn = hasOffsite
      ? `<select class="mini-btn" data-dest-sel="${b.file}" style="padding:4px 6px">
           <option value="local">local</option>
           <option value="offsite">offsite</option>
         </select>
         <button class="mini-btn primary" data-restore="${b.file}">恢复</button>`
      : `<button class="mini-btn" data-restore="${b.file}">恢复</button>`;
    const lockBadge = b.encrypted ? '<span class="bk-lock" title="异地副本已加密">🔒</span>' : '';
    return `<div class="bk-row">
      <div>
        <div class="bk-file">${b.file}${lockBadge}</div>
        <div class="bk-meta">${b.modified.slice(0, 19).replace("T", " ")} · ${(b.size / 1024).toFixed(0)} KB
          &ensp;${_destBadges(dests)}&ensp;${_bkBadge(b.file)}</div>
      </div>
      <div class="bk-row-right">${restoreBtn}</div>
    </div>`;
  }).join("");
  const pager = pages > 1
    ? `<div class="bk-pager">
         <button class="mini-btn" data-bk-prev ${_bkPage === 0 ? "disabled" : ""}>← 上一页</button>
         <span class="bk-pageno">${_bkPage + 1} / ${pages}</span>
         <button class="mini-btn" data-bk-next ${_bkPage >= pages - 1 ? "disabled" : ""}>下一页 →</button>
       </div>`
    : "";
  box.innerHTML = _bkSummary(list) + rows + pager;
  box.querySelectorAll("[data-restore]").forEach(btn =>
    btn.addEventListener("click", () => {
      const file = btn.dataset.restore;
      const sel = box.querySelector(`[data-dest-sel="${file}"]`);
      doRestore(file, sel ? sel.value : "local");
    }));
  const prev = box.querySelector("[data-bk-prev]");
  if (prev) prev.addEventListener("click", () => { _bkPage--; _renderBackupPage(); });
  const next = box.querySelector("[data-bk-next]");
  if (next) next.addEventListener("click", () => { _bkPage++; _renderBackupPage(); });
}

async function doBackup() {
  try { const r = await api("POST", "/api/backup"); toast(`已备份:${r.file}`); await renderBackupList(); }
  catch (e) { toast(e.message, true); }
}

async function doRestore(file, destination) {
  if (!confirm(`恢复到「${file}」?当前数据会先自动备份一次,再被覆盖。`)) return;
  try {
    const body = { file };
    if (destination && destination !== "local") body.destination = destination;
    await api("POST", "/api/restore", body);
    byId("backup-modal").classList.remove("show");
    toast("已恢复备份");
    await load();
  } catch (e) { toast(e.message, true); }
}

async function verifyBackups() {
  const btn = byId("bk-verify");
  if (btn) { btn.disabled = true; btn.textContent = "校验中…"; }
  try {
    const results = await api("POST", "/api/backup/verify");
    // Worst-status-wins per file: mismatch(2) > unavailable(1) > ok(0)
    const rank = { ok: 0, unavailable: 1, mismatch: 2 };
    _bkVerified = {};
    results.forEach(r => {
      if (_bkVerified[r.file] === undefined || rank[r.status] > rank[_bkVerified[r.file]]) {
        _bkVerified[r.file] = r.status;
      }
    });
    _renderBackupPage();
    const mismatches = results.filter(r => r.status === "mismatch");
    if (mismatches.length) {
      const decryptFails = mismatches.filter(r => (r.reason || "").includes("解密失败"));
      if (decryptFails.length) {
        toast("🔒 解密失败:口令错误或文件损坏 —— 先确认 .env 口令再判定损坏", true);
      } else {
        toast(`⚠ ${mismatches.length} 个备份校验不一致`, true);
      }
    } else {
      toast("校验完成");
    }
  } catch (e) { toast(e.message, true); }
  finally { if (btn) { btn.disabled = false; btn.textContent = "立即校验"; } }
}

/* ---------- live quotes ---------- */
async function refreshPrices(silent) {
  const btn = byId("refresh-prices");
  if (btn) { btn.disabled = true; btn.textContent = "刷新中…"; }
  try {
    const r = await api("POST", "/api/prices/refresh");
    await load();
    if (!silent) {
      const src = r.source ? ` · 源:${r.source}` : "";
      const tail = (r.unresolved && r.unresolved.length) ? ` · ${r.unresolved.length} 个未取到:${r.unresolved.join("、")}` : "";
      toast(`行情已更新 ${r.updated}/${r.total} 个标的${src}${tail}`);
    }
  } catch (e) {
    if (!silent) toast(e.message, true);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "↻ 刷新行情"; }
  }
}

/* =======================================================================
   DASHBOARD
   ======================================================================= */
function renderDashboard() {
  SLIDERS = DASH.asset_classes.map(ac => ({
    id: ac.id, name: ac.name, band: [ac.band_low, ac.band_high],
    color: ac.color, value: ac.target_weight,
  }));
  AXIS = computeAxis();
  renderTopstats();
  renderSliders();
  renderTargetStack();
  renderActualStack();
  renderLegend();
  renderHoldings();
  renderRebalance();
  updateUnalloc();
}

function computeAxis() {
  let m = 0;
  DASH.asset_classes.forEach(ac => { m = Math.max(m, ac.band_high, ac.target_weight, ac.current_weight || 0); });
  return clamp(Math.ceil((m + 3) / 5) * 5, 20, 100);
}

function renderTopstats() {
  const warn = byId("cash-warn");
  if (warn) {
    if (DASH.cash_balance < 0) {
      warn.hidden = false;
      warn.innerHTML = `⚠ 现金余额为负(${money(DASH.cash_balance)})——通常是<b>还没记录资金注入</b>。` +
        `占比已按现金 0 计算以保持正常;请到「记录」tab 点「＋ 资金流水」补记入金。`;
    } else {
      warn.hidden = true;
    }
  }
  byId("stat-total").textContent = money(DASH.total_assets);
  const dn = byId("stat-deviating");
  dn.textContent = `${DASH.deviating_count} / ${DASH.asset_classes.length}`;
  dn.classList.toggle("warn", DASH.deviating_count > 0);
  const d = DASH.valuation_date ? DASH.valuation_date.slice(0, 10) : "待估值";
  const tag = DASH.price_state === "live" ? ` <span class="px-tag live">实时</span>`
            : DASH.price_state === "close" ? ` <span class="px-tag">收盘</span>` : "";
  byId("stat-date").innerHTML = d + tag;
}

/* ----- sliders (free movement, pool may go negative) ----- */
function renderSliders() {
  byId("sliders").innerHTML = SLIDERS.map((s, i) => `
    <div class="srow">
      <div class="sname">
        <span class="dot" style="background:${colorVar(s.color)}"></span>
        <span class="meta">
          <span class="n">${s.name}</span>
          <span class="band">区间 ${fmtWeight(s.band[0])}–${fmtWeight(s.band[1])}%</span>
        </span>
      </div>
      <input type="range" min="0" max="100" step="1" value="${s.value}" data-i="${i}"
             ${RO ? "disabled" : "data-write"} />
      <span class="sval" data-sval="${i}">${fmtWeight(s.value)}%</span>
      <button class="icon-btn" data-edit-i="${i}" data-write title="编辑大类">✎</button>
    </div>`).join("");
  if (!RO) {
    byId("sliders").querySelectorAll('input[type="range"]').forEach(inp =>
      inp.addEventListener("input", onSlide));
    byId("sliders").querySelectorAll("[data-edit-i]").forEach(btn =>
      btn.addEventListener("click", () => openEditClass(SLIDERS[+btn.dataset.editI].id)));
  }
}

function onSlide(e) {
  const i = +e.target.dataset.i;
  // No clamping — let the user drag freely; the unallocated pool may go
  // negative and they reconcile it themselves before saving.
  SLIDERS[i].value = parseFloat(e.target.value);
  byId(null, `[data-sval="${i}"]`).textContent = `${fmtWeight(SLIDERS[i].value)}%`;
  renderTargetStack();
  updateUnalloc();
  updateTargetLine(SLIDERS[i]);
}

function unallocated() { return 100 - SLIDERS.reduce((s, x) => s + x.value, 0); }

function updateUnalloc() {
  const u = unallocated(), el = byId("unalloc");
  if (Math.abs(u) < 1e-6) {
    el.className = "total-chip";
    el.innerHTML = "✓ 已配平 <b>0%</b>";
  } else if (u > 0) {
    el.className = "total-chip off";
    el.innerHTML = `未分配 <b>${fmtWeight(u)}%</b>`;
  } else {
    el.className = "total-chip off";
    el.innerHTML = `超额 <b>${fmtWeight(-u)}%</b> · 需下调`;
  }
}

/* ----- stacked bars ----- */
function renderTargetStack() {
  byId("targetStack").innerHTML = SLIDERS.map(s => seg(s.value, s.color, 1)).join("");
}
function renderActualStack() {
  byId("actualStack").innerHTML = DASH.asset_classes.map(ac => seg(ac.current_weight || 0, ac.color, 0.82)).join("");
}
function seg(w, color, opacity) {
  if (w <= 0) return "";
  const label = w >= 7 ? `<span>${fmtWeight(w)}%</span>` : "";
  return `<div class="seg" style="width:${w}%;background:${colorVar(color)};opacity:${opacity}">${label}</div>`;
}
function renderLegend() {
  byId("legend").innerHTML = DASH.asset_classes.map(ac =>
    `<div class="item"><span class="dot" style="background:${colorVar(ac.color)}"></span>${ac.name}</div>`).join("");
}

/* ----- holdings & deviation ----- */
function renderHoldings() {
  const box = byId("holdings");
  box.innerHTML = DASH.asset_classes.map(hblock).join("");
  box.querySelectorAll(".hrow.clickable").forEach(row =>
    row.addEventListener("click", () => row.closest(".hblock").classList.toggle("open")));

  const pend = DASH.pending_securities, note = byId("pending-note");
  if (pend.length) {
    note.hidden = false;
    note.textContent = `⏳ ${pend.length} 个标的待估值(未纳入市值汇总):` +
      pend.map(s => `${s.name}(${s.code})`).join("、");
  } else { note.hidden = true; }
}

function hblock(ac) {
  const st = STATUS_CLASS[ac.status];
  const col = colorVar(ac.status === "ok" ? "--sage" : ac.status === "over" ? "--accent"
            : ac.status === "under" ? "--ochre" : "--ink-3");
  const lo = ac.band_low / AXIS * 100, hi = ac.band_high / AXIS * 100;
  const actDot = ac.current_weight == null ? "" :
    `<div class="actual" style="left:${clamp(ac.current_weight, 0, AXIS) / AXIS * 100}%;background:${col}"></div>`;
  const pctHtml = ac.current_weight == null
    ? `<div class="pct na">待估值</div>`
    : `<div class="pct" style="color:${col}">${pct(ac.current_weight)}</div>`;
  const hasSecs = ac.securities.length > 0;
  return `
    <div class="hblock">
      <div class="hrow ${hasSecs ? "clickable" : ""}">
        <div class="hname">
          <span class="caret">${hasSecs ? "▶" : ""}</span>
          <span class="dot" style="background:${colorVar(ac.color)}"></span>
          <span class="meta"><span class="n">${ac.name}</span><span class="v">${money(ac.market_value)}</span></span>
        </div>
        <div class="track">
          <div class="axis"></div>
          <div class="bandzone" style="left:${lo}%;width:${Math.max(0, hi - lo)}%"></div>
          <div class="bedge" style="left:${lo}%"></div>
          <div class="bedge" style="left:${hi}%"></div>
          <div class="bnum" style="left:${lo}%">${fmtWeight(ac.band_low)}</div>
          <div class="bnum" style="left:${hi}%">${fmtWeight(ac.band_high)}</div>
          <div class="target" data-tgt="${ac.id}" style="left:${clamp(ac.target_weight, 0, AXIS) / AXIS * 100}%"></div>
          ${actDot}
        </div>
        <div class="hstat">
          ${pctHtml}
          <span class="chip ${st}">${STATUS_TEXT[ac.status]}</span>
          <div class="drift" data-drift="${ac.id}">目标 ${fmtWeight(ac.target_weight)}% · 偏离 ${pctSigned(ac.deviation)}</div>
        </div>
      </div>
      <div class="subsec">${securitiesHtml(ac)}</div>
    </div>`;
}

function securitiesHtml(ac) {
  if (!ac.securities.length) return `<div class="empty">该大类暂无标的</div>`;
  return ac.securities.map(seccard).join("");
}

function seccard(s) {
  const pnlHtml = s.unrealized_pnl == null ? "" : (() => {
    const cls = s.unrealized_pnl > 0 ? "up" : s.unrealized_pnl < 0 ? "down" : "flat";
    return `<span class="pnl ${cls}">${moneySigned(s.unrealized_pnl)} (${pctSigned(s.pnl_pct)})</span>`;
  })();
  const mv = s.pending
    ? `<span class="pending-tag">待估值 · 去录入设现价</span>`
    : `<span class="mvk">市值</span>${money(s.market_value)} ${pnlHtml}`;
  const metric = (k, v) => `<div class="m"><span class="k">${k}</span><span class="v">${v}</span></div>`;
  return `
    <div class="seccard ${s.pending ? "pending" : ""}">
      <div class="seccard-top">
        <div class="seccard-id"><span class="sn">${s.name}</span><span class="sc">${s.code} · ${s.market}</span></div>
        <div class="seccard-mv">${mv}</div>
      </div>
      <div class="seccard-metrics">
        ${metric("单价", priceMoney(s.price))}
        ${metric("持仓", HIDE_AMT ? "•••••" : `${fmtNum(s.shares)} 股`)}
        ${metric("成本价", priceMoney(s.avg_cost))}
        ${metric("大类内占比", pct(s.weight_in_class))}
        ${metric("总资产占比", pct(s.weight_in_total))}
      </div>
    </div>`;
}

function updateTargetLine(s) {
  const tgt = byId(null, `[data-tgt="${s.id}"]`);
  if (tgt) tgt.style.left = `${clamp(s.value, 0, AXIS) / AXIS * 100}%`;
  const ac = DASH.asset_classes.find(a => a.id === s.id);
  const drift = byId(null, `[data-drift="${s.id}"]`);
  if (drift) {
    const dev = ac.current_weight == null ? null : ac.current_weight - s.value;
    drift.textContent = `目标 ${fmtWeight(s.value)}% · 偏离 ${pctSigned(dev)}`;
  }
}

/* ----- rebalance (reminder + controls + filtered list) ----- */
function renderRebalance() {
  byId("rebal").innerHTML = rebReminderHtml() + rebControlsHtml() + rebListHtml();
  bindRebControls();
}

function rebReminderHtml() {
  const last = DASH.last_rebalanced_at;
  let text, warn = false;
  if (!last) {
    text = "尚未记录上次再平衡 · 建议执行后点右侧按钮留痕";
  } else {
    const m = monthsSince(last);
    warn = m >= 12;
    text = `上次再平衡:${last.slice(0, 10)} · ${m} 个月前` + (warn ? " · 已超一年,建议再平衡" : "");
  }
  const btn = RO ? "" : `<button class="btn" id="mark-reb" data-write>标记已完成再平衡</button>`;
  return `<div class="reb-reminder ${warn ? "warn" : ""}">${text}<span class="spacer"></span>${btn}</div>`;
}

function rebControlsHtml() {
  return `
    <div class="reb-controls">
      <div class="segmented">
        <button data-mode="target" class="${rebMode === "target" ? "on" : ""}">回到目标</button>
        <button data-mode="edge" class="${rebMode === "edge" ? "on" : ""}">回到区间边缘</button>
      </div>
      <label class="toggle"><input type="checkbox" id="reb-cash" ${rebCashOnly ? "checked" : ""}/> 仅加仓(用新增资金)</label>
      <label class="toggle"><input type="checkbox" id="reb-small" ${rebIgnoreSmall ? "checked" : ""}/> 忽略零碎(&lt;¥${SMALL_THRESHOLD.toLocaleString("en-US")})</label>
    </div>
    <div class="reb-hint">A股按手(100 股)成交,实际下单请就近取整;欠配也可优先用新增资金/分红加仓而非卖出。</div>`;
}

function rebListHtml() {
  let items = DASH.rebalance.map(r => ({ ...r, amt: rebMode === "edge" ? r.edge_amount : r.amount }));
  if (rebCashOnly) items = items.filter(r => r.amt > 0);
  if (rebIgnoreSmall) items = items.filter(r => Math.abs(r.amt) >= SMALL_THRESHOLD);
  if (!items.length) {
    const msg = DASH.rebalance.length ? "当前筛选下无需操作" : "当前所有类别均在容忍区间内,无需调仓";
    return `<div class="empty">✓ ${msg}</div>`;
  }
  const goal = rebMode === "edge" ? "区间边缘" : "目标";
  return `<ul>${items.map(r => {
    const buy = r.amt >= 0, where = r.status === "over" ? "超出上限" : "低于下限";
    return `<li>
      <span class="actdot ${buy ? "buy" : "sell"}">${buy ? "+" : "−"}</span>
      <span class="body"><b>${r.name}</b> 实际 ${pct(r.current_weight)}(${where}),
        建议${buy ? "加仓" : "减仓"}回到${goal}</span>
      <span class="amt ${buy ? "buy" : "sell"}">${moneySigned(r.amt)}</span>
    </li>`;
  }).join("")}</ul>`;
}

function bindRebControls() {
  const box = byId("rebal");
  box.querySelectorAll(".segmented button").forEach(b =>
    b.addEventListener("click", () => { rebMode = b.dataset.mode; renderRebalance(); }));
  byId("reb-cash")?.addEventListener("change", e => { rebCashOnly = e.target.checked; renderRebalance(); });
  byId("reb-small")?.addEventListener("change", e => { rebIgnoreSmall = e.target.checked; renderRebalance(); });
  byId("mark-reb")?.addEventListener("click", markRebalanced);
  if (RO) applyReadonly();
}

async function markRebalanced() {
  try { await api("POST", "/api/strategy/rebalanced"); toast("已记录再平衡时间"); await load(); }
  catch (e) { toast(e.message, true); }
}

function monthsSince(iso) {
  const d = new Date(iso), now = new Date();
  let m = (now.getFullYear() - d.getFullYear()) * 12 + (now.getMonth() - d.getMonth());
  if (now.getDate() < d.getDate()) m--;
  return Math.max(0, m);
}

/* ----- save / reset ----- */
async function saveTargets() {
  if (Math.abs(unallocated()) > 1e-6)
    return toast(`未分配为 ${fmtWeight(unallocated())}%,需调到 0 才能保存`, true);
  const targets = SLIDERS.map(s => ({ asset_class_id: s.id, target_weight: s.value }));
  try { await api("PUT", "/api/strategy/targets", { targets }); toast("目标已保存"); await load(); }
  catch (e) { toast(e.message, true); }
}
async function resetDefault() {
  if (!confirm("确定重置为默认示例数据?当前所有改动将被清除。")) return;
  try { await api("POST", "/api/reset"); toast("已重置为默认"); await load(); }
  catch (e) { toast(e.message, true); }
}
async function recolorClasses() {
  try { await api("POST", "/api/asset-classes/recolor"); toast("已重新配色"); await load(); }
  catch (e) { toast(e.message, true); }
}

/* =======================================================================
   ASSET-CLASS MODAL (create / edit / delete)
   ======================================================================= */
function openCreateClass() {
  acEditId = null;
  byId("ac-modal-title").textContent = "新增大类";
  setVal("m-name", ""); setVal("m-target", 0);
  setVal("m-low", 0); setVal("m-high", 100);
  byId("m-cash").checked = false;
  byId("m-delete").hidden = true;
  byId("ac-modal").classList.add("show");
}
function openEditClass(id) {
  const ac = DASH.asset_classes.find(a => a.id === id);
  if (!ac) return;
  acEditId = id;
  byId("ac-modal-title").textContent = `编辑大类 · ${ac.name}`;
  setVal("m-name", ac.name); setVal("m-target", ac.target_weight);
  setVal("m-low", ac.band_low); setVal("m-high", ac.band_high);
  byId("m-cash").checked = !!ac.is_cash;
  byId("m-delete").hidden = false;
  byId("ac-modal").classList.add("show");
}
function closeModal() { byId("ac-modal").classList.remove("show"); }

async function saveClass() {
  const payload = {
    name: val("m-name"),  // color is auto-assigned by the server (no manual picker)
    target_weight: num("m-target"), band_low: num("m-low"), band_high: num("m-high"),
    is_cash: byId("m-cash").checked,
  };
  if (!payload.name) return toast("请填写大类名称", true);
  const warn = bandWarn(payload.band_low, payload.target_weight, payload.band_high);
  try {
    if (acEditId == null) await api("POST", "/api/asset-classes", payload);
    else await api("PUT", `/api/asset-classes/${acEditId}`, payload);
    closeModal(); toast(warn || (acEditId == null ? "已新增大类" : "已更新")); await load();
  } catch (e) { toast(e.message, true); }
}

async function deleteClass() {
  if (!confirm("删除该大类及其下所有标的与交易?")) return;
  try { await api("DELETE", `/api/asset-classes/${acEditId}`); closeModal(); toast("已删除"); await load(); }
  catch (e) { toast(e.message, true); }
}

/* =======================================================================
   HOLDINGS TAB (security-centric: cost / price / pnl / target sell)
   ======================================================================= */
function flatSecurities() {
  const out = [];
  DASH.asset_classes.forEach(ac => ac.securities.forEach(s =>
    out.push({ ...s, acName: ac.name, acColor: ac.color, acId: ac.id })));
  return out;
}
function secInfo(sid) {
  for (const ac of DASH.asset_classes)
    for (const s of ac.securities)
      if (s.id === sid) return { ...s, acName: ac.name, acId: ac.id };
  return null;
}

function renderHoldingsTab() {
  const list = flatSecurities();
  const box = byId("holdings-list");
  if (!list.length) {
    box.innerHTML = `<div class="hold-empty">暂无持仓 · 点击右上角「＋ 记一笔交易」开始(填代码即可,新标的会让你选大类)</div>`;
    return;
  }
  const head = `<div class="hold-head holdgrid">
    <div class="hn">标的</div><div>大类</div><div class="r">持仓</div>
    <div class="r">买价</div><div class="r">现价</div><div class="r">盈亏</div></div>`;
  box.innerHTML = head + list.map(holdRow).join("");
  box.querySelectorAll(".holdrow").forEach(r =>
    r.addEventListener("click", () => toggleHolding(r.closest(".hblock"))));
}

function holdRow(s) {
  const cls = s.unrealized_pnl == null ? "flat" : s.unrealized_pnl > 0 ? "up" : s.unrealized_pnl < 0 ? "down" : "flat";
  const pnl = s.unrealized_pnl == null ? `<span class="pnl flat">—</span>`
    : `<span class="pnl ${cls}">${moneySigned(s.unrealized_pnl)} (${pctSigned(s.pnl_pct)})</span>`;
  return `
    <div class="hblock" data-sec="${s.id}">
      <div class="holdrow holdgrid">
        <div class="hn"><span class="caret">▶</span>
          <span class="dot" style="background:${colorVar(s.acColor)}"></span>
          <span><span class="nm">${s.name}</span> <span class="code">${s.code}</span></span></div>
        <div class="acname">${s.acName}</div>
        <div class="r">${HIDE_AMT ? "•••••" : fmtNum(s.shares)}</div>
        <div class="r">${priceMoney(s.avg_cost)}</div>
        <div class="r">${priceMoney(s.price)}</div>
        <div class="r">${pnl}</div>
      </div>
      <div class="subsec"></div>
    </div>`;
}

async function toggleHolding(block) {
  const open = block.classList.toggle("open");
  if (open && !block.dataset.loaded) {
    const sid = +block.dataset.sec;
    try {
      const txs = await api("GET", `/api/securities/${sid}/transactions`);
      block.querySelector(".subsec").innerHTML = holdDetail(sid, txs);
      block.dataset.loaded = "1";
      bindDetail(block, sid);
    } catch (e) { toast(e.message, true); block.classList.remove("open"); }
  }
}

function openBuyLots(txs) {
  // Each sell (matched_buy_id) reduces its specific buy lot. Return open lots.
  const sold = {};
  txs.forEach(t => {
    if (t.action === "sell" && t.matched_buy_id != null) sold[t.matched_buy_id] = (sold[t.matched_buy_id] || 0) + t.shares;
  });
  return txs.filter(t => t.action === "buy")
    .map(t => ({ ...t, remaining: t.shares - (sold[t.id] || 0) }))
    .filter(t => t.remaining > 1e-9);
}

function holdDetail(sid, txs) {
  const s = secInfo(sid);
  if (!s) return "";
  const lots = openBuyLots(txs);
  // Weighted expected-sell price over remaining (open) shares.
  let tw = 0, tws = 0;
  lots.forEach(t => { if (t.target_sell_price != null) { tw += t.remaining * t.target_sell_price; tws += t.remaining; } });
  const avgTarget = tws ? tw / tws : null;
  const dist = (avgTarget != null && s.price) ? (avgTarget - s.price) / s.price * 100 : null;
  const m = (k, v) => `<div class="m"><span class="k">${k}</span><span class="v">${v}</span></div>`;
  // 现价 / 市值 / 盈亏 / 占比 / 成本 are live or computed → display only (not editable).
  const summary = `<div class="hd-label">持仓汇总</div>
    <div class="hsummary">
      ${m("市值", money(s.market_value))}
      ${m("总资产占比", pct(s.weight_in_total))}
      ${m("大类内占比", pct(s.weight_in_class))}
      ${m("成本均价", priceMoney(s.avg_cost))}
      ${m("现价", priceMoney(s.price) + (s.pending ? ` <span class="px-tag">待行情</span>` : ""))}
      ${m("盈亏", s.unrealized_pnl == null ? "—" : `<span class="pnl ${s.unrealized_pnl >= 0 ? "up" : "down"}">${moneySigned(s.unrealized_pnl)} (${pctSigned(s.pnl_pct)})</span>`)}
      ${m("加权预期卖价", avgTarget == null ? "—" : priceMoney(avgTarget))}
      ${m("距卖价", dist == null ? "—" : (dist <= 0 ? `<span style="color:var(--sage)">✓ 已到价</span>` : `+${dist.toFixed(1)}%`))}
    </div>`;
  const acOpts = DASH.asset_classes.map(ac =>
    `<option value="${ac.id}" ${ac.id === s.acId ? "selected" : ""}>${ac.name}</option>`).join("");
  const actions = RO ? "" : `<div class="hactions">
    <span style="font-size:12px;color:var(--ink-3)">改大类</span>
    <select data-movesec="${sid}">${acOpts}</select>
    <span style="flex:1"></span>
    <button class="mini-btn danger" data-delsec="${sid}">删除标的</button></div>`;
  const lotsHtml = lots.length
    ? lots.map(t => lotCard(t, s.price)).join("")
    : `<div class="empty">无持仓批次(已全部卖出)</div>`;
  const lotsBlock = `<div class="hd-divider"></div>
    <div class="hd-label">持仓批次 · ${lots.length} 笔 <span style="color:var(--ink-3);font-size:11px">(卖出记录见「记录」)</span></div>
    <div class="lots">${lotsHtml}</div>`;
  return `<div class="hold-detail">${summary}${actions}${lotsBlock}</div>`;
}

// `t` is an OPEN buy lot: t.shares (original) + t.remaining (still held).
function lotCard(t, price) {
  const value = price != null ? price * t.remaining : null;            // 持有价值
  const pnl = price != null ? (price - t.price) * t.remaining : null;  // 持有盈亏
  const pnlPct = (pnl != null && t.price) ? (price - t.price) / t.price * 100 : null;
  const pnlCls = pnl == null ? "flat" : pnl > 0 ? "up" : pnl < 0 ? "down" : "flat";
  const partly = Math.abs(t.remaining - t.shares) > 1e-9;

  const dateCell = RO ? `<span class="lc-date">${t.date}</span>`
    : `<input type="date" data-f="date" value="${t.date}"/>`;
  // 买入价 · 现价 · 买入股数 · 剩余 · 当前盈亏 · 价值 · 预期卖价
  const priceField = field("买入价",
    RO ? priceMoney(t.price) : `<input type="number" min="0" step="0.001" data-f="price" value="${t.price}"/>`);
  const curField = field("现价", priceMoney(price) + (price == null ? ` <span class="px-tag">待行情</span>` : ""));
  const sharesField = field("买入数量(原)",
    RO ? (HIDE_AMT ? "•••••" : fmtNum(t.shares)) : `<input type="number" min="0" step="1" data-f="shares" value="${t.shares}"/>`);
  const remField = field("当前持有", `<span class="${partly ? "rem-partly" : ""}">${HIDE_AMT ? "•••••" : fmtNum(t.remaining)}${partly ? " (已卖部分)" : ""}</span>`);
  const pnlField = field("当前盈亏", pnl == null ? "—"
    : `<span class="pnl ${pnlCls}">${moneySigned(pnl)} (${pctSigned(pnlPct)})</span>`);
  const prog = (t.target_sell_price != null && price != null)
    ? (price >= t.target_sell_price ? `<span class="lc-prog reached">✓ 已到卖价</span>`
       : `<span class="lc-prog">距卖价 +${((t.target_sell_price - price) / price * 100).toFixed(1)}%</span>`)
    : "";
  const targetField = field("预期卖价",
    RO ? priceMoney(t.target_sell_price)
       : `<input type="number" min="0" step="0.001" data-f="target" value="${t.target_sell_price ?? ""}" placeholder="—"/>`);
  const metrics = `${priceField}${curField}${sharesField}${remField}${pnlField}${field("价值", money(value))}${targetField}${prog}`;

  const tools = RO ? "" :
    `<button class="mini-btn sell" data-sell="${t.id}" data-rem="${t.remaining}" data-bp="${t.price}">卖出</button>
     <button class="mini-btn primary" data-savelot="${t.id}">保存</button>
     <button class="mini-btn danger" data-dellot="${t.id}" title="删除该批次">✕</button>`;
  return `<div class="lotcard buy" data-tx="${t.id}">
    <div class="lc-head">
      <span class="tag buy">买入</span>${dateCell}<span class="lc-spacer"></span>${tools}
    </div>
    <div class="lc-metrics">${metrics}</div>
  </div>`;
}
function field(k, v) { return `<div class="lc-field"><span class="k">${k}</span><span class="v">${v}</span></div>`; }

function bindDetail(block, sid) {
  block.querySelectorAll(".lotcard").forEach(card => {
    const tid = +card.dataset.tx;
    card.querySelector("[data-savelot]")?.addEventListener("click", () => saveLot(sid, tid, card));
    card.querySelector("[data-dellot]")?.addEventListener("click", () => delLot(sid, tid));
    const sb = card.querySelector("[data-sell]");
    if (sb) sb.addEventListener("click", () => openSellModal(sid, tid, +sb.dataset.rem, +sb.dataset.bp));
  });
  block.querySelector("[data-delsec]")?.addEventListener("click", () => delSecurityH(sid));
  block.querySelector("[data-movesec]")?.addEventListener("change", e => moveClass(sid, +e.target.value));
}

async function saveLot(sid, tid, card) {
  const get = f => { const el = card.querySelector(`[data-f="${f}"]`); return el ? el.value.trim() : undefined; };
  const payload = {};
  const date = get("date");
  if (date) payload.date = date;
  const price = get("price");
  if (price !== undefined) {
    if (!(parseFloat(price) > 0)) return toast("价格须为正数", true);
    payload.price = parseFloat(price);
  }
  const shares = get("shares");
  if (shares !== undefined) {
    if (!(parseFloat(shares) > 0)) return toast("股数须为正数", true);
    payload.shares = parseFloat(shares);
  }
  const target = get("target");  // buys only
  if (target !== undefined) {
    if (target === "") payload.target_sell_price = null;
    else {
      if (!(parseFloat(target) > 0)) return toast("预期卖价须为正数", true);
      payload.target_sell_price = parseFloat(target);
    }
  }
  try { await api("PUT", `/api/transactions/${tid}`, payload); toast("已更新该笔交易"); await reloadKeepOpen(sid); }
  catch (e) { toast(e.message, true); }
}
async function delLot(sid, tid) {
  if (!confirm("删除该笔交易?")) return;
  try { await api("DELETE", `/api/transactions/${tid}`); toast("已删除"); await reloadKeepOpen(sid); }
  catch (e) { toast(e.message, true); }
}
async function delSecurityH(sid) {
  if (!confirm("删除该标的及其所有交易记录?")) return;
  try { await api("DELETE", `/api/securities/${sid}`); toast("已删除标的"); await load(); switchTab("holdings"); }
  catch (e) { toast(e.message, true); }
}
async function moveClass(sid, acId) {
  try { await api("PUT", `/api/securities/${sid}`, { asset_class_id: acId }); toast("已改大类"); await reloadKeepOpen(sid); }
  catch (e) { toast(e.message, true); }
}

async function reloadKeepOpen(sid) {
  await load();
  switchTab("holdings");
  const block = byId(null, `.hblock[data-sec="${sid}"]`);
  if (block) toggleHolding(block);  // re-open (lazy-fetches fresh detail)
}

/* ---------- trade modal ---------- */
function openTradeModal() {
  byId("t-ac").innerHTML = DASH.asset_classes.filter(ac => !ac.is_cash)
    .map(ac => `<option value="${ac.id}">${ac.name}</option>`).join("");
  ["t-code", "t-shares", "t-price", "t-target"].forEach(id => setVal(id, ""));
  byId("t-date").value = todayStr();
  byId("trade-modal").classList.add("show");
}
function closeTradeModal() { byId("trade-modal").classList.remove("show"); }
async function saveTrade() {
  const code = val("t-code");
  if (!code) return toast("请填写标的代码", true);
  const shares = num("t-shares"), price = num("t-price"), date = val("t-date");
  if (!date) return toast("请选择日期", true);
  if (!(shares > 0)) return toast("股数须为正", true);
  if (!(price > 0)) return toast("买入价须为正", true);
  const payload = { code, asset_class_id: num("t-ac"), action: "buy", shares, price, date };
  const tgt = val("t-target");
  if (tgt !== "") payload.target_sell_price = parseFloat(tgt);
  try {
    await api("POST", "/api/transactions", payload);
    closeTradeModal(); toast("买入已记录"); await load(); switchTab("holdings");
  } catch (e) { toast(e.message, true); }
}

/* ---------- sell modal (close a specific buy lot) ---------- */
let sellCtx = null;  // {secId, lotId, remaining, buyPrice}
function openSellModal(secId, lotId, remaining, buyPrice) {
  const s = secInfo(secId);
  sellCtx = { secId, lotId, remaining, buyPrice };
  byId("sell-info").innerHTML =
    `批次买价 <b>${priceMoney(buyPrice)}</b> · 剩余 <b>${fmtNum(remaining)}</b> 股 · 现价 ${priceMoney(s ? s.price : null)}`;
  setVal("sell-shares", remaining);
  setVal("sell-price", s && s.price != null ? s.price : "");
  byId("sell-date").value = todayStr();
  byId("sell-modal").classList.add("show");
}
async function saveSell() {
  const shares = num("sell-shares"), price = num("sell-price"), date = val("sell-date");
  if (!date) return toast("请选择日期", true);
  if (!(shares > 0)) return toast("卖出股数须为正", true);
  if (shares > sellCtx.remaining + 1e-9) return toast(`最多卖 ${fmtNum(sellCtx.remaining)} 股`, true);
  if (!(price > 0)) return toast("卖价须为正", true);
  try {
    await api("POST", "/api/transactions", { action: "sell", matched_buy_id: sellCtx.lotId,
                                             shares, price, date });
    byId("sell-modal").classList.remove("show");
    toast("卖出已记录"); await reloadKeepOpen(sellCtx.secId);
  } catch (e) { toast(e.message, true); }
}
function todayStr() {
  const d = new Date(), p = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

/* =======================================================================
   RECORDS TAB (ledger: buy / sell / deposit / withdraw + cash overview)
   ======================================================================= */
let LEDGER = null;
const recFilter = { view: "time", security: "all", kind: "all", from: "", to: "", pnl: "all" };

async function renderRecords() {
  try { LEDGER = await api("GET", "/api/ledger"); }
  catch (e) { toast(e.message, true); return; }
  renderCashOverview();
  renderRecFilters();
  renderLedgerList();
}

function renderCashOverview() {
  const s = LEDGER.summary;
  const sign = v => v > 0 ? "up" : v < 0 ? "down" : "";
  const m = (k, v, cls = "", hint = "") =>
    `<div class="m" ${hint ? `title="${hint}"` : ""}><span class="k">${k}</span><span class="v ${cls}">${v}</span></div>`;
  // 总收益 only means something once 净投入 (本金) is recorded.
  const hasPrincipal = s.net_invested > 1e-9;
  const totalReturn = hasPrincipal
    ? `<span class="v ${sign(s.total_return)}">${moneySigned(s.total_return)}</span>`
    : `<span class="v" style="color:var(--ink-3);font-size:13px">先记资金注入</span>`;
  byId("cash-overview").innerHTML =
    m("总资产", money(s.total_assets), "", "持仓市值(今日行情)+ 现金余额") +
    m("持仓市值", money(s.holdings_value), "", "全部标的按当天现价的市值") +
    m("现金余额", money(s.cash_balance), s.cash_balance < 0 ? "down" : "", "注入−移出−买入+卖出") +
    `<div class="m" title="累计注入−累计移出,你投入市场的本金"><span class="k">净投入(本金)</span>${
      `<span class="v">${money(s.net_invested)}</span>`}</div>` +
    `<div class="m" title="总资产−净投入,浮动+已实现的整体盈亏"><span class="k">总收益</span>${totalReturn}</div>` +
    m("已实现盈亏", moneySigned(s.realized_pnl), sign(s.realized_pnl), "已配对卖出的(卖价−买价)累计") +
    m("累计注入", money(s.deposits)) + m("累计移出", money(s.withdrawals));
}

function renderRecFilters() {
  const seen = new Set(), secs = [];
  LEDGER.entries.forEach(e => {
    if (e.security_id != null && !seen.has(e.security_id)) {
      seen.add(e.security_id); secs.push({ id: e.security_id, name: e.name, code: e.code });
    }
  });
  const secOpts = `<option value="all">全部标的</option>` +
    secs.map(s => `<option value="${s.id}">${s.name} (${s.code})</option>`).join("");
  const batch = recFilter.view === "batch";
  byId("rec-filters").innerHTML = `
    <div class="segmented" id="rec-view">
      <button data-view="time" class="${batch ? "" : "on"}">时间</button>
      <button data-view="batch" class="${batch ? "on" : ""}">配对分组</button>
    </div>
    <select data-f="security">${secOpts}</select>
    ${batch ? "" : `<select data-f="kind">
      <option value="all">全部类型</option><option value="buy">买入</option>
      <option value="sell">卖出</option><option value="deposit">注入</option><option value="withdraw">移出</option>
    </select>`}
    <span class="fl">从</span><input type="date" data-f="from" />
    <span class="fl">到</span><input type="date" data-f="to" />
    <select data-f="pnl">
      <option value="all">盈亏不限</option><option value="profit">仅盈利</option><option value="loss">仅亏损</option>
    </select>`;
  byId("rec-filters").querySelectorAll("#rec-view button").forEach(b =>
    b.addEventListener("click", () => { recFilter.view = b.dataset.view; renderRecFilters(); renderLedgerList(); }));
  byId("rec-filters").querySelectorAll("[data-f]").forEach(el => {
    el.value = recFilter[el.dataset.f];
    el.addEventListener("change", () => { recFilter[el.dataset.f] = el.value; renderLedgerList(); });
  });
}

function renderLedgerList() {
  if (recFilter.view === "batch") return renderBatchList();
  const f = recFilter;
  const list = LEDGER.entries.filter(e => {
    if (f.kind !== "all" && e.kind !== f.kind) return false;
    if (f.security !== "all" && String(e.security_id) !== f.security) return false;
    if (f.from && e.date < f.from) return false;
    if (f.to && e.date > f.to) return false;
    if (f.pnl !== "all") {
      if (e.kind !== "sell" || e.realized_pnl == null) return false;
      if (f.pnl === "profit" && !(e.realized_pnl > 0)) return false;
      if (f.pnl === "loss" && !(e.realized_pnl < 0)) return false;
    }
    return true;
  });
  const box = byId("ledger-list");
  if (!list.length) { box.innerHTML = `<div class="ledger-empty">没有符合条件的记录</div>`; return; }
  box.innerHTML = list.map(ledgerRow).join("");
  box.querySelectorAll("[data-delcf]").forEach(b => b.addEventListener("click", () => delCashFlow(+b.dataset.delcf)));
}

function ledgerRow(e) {
  const tags = { buy: "买入", sell: "卖出", deposit: "注入", withdraw: "移出" };
  const isSec = e.kind === "buy" || e.kind === "sell";
  const signed = ((e.kind === "sell" || e.kind === "deposit") ? 1 : -1) * (e.amount || 0);
  let who, mid = "";
  if (isSec) {
    if (e.kind === "buy") {
      who = `${e.name} <span class="sub">${e.code}</span>`;
      mid = `买价 ${priceMoney(e.price)}`;
    } else {
      // The matched buy is shown inline (↳ 平仓自 …) so the pairing is obvious
      // without hunting for a badge elsewhere in the list.
      const ref = `↳ 平仓自 ${e.matched_buy_date || "—"} 买入 @${priceMoney(e.buy_price)}`;
      who = `${e.name} <span class="sub">${e.code}</span><span class="sub link">${ref}</span>`;
      const cls = e.realized_pnl == null ? "flat" : e.realized_pnl >= 0 ? "up" : "down";
      const pp = (e.buy_price) ? (e.price - e.buy_price) / e.buy_price * 100 : null;
      const pnl = e.realized_pnl == null ? "" : ` · 盈亏 <span class="pnl ${cls}">${moneySigned(e.realized_pnl)} (${pctSigned(pp)})</span>`;
      mid = `卖 ${priceMoney(e.price)}${pnl}`;
    }
  } else {
    who = e.note || (e.kind === "deposit" ? "资金注入" : "资金移出");
  }
  const tools = (!isSec && !RO)
    ? `<span class="ltools"><button class="mini-btn danger" data-delcf="${e.id}" title="删除">✕</button></span>`
    : `<span class="ltools"></span>`;
  return `<div class="lrow">
    <span class="tag ${e.kind}">${tags[e.kind]}</span>
    <span class="ldate">${e.date}</span>
    <span class="lwho">${who}</span>
    <span class="lmid">${mid}</span>
    <span class="lamt ${signed >= 0 ? "up" : "down"}">${moneySigned(signed)}</span>
    ${tools}
  </div>`;
}

/* ---------- batch (配对分组) view ---------- */
function buildBatches(entries) {
  const sellsByBuy = {};
  entries.filter(e => e.kind === "sell").forEach(s => {
    (sellsByBuy[s.matched_buy_id] = sellsByBuy[s.matched_buy_id] || []).push(s);
  });
  return entries.filter(e => e.kind === "buy").map(buy => {
    const sells = (sellsByBuy[buy.id] || []).slice().sort((a, b) => a.date < b.date ? -1 : 1);
    const sold = sells.reduce((a, s) => a + s.shares, 0);
    const realized = sells.reduce((a, s) => a + (s.realized_pnl || 0), 0);
    return { buy, sells, realized, remaining: buy.shares - sold };
  });
}

function renderBatchList() {
  const f = recFilter;
  let batches = buildBatches(LEDGER.entries);
  if (f.security !== "all") batches = batches.filter(b => String(b.buy.security_id) === f.security);
  if (f.from) batches = batches.filter(b => b.buy.date >= f.from);
  if (f.to) batches = batches.filter(b => b.buy.date <= f.to);
  if (f.pnl !== "all") batches = batches.filter(b => b.sells.length && (f.pnl === "profit" ? b.realized > 0 : b.realized < 0));
  batches.sort((a, b) => a.buy.date < b.buy.date ? 1 : a.buy.date > b.buy.date ? -1 : b.buy.id - a.buy.id);

  const box = byId("ledger-list");
  let html = batches.length ? batches.map(batchCard).join("") : `<div class="ledger-empty">没有符合条件的批次</div>`;
  // Cash flows have no batch — list them below when not filtered by security/pnl.
  if (f.security === "all" && f.pnl === "all") {
    const cash = LEDGER.entries.filter(e => e.kind === "deposit" || e.kind === "withdraw");
    if (cash.length) html += `<div class="hd-label" style="margin:18px 4px 6px">资金流水</div>` + cash.map(ledgerRow).join("");
  }
  box.innerHTML = html;
  box.querySelectorAll(".batch-head").forEach(h => h.addEventListener("click", () => h.closest(".batch").classList.toggle("open")));
  box.querySelectorAll("[data-delcf]").forEach(b => b.addEventListener("click", () => delCashFlow(+b.dataset.delcf)));
}

function batchCard(b) {
  const realCls = b.realized > 0 ? "up" : b.realized < 0 ? "down" : "flat";
  const sellsHtml = b.sells.length
    ? b.sells.map(s => {
        const cls = s.realized_pnl >= 0 ? "up" : "down";
        const pp = b.buy.price ? (s.price - b.buy.price) / b.buy.price * 100 : null;
        return `<div class="bsell">
          <span class="bs-date">${s.date}</span>
          <span>卖 ${HIDE_AMT ? "•••••" : fmtNum(s.shares)} 股 @${priceMoney(s.price)}</span>
          <span class="pnl ${cls}" style="margin-left:auto">${moneySigned(s.realized_pnl)} (${pctSigned(pp)})</span>
        </div>`;
      }).join("")
    : `<div class="bsell bempty">尚未卖出 · 当前持有 ${HIDE_AMT ? "•••••" : fmtNum(b.remaining)} 股</div>`;
  const remNote = b.remaining > 1e-9 ? `持有 ${HIDE_AMT ? "•••••" : fmtNum(b.remaining)}` : `<span style="color:var(--ink-3)">已清仓</span>`;
  return `<div class="batch ${b.sells.length ? "open" : ""}">
    <div class="batch-head">
      <span class="caret">▶</span>
      <span class="bname">${b.buy.name} <span class="sub">${b.buy.code}</span></span>
      <span class="bmeta">买入 ${b.buy.date} @${priceMoney(b.buy.price)} · ${HIDE_AMT ? "•••••" : fmtNum(b.buy.shares)}股 · ${remNote}</span>
      <span class="breal pnl ${realCls}">已实现 ${moneySigned(b.realized)}</span>
    </div>
    <div class="batch-sells">${sellsHtml}</div>
  </div>`;
}

/* ---------- cash-flow modal ---------- */
function openCashModal() {
  byId("cf-dir").value = "in";
  setVal("cf-amount", ""); setVal("cf-note", "");
  byId("cf-date").value = todayStr();
  byId("cash-modal").classList.add("show");
}
async function saveCashFlow() {
  const amount = num("cf-amount"), date = val("cf-date");
  if (!date) return toast("请选择日期", true);
  if (!(amount > 0)) return toast("金额须为正数", true);
  const payload = { date, direction: val("cf-dir"), amount, note: val("cf-note") || null };
  try {
    await api("POST", "/api/cashflows", payload);
    byId("cash-modal").classList.remove("show");
    toast("已记录资金流水"); await load(); await renderRecords();
  } catch (e) { toast(e.message, true); }
}
async function delCashFlow(id) {
  if (!confirm("删除这笔资金流水?")) return;
  try { await api("DELETE", `/api/cashflows/${id}`); toast("已删除"); await load(); await renderRecords(); }
  catch (e) { toast(e.message, true); }
}

/* ---------- helpers ---------- */
function byId(id, sel) { return sel ? document.querySelector(sel) : document.getElementById(id); }
function val(id) { return byId(id).value.trim(); }
function num(id) { return parseFloat(byId(id).value); }
function setVal(id, v) { byId(id).value = v; }
function clamp(v, a = 0, b = 100) { return Math.max(a, Math.min(b, v)); }
function fmtWeight(v) { return v == null ? "—" : (Number.isInteger(v) ? v : +v.toFixed(1)); }
function fmtNum(v) {
  if (v == null) return "—";
  return (Number.isInteger(v) ? v : +v.toFixed(2)).toLocaleString("en-US");
}
// Per-share price: keep meaningful decimals, strip trailing zeros. Masked when hiding amounts.
function priceMoney(v) {
  if (HIDE_AMT) return "•••••";
  if (v == null) return "—";
  return "¥" + (+v).toFixed(3).replace(/\.?0+$/, "");
}
