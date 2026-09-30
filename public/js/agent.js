/* 금융정보 Agent: AI 채팅, CB 분석, 금융상품, 뉴스/RAG, 크롤링
 * app.html 인라인 스크립트에서 분리됨. 엔트리는 main.js */
import { api, getMe, setToast, escHtml, fmt, fmtPct, colorPct } from "/js/common.js";

let chatHistory = [];
// ── 1. AI 채팅 ────────────────────────────────────────────────────
function appendUserMsg(text) {
  const d = document.createElement("div");
  d.className = "flex justify-end";
  d.innerHTML = `<div class="max-w-[80%] px-4 py-3 text-sm leading-relaxed" style="background:var(--accent);color:#fff;border-radius:18px 4px 18px 18px;box-shadow:0 2px 8px rgba(41,98,255,0.25);">${escHtml(text)}</div>`;
  document.getElementById("chat-messages").appendChild(d);
  scrollChat();
}

function appendAssistantMsg(answer, steps) {
  const msgId = "m" + Date.now();
  let stepsHtml = "";
  if (steps?.length) {
    const items = steps.map((s, i) => {
      const obs = s.observation ? `<div class="mt-1 text-slate-500 bg-black/20 rounded p-2 max-h-24 overflow-y-auto">${escHtml(s.observation.slice(0, 300))}</div>` : "";
      return `<div class="step-${escHtml(s.action)} pl-3 py-1 mb-1">
        <div class="text-xs font-medium text-slate-300">${i+1}. ${escHtml(s.action)}</div>
        <div class="text-xs text-slate-500 italic">${escHtml(s.thought)}</div>${obs}
      </div>`;
    }).join("");
    stepsHtml = `<div class="mt-2 border-t border-white/10 pt-2">
      <button class="steps-btn text-xs text-slate-500 hover:text-slate-300" data-target="${msgId}-steps">▶ 추론 (${steps.length}단계)</button>
      <div id="${msgId}-steps" class="hidden mt-1">${items}</div>
    </div>`;
  }
  const d = document.createElement("div");
  d.className = "flex justify-start";
  d.innerHTML = `<div class="max-w-[88%] px-4 py-3 text-sm leading-relaxed" style="background:var(--surf);border:1px solid var(--border);border-radius:4px 18px 18px 18px;box-shadow:0 1px 4px rgba(0,0,0,0.06);color:var(--text);">
    <pre style="white-space:pre-wrap;word-break:break-word;font-family:inherit;font-size:13px;line-height:1.7;">${escHtml(answer)}</pre>${stepsHtml}
  </div>`;
  d.querySelectorAll(".steps-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      const p = document.getElementById(btn.dataset.target);
      p?.classList.toggle("hidden");
      btn.textContent = p?.classList.contains("hidden") ? "▶ 추론" : "▼ 추론";
    });
  });
  document.getElementById("chat-messages").appendChild(d);
  scrollChat();
}

function scrollChat() {
  const c = document.getElementById("chat-messages");
  c.scrollTop = c.scrollHeight;
}

async function sendChat() {
  const inp = document.getElementById("chat-input");
  const q = inp.value.trim();
  if (!q) return;
  inp.value = "";
  appendUserMsg(q);

  // thinking indicator
  const thinking = document.createElement("div");
  thinking.id = "thinking";
  thinking.className = "flex justify-start";
  thinking.innerHTML = `<div class="px-4 py-3 text-sm animate-pulse" style="background:var(--surf);border:1px solid var(--border);border-radius:4px 18px 18px 18px;color:var(--text-mute);display:inline-block;"><i class="fa-solid fa-circle-notch fa-spin" style="margin-right:6px;color:var(--accent);"></i>에이전트 분석 중...</div>`;
  document.getElementById("chat-messages").appendChild(thinking);
  scrollChat();

  try {
    const res = await api("/api/chat", { method: "POST", body: { question: q, history: chatHistory } });
    document.getElementById("thinking")?.remove();
    chatHistory.push({ role: "user", content: q });
    chatHistory.push({ role: "assistant", content: res.answer });
    if (chatHistory.length > 20) chatHistory = chatHistory.slice(-20);
    appendAssistantMsg(res.answer, res.steps);
  } catch (e) {
    document.getElementById("thinking")?.remove();
    setToast(e.message, "error");
  }
}

