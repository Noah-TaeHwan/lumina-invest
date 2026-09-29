/* 직접매매: 주가 차트, 포트폴리오, 주문
 * app.html 인라인 스크립트에서 분리됨. 엔트리는 main.js */
import { api, getMe, setToast, escHtml, fmt, fmtPct, colorPct } from "/js/common.js";

let chartInstance = null;
// ── 주가 차트 ────────────────────────────────────────────────────
async function loadStockChart() {
  const symbol = document.getElementById("chart-symbol").value.trim() || "005930.KS";
  const period = document.getElementById("chart-period").value;
  const infoEl = document.getElementById("stock-info");
  const container = document.getElementById("chart-container");

  infoEl.textContent = "로딩 중...";
  try {
    const [quote, candles] = await Promise.all([
      api(`/api/stocks/quote?symbol=${encodeURIComponent(symbol)}`),
      api(`/api/stocks/candles?symbol=${encodeURIComponent(symbol)}&period=${period}`),
    ]);

    const price = quote.price ?? "-";
    const prevClose = quote.prev_close ?? 0;
    const chg = prevClose ? ((price - prevClose) / prevClose * 100).toFixed(2) : null;
    const chgClass = chg >= 0 ? "text-emerald-400" : "text-red-400";
    infoEl.innerHTML = `<span class="font-semibold">${escHtml(symbol)}</span>
      <span class="ml-3">${fmt(price)}</span>
      ${chg != null ? `<span class="ml-2 ${chgClass}">${chg >= 0 ? "+" : ""}${chg}%</span>` : ""}`;

    // Chart — ApexCharts 캔들스틱
    container.innerHTML = "";
    if (chartInstance) { chartInstance.destroy(); chartInstance = null; }
    const ohlcv = candles.candles
      .filter(c => c.open && c.high && c.low && c.close)
      .map(c => ({ x: new Date(c.time * 1000), y: [c.open, c.high, c.low, c.close] }));
    chartInstance = new ApexCharts(container, {
      chart: { type: "candlestick", height: 380, background: "#fff", toolbar: { show: true },
               animations: { enabled: false }, fontFamily: "Pretendard, sans-serif" },
      series: [{ name: "OHLC", data: ohlcv }],
      xaxis: { type: "datetime", labels: { style: { colors: "#434651" } } },
      yaxis: { tooltip: { enabled: true }, labels: { style: { colors: "#434651" },
               formatter: v => v?.toLocaleString() } },
      plotOptions: { candlestick: { colors: { upward: "#10b981", downward: "#ef4444" },
                     wick: { useFillColor: true } } },
      grid: { borderColor: "#e0e3eb" },
      theme: { mode: "light" },
      tooltip: { theme: "light" },
    });
    chartInstance.render();
  } catch (e) {
    infoEl.textContent = "오류: " + e.message;
  }
}

document.getElementById("chart-load").addEventListener("click", loadStockChart);

// ── 포트폴리오 ───────────────────────────────────────────────────
async function loadPortfolio() {
  try {
    const { holdings } = await api("/api/portfolio");
    const el = document.getElementById("portfolio-table");
    if (!holdings.length) { el.innerHTML = "<div class='text-slate-400 text-xs'>보유 종목이 없습니다.</div>"; return; }
    el.innerHTML = `<table class="w-full text-xs border-collapse">
      <thead><tr class="text-slate-400 border-b border-white/10">
        <th class="py-2 text-left">종목</th><th class="py-2 text-right">수량</th>
        <th class="py-2 text-right">평균단가</th><th class="py-2 text-right">평가금액(예상)</th>
        <th class="py-2 text-right">조작</th>
      </tr></thead>
      <tbody>
        ${holdings.map(h => `
          <tr class="border-b border-white/5">
            <td class="py-2">${escHtml(h.name)}<div class="text-slate-500">${escHtml(h.symbol)}</div></td>
            <td class="py-2 text-right">${fmt(h.quantity)}</td>
            <td class="py-2 text-right">${fmt(h.avg_price)}</td>
            <td class="py-2 text-right text-slate-400">-</td>
            <td class="py-2 text-right">
              <button class="text-red-400 hover:text-red-300 delete-holding" data-symbol="${escHtml(h.symbol)}">삭제</button>
            </td>
          </tr>
        `).join("")}
      </tbody>
    </table>`;
    el.querySelectorAll(".delete-holding").forEach(btn => {
      btn.addEventListener("click", async () => {
        await api(`/api/portfolio/${encodeURIComponent(btn.dataset.symbol)}`, { method: "DELETE" });
        loadPortfolio();
      });
    });
  } catch {}
}

