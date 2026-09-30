/* TradingView 연동 화면 (indicator-tradingview)
 * Webhook 신호 수신 설정 · 수신 이력 · Strategy Tester ↔ LEAN 교차 검증
 * app.html 메인 모듈에서 initTradingViewView() / onTradingViewViewActivated(view) 로 연결한다. */
import { api, setToast, escHtml, fmt } from "/js/common.js";

const $ = (id) => document.getElementById(id);
const ts = (iso) => iso ? new Date(iso).toLocaleString("ko-KR", { hour12: false }) : "-";
const STATUS_BADGE = {
  filled: `<span class="badge-buy">체결</span>`, alert: `<span class="badge-hold">알림</span>`,
  duplicate: `<span class="badge-hold">중복</span>`, rejected: `<span class="badge-sell">거절</span>`, error: `<span class="badge-sell">오류</span>`,
};
const VERDICT_CLS = { "일치": "badge-buy", "부분 일치": "badge-hold", "불일치": "badge-sell", "비교 불가": "badge-hold" };
let cmpChart = null;

async function loadWebhookInfo() {
  try {
    const r = await api("/api/tradingview/webhook-info");
    $("tv-webhook-url").textContent = r.webhook_url;
    $("tv-alert-template").textContent = r.alert_template_text;
    $("tv-notes").innerHTML = r.notes.map(n => `<li>${escHtml(n)}</li>`).join("");
  } catch (e) { setToast(e.message, "error"); }
}

async function loadSignals() {
  try {
    const r = await api("/api/tradingview/signals?limit=30");
    $("tv-signals").innerHTML = r.signals.length ? `<table><thead><tr><th>수신 시각</th><th>전략</th><th>종목</th><th>방향</th><th style="text-align:right">수량</th><th style="text-align:right">신호가</th><th style="text-align:right">체결가</th><th>처리</th><th>메시지</th></tr></thead><tbody>${
      r.signals.map(s => `<tr><td class="text-xs">${ts(s.created_at)}</td><td class="text-xs">${escHtml(s.strategy || "-")}</td><td class="font-mono text-xs">${escHtml(s.symbol)}</td>
        <td>${s.side === "BUY" ? '<span class="badge-buy">매수</span>' : s.side === "SELL" ? '<span class="badge-sell">매도</span>' : '<span class="badge-hold">알림</span>'}</td>
        <td style="text-align:right">${fmt(s.quantity)}</td><td style="text-align:right">${s.signal_price ? fmt(s.signal_price) : "-"}</td><td style="text-align:right">${s.fill_price ? fmt(s.fill_price) : "-"}</td>
        <td>${STATUS_BADGE[s.status] || escHtml(s.status)}</td><td class="text-xs" style="color:var(--text-mute)">${escHtml(s.message || "")}</td></tr>`).join("")}</tbody></table>`
      : `<div class="text-sm" style="color:var(--text-mute);">아직 수신된 TradingView 신호가 없습니다. 아래 '테스트 신호 보내기'로 흐름을 확인해 보세요.</div>`;
  } catch (e) { $("tv-signals").innerHTML = `<span class="text-red-500">${escHtml(e.message)}</span>`; }
}

async function sendTestSignal() {
  const token = $("tv-test-token").value.trim();
  if (!token) return setToast("발급한 API 키(token)를 입력하세요.", "error");
  const body = { token, strategy: "테스트 알림", ticker: $("tv-test-ticker").value.trim() || "005930",
    action: $("tv-test-action").value, contracts: Number($("tv-test-qty").value || 1), comment: "화면에서 보낸 테스트" };
  try {
    const r = await api("/api/webhooks/tradingview", { method: "POST", body });
    setToast(`Webhook 처리: ${r.status} — ${r.message}`, r.ok ? "ok" : "error");
    loadSignals();
  } catch (e) { setToast(e.message, "error"); }
}

/* ── Strategy Tester ↔ LEAN 교차 검증 ── */
async function runCompare() {
  const num = (id) => { const v = $(id).value.trim(); return v === "" ? null : Number(v); };
  const body = {
    ticker: $("tvc-ticker").value.trim(), strategy: $("tvc-strategy").value,
    start_date: $("tvc-start").value, end_date: $("tvc-end").value, initial_cash: Number($("tvc-cash").value || 10000),
    short_window: Number($("tvc-short").value || 20), long_window: Number($("tvc-long").value || 60),
    tv_metrics: { net_profit_pct: num("tvc-net"), max_drawdown_pct: num("tvc-mdd"), total_trades: num("tvc-trades"), win_rate_pct: num("tvc-win"), sharpe_ratio: num("tvc-sharpe") },
    tv_trades_csv: $("tvc-csv").value.trim() || null,
  };
  if (!body.ticker || !body.start_date || !body.end_date) return setToast("종목·기간을 입력하세요.", "error");
  $("tvc-result").innerHTML = `<div class="text-sm" style="color:var(--text-mute);">LEAN 백테스트 실행 중… (Docker 실행 시 30초~1분)</div>`;
  try {
    const r = await api("/api/tradingview/compare", { method: "POST", body });
    renderCompare(r);
    loadComparisons();
  } catch (e) { $("tvc-result").innerHTML = `<span class="text-red-500">${escHtml(e.message)}</span>`; }
}