document.getElementById("chat-send").addEventListener("click", sendChat);
document.getElementById("chat-input").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChat(); }
});
document.getElementById("clear-chat").addEventListener("click", () => {
  chatHistory = [];
  document.getElementById("chat-messages").innerHTML = "";
  setToast("대화 초기화됨", "ok");
});

// ── CB 분석 ───────────────────────────────────────────────────────
async function runCbQuery(type) {
  const resultEl = document.getElementById(`${type}-result`);
  resultEl.textContent = "조회 중...";
  try {
    let q;
    if (type === "pcb") {
      const period = document.getElementById("pcb-period").value;
      const gender = document.getElementById("pcb-gender").value;
      const age = document.getElementById("pcb-age").value;
      q = `개인 CB 신용 통계를 조회해줘.${period ? " 기준월:" + period : ""}${gender ? " 성별:" + gender : ""}${age ? " 연령대:" + age : ""}`;
    } else {
      const period = document.getElementById("ccb-period").value;
      const size = document.getElementById("ccb-size").value;
      const ind = document.getElementById("ccb-industry").value;
      q = `기업 CB 신용 통계를 조회해줘.${period ? " 기간:" + period : ""}${size ? " 규모:" + size : ""}${ind ? " 업종:" + ind : ""}`;
    }
    const res = await api("/api/chat", { method: "POST", body: { question: q, history: [] } });
    resultEl.textContent = res.answer;
  } catch (e) {
    resultEl.textContent = "오류: " + e.message;
  }
}
document.getElementById("pcb-search").addEventListener("click", () => runCbQuery("pcb"));
document.getElementById("ccb-search").addEventListener("click", () => runCbQuery("ccb"));

// ── 금융상품 ──────────────────────────────────────────────────────
let productTab = "bank";
document.querySelectorAll(".product-tab").forEach(btn => {
  btn.addEventListener("click", () => {
    productTab = btn.dataset.tab;
    document.getElementById("bank-search-form").classList.toggle("hidden", productTab !== "bank");
    document.getElementById("fund-search-form").classList.toggle("hidden", productTab !== "fund");
    document.querySelectorAll(".product-tab").forEach(b => {
      b.className = b === btn ? "product-tab btn-primary" : "product-tab btn-secondary";
    });
  });
});

async function searchProducts() {
  const el = document.getElementById("product-results");
  el.innerHTML = "<div class='text-slate-400'>검색 중...</div>";
  try {
    let q;
    if (productTab === "bank") {
      const rate = document.getElementById("bank-rate").value;
      const kw = document.getElementById("bank-keyword").value;
      q = `은행 수신상품을 검색해줘.${rate ? " 최소금리:" + rate + "%" : ""}${kw ? " 키워드:" + kw : ""}`;
    } else {
      const type = document.getElementById("fund-type").value;
      const ret = document.getElementById("fund-return").value;
      const kw = document.getElementById("fund-keyword").value;
      q = `공모펀드를 검색해줘.${type ? " 유형:" + type : ""}${ret ? " 최소1년수익률:" + ret + "%" : ""}${kw ? " 키워드:" + kw : ""}`;
    }
    const res = await api("/api/chat", { method: "POST", body: { question: q, history: [] } });
    el.innerHTML = `<pre class="text-xs text-slate-300 bg-black/30 rounded-xl p-3">${escHtml(res.answer)}</pre>`;
  } catch (e) {
    el.innerHTML = `<div class='text-red-400'>${escHtml(e.message)}</div>`;
  }
}
document.getElementById("bank-search").addEventListener("click", searchProducts);
document.getElementById("fund-search").addEventListener("click", searchProducts);

