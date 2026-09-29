/* 리밸런싱 엔진 화면 (robo-rebalance)
 * 목표 비중 플랜 · 현재 비중/이탈률 · 주문 제안/실행 · 입출금/배당 이벤트 · 실행 이력
 * app.html 메인 모듈에서 initRebalanceView() / onRebalanceViewActivated(view) 로 연결한다. */
import { api, setToast, escHtml, fmt, fmtPct } from "/js/common.js";

const $ = (id) => document.getElementById(id);
const won = (n, d = 0) => `${fmt(n, d)}원`;
const ts = (iso) => iso ? new Date(iso).toLocaleString("ko-KR", { hour12: false }) : "-";
const PERIOD_LABEL = { none: "사용 안 함", monthly: "매월", quarterly: "매분기", yearly: "매년" };
const TRIGGER_LABEL = { TIME: "시간", DRIFT: "이탈률", CASHFLOW: "현금흐름", MANUAL: "수동" };
const STATUS_BADGE = {
  executed: `<span class="badge-buy">체결</span>`, proposed: `<span class="badge-hold">제안</span>`,
  skipped: `<span class="badge-hold">생략</span>`, failed: `<span class="badge-sell">실패</span>`,
};
const sideBadge = (s) => s === "BUY" ? `<span class="badge-buy">매수</span>` : `<span class="badge-sell">매도</span>`;

let weightChart = null;
let targetRows = [];   // [{symbol, name, weight_pct}]
let lastProposal = null;

/* ── 플랜 편집 ────────────────────────────────────────────── */
function renderTargets() {
  const tbody = $("rb-targets-body");
  if (!targetRows.length) {
    tbody.innerHTML = `<tr><td colspan="4" class="text-center" style="color:var(--text-mute);">종목을 추가하세요. 합계가 100% 미만이면 나머지는 현금으로 배분됩니다.</td></tr>`;
  } else {
    tbody.innerHTML = targetRows.map((t, i) => `
      <tr>
        <td class="font-mono text-xs">${escHtml(t.symbol)}</td>
        <td>${escHtml(t.name || "")}</td>
        <td style="text-align:right"><input type="number" min="0" max="100" step="0.5" value="${t.weight_pct}" data-idx="${i}" class="input rb-w" style="width:90px;text-align:right;" /></td>
        <td style="text-align:center"><button class="btn-secondary text-xs rb-del" data-idx="${i}">삭제</button></td>
      </tr>`).join("");
    tbody.querySelectorAll(".rb-w").forEach(el => el.addEventListener("input", e => {
      targetRows[+e.target.dataset.idx].weight_pct = parseFloat(e.target.value) || 0; updateSum();
    }));
    tbody.querySelectorAll(".rb-del").forEach(el => el.addEventListener("click", e => {
      targetRows.splice(+e.target.dataset.idx, 1); renderTargets();
    }));
  }
  updateSum();
}

function updateSum() {
  const sum = targetRows.reduce((a, t) => a + (parseFloat(t.weight_pct) || 0), 0);
  const cash = Math.max(0, 100 - sum);
  $("rb-target-sum").innerHTML = `주식 합계 <b>${sum.toFixed(1)}%</b> · 현금 <b>${cash.toFixed(1)}%</b>` +
    (sum > 100 ? ` <span class="text-red-500">합계가 100%를 초과합니다</span>` : "");
}

async function addTarget() {
  const raw = $("rb-add-symbol").value.trim();
  const w = parseFloat($("rb-add-weight").value);
  if (!raw) return setToast("종목코드를 입력하세요.", "error");
  if (!(w >= 0)) return setToast("비중(%)을 입력하세요.", "error");
  try {
    const q = await api(`/api/paper/stocks/quote?symbol=${encodeURIComponent(raw)}`);
    const exist = targetRows.find(t => t.symbol === q.symbol);
    if (exist) exist.weight_pct = w; else targetRows.push({ symbol: q.symbol, name: q.name, weight_pct: w });
    $("rb-add-symbol").value = ""; $("rb-add-weight").value = "";
    renderTargets();
  } catch (e) { setToast(e.message, "error"); }
}