function renderCompare(r) {
  const fmtv = (v, k) => v == null ? "-" : (k === "total_trades" ? fmt(v) : Number(v).toFixed(2));
  $("tvc-result").innerHTML = `
    <div class="flex flex-wrap items-center gap-2 mb-2">
      <span class="${VERDICT_CLS[r.verdict] || "badge-hold"}">${escHtml(r.verdict)}</span>
      <span class="text-sm font-semibold">${escHtml(r.ticker)} · ${escHtml(r.strategy_label || r.strategy)} · ${r.period.start} ~ ${r.period.end}</span>
      <span class="text-xs" style="color:var(--text-mute)">LEAN 엔진: ${escHtml(r.lean.engine || "")}${r.lean.lean_ok ? "" : " (미실행 → pandas 대체)"}</span>
    </div>
    <table><thead><tr><th>지표</th><th style="text-align:right">TradingView</th><th style="text-align:right">LEAN</th><th style="text-align:right">차이 (LEAN − TV)</th></tr></thead><tbody>${
      r.rows.map(x => `<tr><td>${escHtml(x.label)}</td><td style="text-align:right">${fmtv(x.tradingview, x.key)}</td><td style="text-align:right">${fmtv(x.lean, x.key)}</td>
        <td style="text-align:right" class="${x.diff == null ? "" : Math.abs(x.diff) <= 3 ? "text-emerald-600" : Math.abs(x.diff) <= 10 ? "text-amber-500" : "text-red-500"}">${x.diff == null ? "-" : (x.diff > 0 ? "+" : "") + Number(x.diff).toFixed(2)}</td></tr>`).join("")}</tbody></table>
    <div id="tvc-chart" class="mt-3"></div>
    <h4 class="text-sm font-semibold mt-3 mb-1">차이가 나는 이유 점검</h4>
    <ul class="text-xs list-disc ml-5 space-y-1" style="color:var(--text-dim)">${r.causes.map(c => `<li>${escHtml(c)}</li>`).join("")}</ul>
    <p class="text-xs mt-2" style="color:var(--text-mute)">판정 기준: 수익률·MDD 차이 3%p 이하 → 일치, 10%p 이하 → 부분 일치. TV 값 출처: ${escHtml(r.tradingview.source || "직접 입력")}</p>`;
  const pts = r.lean_points || [];
  if (pts.length && window.ApexCharts) {
    const opts = { chart: { type: "area", height: 220, toolbar: { show: false }, background: "transparent" },
      theme: { mode: document.documentElement.dataset.theme === "light" ? "light" : "dark" },
      series: [{ name: "LEAN 자산 곡선", data: pts.map(p => [new Date(p.date).getTime(), p.value]) }],
      xaxis: { type: "datetime" }, dataLabels: { enabled: false }, stroke: { width: 2 }, colors: ["#2962ff"] };
    if (cmpChart) cmpChart.destroy();
    cmpChart = new ApexCharts($("tvc-chart"), opts); cmpChart.render();
  }
}

async function loadComparisons() {
  try {
    const r = await api("/api/tradingview/comparisons?limit=15");
    $("tvc-history").innerHTML = r.comparisons.length ? `<table><thead><tr><th>시각</th><th>종목</th><th>전략</th><th>기간</th><th>판정</th><th style="text-align:right">수익률 TV / LEAN</th><th style="text-align:right">MDD TV / LEAN</th></tr></thead><tbody>${
      r.comparisons.map(c => `<tr><td class="text-xs">${ts(c.created_at)}</td><td class="font-mono text-xs">${escHtml(c.ticker)}</td><td class="text-xs">${escHtml(c.strategy)}</td><td class="text-xs">${c.start_date}~${c.end_date}</td>
        <td><span class="${VERDICT_CLS[c.verdict] || "badge-hold"}">${escHtml(c.verdict)}</span></td>
        <td style="text-align:right">${c.tv_metrics.net_profit_pct ?? "-"} / ${c.lean_metrics.net_profit_pct ?? "-"}</td><td style="text-align:right">${c.tv_metrics.max_drawdown_pct ?? "-"} / ${c.lean_metrics.max_drawdown_pct ?? "-"}</td></tr>`).join("")}</tbody></table>`
      : `<div class="text-sm" style="color:var(--text-mute);">아직 비교 이력이 없습니다.</div>`;
  } catch (e) { $("tvc-history").innerHTML = `<span class="text-red-500">${escHtml(e.message)}</span>`; }
}

export function initTradingViewView() {
  const on = (id, ev, fn) => $(id)?.addEventListener(ev, fn);
  on("tv-copy-url", "click", () => { navigator.clipboard.writeText($("tv-webhook-url").textContent); setToast("Webhook URL 복사됨", "ok"); });
  on("tv-copy-template", "click", () => { navigator.clipboard.writeText($("tv-alert-template").textContent); setToast("알림 메시지 템플릿 복사됨", "ok"); });
  on("tv-test-send", "click", sendTestSignal);
  on("tv-refresh", "click", loadSignals);
  on("tvc-run", "click", runCompare);
  const end = new Date(), start = new Date(end.getFullYear() - 1, end.getMonth(), end.getDate());
  if ($("tvc-end") && !$("tvc-end").value) $("tvc-end").value = end.toISOString().slice(0, 10);
  if ($("tvc-start") && !$("tvc-start").value) $("tvc-start").value = start.toISOString().slice(0, 10);
}

export function onTradingViewViewActivated(view) {
  if (view === "indicator-tradingview") { loadWebhookInfo(); loadSignals(); loadComparisons(); }
}