// ── 뉴스/RAG ─────────────────────────────────────────────────────
document.getElementById("news-search").addEventListener("click", async () => {
  const q = document.getElementById("news-q").value.trim();
  const el = document.getElementById("news-results");
  if (!q) return;
  el.innerHTML = "<div class='text-slate-400'>검색 중...</div>";
  try {
    const res = await api(`/api/library/search?q=${encodeURIComponent(q)}&category=news`);
    if (!res.items?.length) { el.innerHTML = "<div class='text-slate-400'>결과 없음</div>"; return; }
    el.innerHTML = res.items.map(item => `
      <div class="card"><div class="text-xs text-indigo-300 mb-1">${escHtml(item.type)}</div>
      <pre class="text-xs text-slate-300">${escHtml(item.content)}</pre></div>
    `).join("");
  } catch (e) { el.innerHTML = `<div class='text-red-400'>${escHtml(e.message)}</div>`; }
});

// ── 크롤링 ───────────────────────────────────────────────────────
document.getElementById("auto-crawl-btn").addEventListener("click", async () => {
  const log = document.getElementById("crawl-log");
  log.textContent = "크롤링 시작...";
  try {
    const res = await api("/api/ingest/crawl/auto", { method: "POST" });
    log.textContent = res.log.join("\n");
    setToast("크롤링 완료", "ok");
  } catch (e) { log.textContent += "\n[ERROR] " + e.message; setToast(e.message, "error"); }
});

document.getElementById("manual-crawl-btn").addEventListener("click", async () => {
  const url = document.getElementById("crawl-url").value.trim();
  const log = document.getElementById("manual-crawl-log");
  if (!url) return setToast("URL을 입력하세요.", "error");
  log.textContent = "크롤링 중...";
  try {
    const res = await api("/api/ingest/crawl/url", { method: "POST", body: { url } });
    log.textContent = res.log.join("\n");
    setToast(`${res.chunks}청크 저장 완료`, "ok");
    loadCrawlList();
  } catch (e) { log.textContent += "\n[ERROR] " + e.message; setToast(e.message, "error"); }
});

document.getElementById("naver-crawl-btn").addEventListener("click", async () => {
  const code = document.getElementById("crawl-naver-code").value.trim();
  const log = document.getElementById("manual-crawl-log");
  if (!code) return setToast("네이버 종목코드를 입력하세요. (예: 005930)", "error");
  log.textContent = "네이버 주식 크롤링 중...";
  try {
    const res = await api("/api/ingest/crawl/naver", { method: "POST", body: { code } });
    log.textContent = res.message || "완료";
    setToast(`${res.chunks}청크 저장 완료`, "ok");
    loadCrawlList();
  } catch (e) {
    log.textContent += "\n[ERROR] " + e.message;
    setToast(e.message, "error");
  }
});

async function loadCrawlList() {
  try {
    const { items } = await api("/api/ingest/crawl/list");
    const el = document.getElementById("crawl-list");
    el.innerHTML = items.map(it => `
      <div class="rounded-xl border border-white/10 bg-black/20 p-2">
        <div class="text-slate-300">${escHtml(it.title || it.url)}</div>
        <div class="text-xs text-slate-500">${escHtml(it.source)} · ${it.crawled_at?.slice(0,10)}</div>
      </div>
    `).join("") || "<div class='text-slate-400 text-xs'>크롤링된 문서가 없습니다.</div>";
  } catch {}
}

document.getElementById("ingest-btn").addEventListener("click", async () => {
  const log = document.getElementById("ingest-log");
  log.textContent = "인제스트 시작 (수 분 소요)...";
  try {
    const res = await api("/api/ingest/financial", { method: "POST" });
    log.textContent = res.log.join("\n");
    setToast("인제스트 완료", "ok");
  } catch (e) { log.textContent += "\n[ERROR] " + e.message; setToast(e.message, "error"); }
});

document.getElementById("admin-reset-btn").addEventListener("click", async () => {
  if (!confirm("DB를 초기화하시겠습니까?")) return;
  try {
    const res = await api("/api/admin/reset", { method: "POST" });
    document.getElementById("ingest-log").textContent = res.message;
    setToast("초기화 완료", "ok");
  } catch (e) { setToast(e.message, "error"); }
});


export { loadCrawlList };