function importFromPositions(snapshot) {
  // 현재 보유 비중을 목표로 복사
  targetRows = snapshot.rows.filter(r => r.quantity > 0).map(r => ({ symbol: r.symbol, name: r.name, weight_pct: r.current_weight_pct }));
  renderTargets();
}

function fillPlanForm(plan) {
  targetRows = (plan.targets || []).map(t => ({ ...t }));
  renderTargets();
  $("rb-name").value = plan.name || "";
  $("rb-period").value = plan.time_period || "none";
  $("rb-drift-enabled").checked = !!plan.drift_enabled;
  $("rb-drift").value = plan.drift_threshold_pct;
  $("rb-cf-enabled").checked = !!plan.cashflow_enabled;
  $("rb-cf-min").value = plan.cashflow_min_amount;
  $("rb-auto").checked = !!plan.auto_execute;
  $("rb-min-order").value = plan.min_order_amount;
  $("rb-active").checked = !!plan.is_active;
}

async function savePlan() {
  const body = {
    name: $("rb-name").value.trim() || undefined,
    is_active: $("rb-active").checked,
    targets: targetRows.map(t => ({ symbol: t.symbol, name: t.name || "", weight_pct: parseFloat(t.weight_pct) || 0 })),
    time_period: $("rb-period").value,
    drift_enabled: $("rb-drift-enabled").checked,
    drift_threshold_pct: parseFloat($("rb-drift").value) || 5,
    cashflow_enabled: $("rb-cf-enabled").checked,
    cashflow_min_amount: parseFloat($("rb-cf-min").value) || 0,
    auto_execute: $("rb-auto").checked,
    min_order_amount: parseFloat($("rb-min-order").value) || 0,
  };
  try {
    await api("/api/rebalance/plan", { method: "PUT", body });
    setToast("리밸런싱 플랜을 저장했습니다.", "ok");
    await loadStatus();
  } catch (e) { setToast(e.message, "error"); }
}

/* ── 현황 ────────────────────────────────────────────────── */
async function loadStatus() {
  try {
    const r = await api("/api/rebalance/status");
    const { plan, snapshot: s, triggers } = r;
    fillPlanForm(plan);
    $("rb-kpis").innerHTML = [
      kpi("총 자산 (현금+주식)", won(s.total_asset)),
      kpi("현금 비중", `${s.cash_weight_pct}% <span class="text-xs" style="color:var(--text-mute)">목표 ${s.cash_target_pct}%</span>`),
      kpi("최대 이탈", `${s.max_drift_pct}%p`, s.drift_exceeded ? "text-red-500" : "text-emerald-600"),
      kpi("다음 시간 리밸런싱", plan.time_period === "none" ? "-" : `${PERIOD_LABEL[plan.time_period]}<div class="text-xs font-normal" style="color:var(--text-mute)">${ts(plan.next_run_at)}</div>`),
    ].join("");
    const badges = [];
    if (triggers.time_due) badges.push(`<span class="badge-sell">시간 트리거 도래</span>`);
    if (triggers.drift_due) badges.push(`<span class="badge-sell">이탈률 초과 (허용 ${plan.drift_threshold_pct}%p)</span>`);
    if (!badges.length) badges.push(`<span class="badge-buy">트리거 조건 미충족 — 목표 비중 유지 중</span>`);
    badges.push(`<span class="badge-hold">${plan.auto_execute ? "자동 체결" : "제안만 생성 (수동 승인)"}</span>`);
    $("rb-trigger-badges").innerHTML = badges.join(" ");

    renderWeightTable(s);
    renderWeightChart(s);
    $("rb-import-positions").onclick = () => importFromPositions(s);
  } catch (e) { setToast(e.message, "error"); }
  loadRuns(); loadCashflows();
}

const kpi = (label, value, cls = "") => `
  <div class="rounded-xl border border-white/10 bg-black/20 p-3">
    <div class="text-xs" style="color:var(--text-mute);">${escHtml(label)}</div>
    <div class="text-lg font-bold ${cls}">${value}</div>
  </div>`;