document.getElementById("pf-add").addEventListener("click", async () => {
  const symbol = document.getElementById("pf-symbol").value.trim();
  const name = document.getElementById("pf-name").value.trim();
  const qty = parseInt(document.getElementById("pf-qty").value);
  const price = parseFloat(document.getElementById("pf-price").value);
  if (!symbol || !name || !qty || !price) return setToast("모든 항목을 입력하세요.", "error");
  try {
    await api("/api/portfolio", { method: "POST", body: { symbol, name, quantity: qty, avg_price: price } });
    setToast("추가 완료", "ok");
    loadPortfolio();
  } catch (e) { setToast(e.message, "error"); }
});

// ── 주문 ─────────────────────────────────────────────────────────
async function placeOrder(type) {
  const symbol = document.getElementById("ord-symbol").value.trim();
  const name = document.getElementById("ord-name").value.trim();
  const price = parseFloat(document.getElementById("ord-price").value);
  const qty = parseInt(document.getElementById("ord-qty").value);
  const broker = document.getElementById("ord-broker").value;
  if (!symbol || !name || !price || !qty) return setToast("모든 항목을 입력하세요.", "error");
  try {
    await api("/api/orders", { method: "POST", body: { symbol, name, order_type: type, quantity: qty, price, broker } });
    setToast(`${type === "buy" ? "매수" : "매도"} 주문 완료 (${broker})`, "ok");
    loadOrderHistory();
  } catch (e) { setToast(e.message, "error"); }
}
document.getElementById("ord-buy").addEventListener("click", () => placeOrder("buy"));
document.getElementById("ord-sell").addEventListener("click", () => placeOrder("sell"));

async function loadOrderHistory() {
  try {
    const { orders } = await api("/api/orders");
    const el = document.getElementById("order-history");
    el.innerHTML = orders.slice(0, 20).map(o => `
      <div class="flex items-center justify-between rounded-xl border border-white/10 bg-black/20 px-3 py-2 text-xs">
        <span class="${o.order_type === "buy" ? "badge-buy" : "badge-sell"}">${o.order_type === "buy" ? "매수" : "매도"}</span>
        <span class="ml-2">${escHtml(o.name)} (${escHtml(o.symbol)})</span>
        <span class="ml-auto">${fmt(o.price)}원 × ${fmt(o.quantity)}주</span>
        <span class="ml-3 text-slate-500">${escHtml(o.broker)} · ${(o.created_at||"").slice(0,10)}</span>
      </div>
    `).join("") || "<div class='text-slate-400'>주문 내역 없음</div>";
  } catch {}
}

async function loadBrokerStatus() {
  try {
    const s = await api("/api/broker/settings");
    const el = document.getElementById("trading-broker-status");
    if (!el) return;
    el.innerHTML = `
      <div>선택 브로커: <span class="text-slate-200">${escHtml(s.broker || "mock")}</span></div>
      <div>연결 상태: <span class="${s.connected ? "text-emerald-400" : "text-red-400"}">${s.connected ? "연동됨" : "미연동"}</span></div>
    `;
  } catch {}
}
document.getElementById("trading-broker-save")?.addEventListener("click", async () => {
  try {
    const selected = document.getElementById("ord-broker")?.value || "mock";
    const broker = selected === "virtual" ? "mock" : selected;
    const appKey = document.getElementById("kiwoom-key")?.value
      || document.getElementById("broker-app-key")?.value
      || "";
    const appSecret = document.getElementById("kiwoom-secret")?.value
      || document.getElementById("broker-app-secret")?.value
      || "";
    const accountNo = document.getElementById("broker-account")?.value || "";
    const paper = document.getElementById("broker-paper")?.checked ?? true;
    await api("/api/broker/settings", { method: "POST", body: {
      broker,
      app_key: appKey,
      app_secret: appSecret,
      account_no: accountNo,
      paper,
    }});
    setToast("설정 저장 완료", "ok");
    loadBrokerStatus();
  } catch (e) { setToast(e.message, "error"); }
});


export { loadBrokerStatus, loadOrderHistory, loadPortfolio, loadStockChart };