function renderWeightTable(s) {
  const rows = [...s.rows, { symbol: "CASH", name: "현금", quantity: "", price: null, current_amount: s.cash,
    current_weight_pct: s.cash_weight_pct, target_weight_pct: s.cash_target_pct, drift_pct: s.cash_drift_pct, in_plan: true }];
  $("rb-weight-table").innerHTML = `<table><thead><tr><th>종목</th><th style="text-align:right">수량</th><th style="text-align:right">평가액</th><th style="text-align:right">현재 비중</th><th style="text-align:right">목표 비중</th><th style="text-align:right">이탈(%p)</th></tr></thead><tbody>${
    rows.map(r => `<tr${r.in_plan ? "" : ' style="opacity:.7"'}><td>${escHtml(r.name)} <span class="text-xs font-mono" style="color:var(--text-mute)">${escHtml(r.symbol)}</span>${r.in_plan ? "" : ' <span class="badge-hold text-xs">플랜 외 → 전량 매도</span>'}</td>
      <td style="text-align:right">${r.quantity === "" ? "-" : fmt(r.quantity)}</td><td style="text-align:right">${won(r.current_amount)}</td>
      <td style="text-align:right">${r.current_weight_pct}%</td><td style="text-align:right">${r.target_weight_pct}%</td>
      <td style="text-align:right" class="${Math.abs(r.drift_pct) >= 0.01 ? (r.drift_pct > 0 ? "text-red-500" : "text-emerald-600") : ""}">${r.drift_pct > 0 ? "+" : ""}${r.drift_pct}</td></tr>`).join("")}</tbody></table>`;
}

function renderWeightChart(s) {
  const cats = [...s.rows.map(r => r.name), "현금"];
  const cur = [...s.rows.map(r => r.current_weight_pct), s.cash_weight_pct];
  const tgt = [...s.rows.map(r => r.target_weight_pct), s.cash_target_pct];
  const opts = {
    chart: { type: "bar", height: 260, toolbar: { show: false }, background: "transparent" },
    theme: { mode: document.documentElement.dataset.theme === "light" ? "light" : "dark" },
    series: [{ name: "현재 비중", data: cur }, { name: "목표 비중", data: tgt }],
    xaxis: { categories: cats }, yaxis: { labels: { formatter: v => `${v}%` } },
    plotOptions: { bar: { columnWidth: "55%", borderRadius: 3 } }, dataLabels: { enabled: false },
    colors: ["#2962ff", "#089981"], legend: { position: "top" },
    tooltip: { y: { formatter: v => `${v}%` } },
  };
  if (weightChart) { weightChart.updateOptions(opts); return; }
  if (window.ApexCharts) { weightChart = new ApexCharts($("rb-weight-chart"), opts); weightChart.render(); }
}

/* ── 제안/실행 ────────────────────────────────────────────── */
function renderOrders(orders, containerId, opts = {}) {
  const el = $(containerId);
  if (!orders?.length) { el.innerHTML = `<div class="text-sm" style="color:var(--text-mute);">${opts.empty || "생성된 주문이 없습니다 (이미 목표 비중 근처이거나 최소 주문금액 미만)."}</div>`; return; }
  el.innerHTML = `<table><thead><tr><th>매매</th><th>종목</th><th style="text-align:right">수량</th><th style="text-align:right">단가</th><th style="text-align:right">금액</th><th>상태</th></tr></thead><tbody>${
    orders.map(o => `<tr><td>${sideBadge(o.side)}</td><td>${escHtml(o.name)} <span class="text-xs font-mono" style="color:var(--text-mute)">${escHtml(o.symbol)}</span></td>
      <td style="text-align:right">${fmt(o.quantity)}</td><td style="text-align:right">${won(o.price)}</td><td style="text-align:right">${won(o.amount)}</td>
      <td class="text-xs">${escHtml(o.status)}${o.error ? ` <span class="text-red-500">${escHtml(o.error)}</span>` : ""}</td></tr>`).join("")}</tbody></table>`;
}

async function previewRebalance() {
  $("rb-proposal").innerHTML = `<span style="color:var(--text-mute);">현재 시세 기준으로 주문을 산출 중…</span>`;
  try {
    lastProposal = await api("/api/rebalance/preview", { method: "POST" });
    renderOrders(lastProposal.orders, "rb-proposal");
    $("rb-proposal-summary").textContent = `예상 회전 금액 ${won(lastProposal.estimated_turnover)} · 실행 후 현금 ${won(lastProposal.estimated_cash_after)}` +
      (lastProposal.skipped?.length ? ` · 시세 실패 ${lastProposal.skipped.map(x => x.symbol).join(", ")}` : "");
    $("rb-execute").disabled = !lastProposal.orders.length;
  } catch (e) { $("rb-proposal").innerHTML = `<span class="text-red-500">${escHtml(e.message)}</span>`; }
}

async function executeRebalance(runId = null) {
  if (!confirm(runId ? "이 제안을 현재 시세로 다시 산출해 체결합니다. 계속할까요?" : "제안된 주문을 모의계좌에 체결합니다. 계속할까요?")) return;
  try {
    const r = await api("/api/rebalance/execute", { method: "POST", body: runId ? { run_id: runId } : { note: "화면에서 수동 실행" } });
    const filled = r.orders.filter(o => o.status === "filled").length;
    setToast(`리밸런싱 ${r.status === "executed" ? "체결" : "생략"} — 주문 ${filled}/${r.orders.length}건`, r.status === "executed" ? "ok" : "error");
    renderOrders(r.orders, "rb-proposal");
    $("rb-execute").disabled = true;
    await loadStatus();
  } catch (e) { setToast(e.message, "error"); }
}

async function checkTriggers() {
  try {
    const r = await api("/api/rebalance/check", { method: "POST" });
    const msg = r.trigger ? `${TRIGGER_LABEL[r.trigger]} 트리거 충족 → ${r.status === "executed" ? "자동 체결" : "제안 생성"}`
      : `트리거 미충족 (시간 ${r.time_due ? "도래" : "대기"} · 최대 이탈 ${r.max_drift_pct ?? "-"}%p)`;
    setToast(msg, r.trigger ? "ok" : "error");
    await loadStatus();
  } catch (e) { setToast(e.message, "error"); }
}

/* ── 현금흐름 ────────────────────────────────────────────── */
async function submitCashflow() {
  const kind = $("rb-cf-kind").value;
  const amount = parseFloat($("rb-cf-amount").value);
  if (!(amount > 0)) return setToast("금액을 입력하세요.", "error");
  const body = { kind, amount, symbol: kind === "DIVIDEND" ? $("rb-cf-symbol").value.trim() : "", memo: $("rb-cf-memo").value.trim() };
  try {
    const r = await api("/api/rebalance/cashflow", { method: "POST", body });
    const label = { DEPOSIT: "입금", WITHDRAW: "출금", DIVIDEND: "배당" }[kind];
    setToast(`${label} ${won(amount)} 반영 (현금 ${won(r.event.cash_after)})` + (r.run ? ` → 리밸런싱 ${r.run.status === "executed" ? "자동 체결" : "제안 생성"}` : ""), "ok");
    $("rb-cf-amount").value = ""; $("rb-cf-memo").value = "";
    await loadStatus();
  } catch (e) { setToast(e.message, "error"); }
}

async function loadCashflows() {
  try {
    const r = await api("/api/rebalance/cashflows?limit=20");
    const label = { DEPOSIT: `<span class="badge-buy">입금</span>`, WITHDRAW: `<span class="badge-sell">출금</span>`, DIVIDEND: `<span class="badge-buy">배당</span>` };
    $("rb-cashflows").innerHTML = r.events.length ? `<table><thead><tr><th>시각</th><th>구분</th><th style="text-align:right">금액</th><th>종목</th><th>메모</th><th style="text-align:right">반영 후 현금</th><th>리밸런싱</th></tr></thead><tbody>${
      r.events.map(e => `<tr><td class="text-xs">${ts(e.created_at)}</td><td>${label[e.kind] || e.kind}</td><td style="text-align:right">${won(e.amount)}</td><td class="font-mono text-xs">${escHtml(e.symbol || "-")}</td><td class="text-xs">${escHtml(e.memo || "")}</td><td style="text-align:right">${won(e.cash_after)}</td><td class="text-xs">${e.rebalance_run_id ? "연결됨" : "-"}</td></tr>`).join("")}</tbody></table>`
      : `<div class="text-sm" style="color:var(--text-mute);">아직 입출금·배당 이벤트가 없습니다.</div>`;
  } catch (e) { $("rb-cashflows").innerHTML = `<span class="text-red-500">${escHtml(e.message)}</span>`; }
}

/* ── 실행 이력 ────────────────────────────────────────────── */
async function loadRuns() {
  try {
    const r = await api("/api/rebalance/runs?limit=20");
    if (!r.runs.length) { $("rb-runs").innerHTML = `<div class="text-sm" style="color:var(--text-mute);">아직 리밸런싱 이력이 없습니다.</div>`; return; }
    $("rb-runs").innerHTML = r.runs.map(run => {
      const filled = run.orders.filter(o => o.status === "filled").length;
      const wchg = Object.keys(run.target_weights).map(k => {
        const b = run.before_weights[k] ?? 0, a = run.after_weights?.[k], t = run.target_weights[k];
        return `<span class="text-xs mr-2">${escHtml(k === "CASH" ? "현금" : k)}: ${b}%${a !== undefined ? ` → ${a}%` : ""} <span style="color:var(--text-mute)">(목표 ${t}%)</span></span>`;
      }).join("");
      return `<div class="rounded-lg p-3 mb-2" style="background:var(--surf2);border:1px solid var(--border);">
        <div class="flex flex-wrap items-center gap-2 text-sm">
          <span class="badge-hold">${TRIGGER_LABEL[run.trigger] || run.trigger}</span> ${STATUS_BADGE[run.status] || run.status}
          <span class="text-xs" style="color:var(--text-mute)">${ts(run.created_at)} · 자산 ${won(run.total_asset)} · 최대 이탈 ${run.max_drift_pct}%p · 주문 ${filled}/${run.orders.length}건</span>
          ${run.status === "proposed" ? `<button class="btn-green text-xs ml-auto rb-approve" data-id="${run.id}">승인·체결</button>` : ""}
        </div>
        <div class="text-xs mt-1" style="color:var(--text-dim)">${escHtml(run.note || "")}</div>
        <div class="mt-1">${wchg}</div>
        <details class="mt-1"><summary class="text-xs cursor-pointer" style="color:var(--text-mute)">주문 상세</summary><div id="rb-run-${run.id}" class="mt-1"></div></details>
      </div>`;
    }).join("");
    r.runs.forEach(run => renderOrders(run.orders, `rb-run-${run.id}`, { empty: "주문 없음" }));
    $("rb-runs").querySelectorAll(".rb-approve").forEach(b => b.addEventListener("click", () => executeRebalance(b.dataset.id)));
  } catch (e) { $("rb-runs").innerHTML = `<span class="text-red-500">${escHtml(e.message)}</span>`; }
}

/* ── 초기화 ─────────────────────────────────────────────── */
export function initRebalanceView() {
  const on = (id, ev, fn) => $(id)?.addEventListener(ev, fn);
  on("rb-add", "click", addTarget);
  on("rb-add-symbol", "keydown", e => { if (e.key === "Enter") addTarget(); });
  on("rb-save", "click", savePlan);
  on("rb-refresh", "click", loadStatus);
  on("rb-preview", "click", previewRebalance);
  on("rb-execute", "click", () => executeRebalance());
  on("rb-check", "click", checkTriggers);
  on("rb-cf-submit", "click", submitCashflow);
  on("rb-cf-kind", "change", () => { $("rb-cf-symbol-wrap").classList.toggle("hidden", $("rb-cf-kind").value !== "DIVIDEND"); });
}

export function onRebalanceViewActivated(view) {
  if (view === "robo-rebalance") loadStatus();
}
