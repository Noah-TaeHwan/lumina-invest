/* 공시 근거 모드(A-2 spec 3·8절): 모드 토글·회사 선택·외부 전송 고지, 문장 배지·요약줄, 근거 펼치기, 판정 폴링.
 * 서버가 준 문장 오프셋으로만 답변을 감싼다(문장을 다시 나누지 않는다).
 * 분기가 있는 부분은 아래 순수 함수로 두고 tests/e2e/evidence_views.py에서 확인한다. */
import { api, setToast, escHtml } from "/js/common.js";

// ── 상수·문구 ─────────────────────────────────────────────────────
// 근거 보고서는 2025.12 사업보고서뿐이다(spec 10절 범위). API에 기간 필드가 없어 화면 상수로 둔다
export const REPORT_LABEL = "2025.12 사업보고서";
const PROVISIONAL = new Set(["a2-provisional", "a2-provisional-2"]);  // 요약줄에 "(시험 기준)"(spec 6.4)
const ACTIVE = new Set(["pending", "running"]);
const POLL_INTERVAL_MS = 500;
const DEFAULT_POLL_UNTIL_S = 15;  // 서버가 상한을 주지 않을 때(spec 결정 4-1)

export const BADGE_TOOLTIP = "AI 판정(JEV 모델) · 검색된 공시 문단 기준이며 사실 여부를 보증하지 않습니다";

const BADGES = {
  pending:      { glyph: "⋯",  cls: "ev-b-pending", label: "AI 판정 중" },
  supported:    { glyph: "✅", cls: "ev-b-ok",      label: "검색된 공시 문단에서 확인됨" },
  contradicted: { glyph: "⚠️", cls: "ev-b-warn",    label: "검색된 공시 문단과 어긋남" },
  no_evidence:  { glyph: "❔", cls: "ev-b-none",    label: "검색된 공시 문단에서 확인되지 않음" },
  unjudged:     { glyph: "⊘",  cls: "ev-b-unj",     label: "판정 불가" },
};
const UNJUDGED_REASONS = {
  deadline: "판정 시간이 초과되었습니다.",
  claim_cap: "긴 답변의 뒷부분은 판정하지 않았습니다.",
  needs_call: "새 기준으로 다시 판정해야 합니다.",
  http_429: "판정 요청이 많아 잠시 후 다시 판정해야 합니다.",
};

// ── 순수 함수 ─────────────────────────────────────────────────────

/** 답변을 서버 오프셋으로 조각낸다. [{text, claim|null}], 이으면 answer와 같다.
 *  서버 오프셋은 파이썬 str 인덱스(코드포인트)라 UTF-16 slice가 아니라 코드포인트 배열로 자른다(이모지 등 아스트랄 문자).
 *  오프셋 순으로 보고, 앞 조각과 겹치거나 범위를 벗어난 문장은 감싸지 않는다(배지 없음). */
export function segmentsFromClaims(answer, claims) {
  const cps = Array.from(String(answer ?? ""));
  const text = { length: cps.length, slice: (a, b) => cps.slice(a, b).join("") };
  const sorted = [...(claims || [])].sort((a, b) => a.start - b.start);
  const out = [];
  let cur = 0;
  for (const c of sorted) {
    const { start, end } = c;
    if (!Number.isInteger(start) || !Number.isInteger(end) || start < cur || end <= start || end > text.length) continue;
    if (start > cur) out.push({ text: text.slice(cur, start), claim: null });
    out.push({ text: text.slice(start, end), claim: c });
    cur = end;
  }
  if (cur < text.length) out.push({ text: text.slice(cur), claim: null });
  return out;
}

/** 실행 상태 × 문장 상태 → 배지 종류(BADGES 키) 또는 null(배지 없음). spec 3.2 표. */
export function badgeKind(runStatus, claimStatus) {
  if (claimStatus === "not_claim" || runStatus === "skipped") return null;
  if (runStatus === "failed" || runStatus === "limited") return "unjudged";
  if (ACTIVE.has(runStatus)) return BADGES[claimStatus] ? claimStatus : "pending";
  // 끝난 실행(done·partial): 남은 pending이나 모르는 상태는 "보지 못했다"(⊘)로. ❔로 보이지 않는다
  return claimStatus !== "pending" && BADGES[claimStatus] ? claimStatus : "unjudged";
}

/** 폴링 계속 여부: "continue" | "timeout" | "done". 상한은 서버 안내 값(초), 없으면 15초. */
export function pollDecision(runStatus, elapsedMs, pollUntilS) {
  if (!ACTIVE.has(runStatus)) return "done";
  const limitS = Number(pollUntilS) > 0 ? Number(pollUntilS) : DEFAULT_POLL_UNTIL_S;
  return elapsedMs >= limitS * 1000 ? "timeout" : "continue";
}

/** 폴링 경과 시간: 서버 실행 생성 시각(created_at) 기준(poll_until_s가 그 기준이다). 없으면 폴링 시작 시각 기준. */
export function pollElapsedMs(run, startedMs, nowMs) {
  const created = Date.parse(run?.created_at ?? "");
  return nowMs - (Number.isFinite(created) ? created : startedMs);
}

export function pollInterval(ms) {
  return Number(ms) > 0 ? Number(ms) : POLL_INTERVAL_MS;
}

/** ✅ 문장의 확신도 "높음"/"보통". 서버(records.confidence_label)가 정책 τ_s로 정해 준 값만 쓴다(spec 3.3).
 *  확률 숫자는 보이지 않는다. */
export function confidenceText(claim) {
  const v = claim?.status === "supported" ? claim.confidence : null;
  return v === "높음" || v === "보통" ? v : null;
}

const NUM_RE = /\d[\d,]*(?:\.\d+)?/g;
const trimNum = t => t.replace(/[.,]+$/, "");

/** 문단 본문을 이스케이프하고, 숫자 확인을 통과한 문단(numberOk)이면 주장에 같은 표기로 나온 숫자만 굵게 한다.
 *  서버는 문단별 통과 여부만 주므로 단위 환산 일치(예: 1조 67억 ↔ 1,006,771백만)는 굵게 하지 않는다. */
export function highlightNumbers(passageText, claimText, numberOk) {
  const raw = String(passageText ?? "");
  if (!numberOk) return escHtml(raw);
  const want = new Set((String(claimText ?? "").match(NUM_RE) || []).map(trimNum));
  if (!want.size) return escHtml(raw);
  let out = "", cur = 0;
  for (const m of raw.matchAll(NUM_RE)) {
    const tok = trimNum(m[0]);
    out += escHtml(raw.slice(cur, m.index));
    out += want.has(tok) ? `<strong>${escHtml(tok)}</strong>${escHtml(m[0].slice(tok.length))}` : escHtml(m[0]);
    cur = m.index + m[0].length;
  }
  return out + escHtml(raw.slice(cur));
}

/** 요약줄. 항상 "AI 판정"으로 시작한다(spec 3.4). */
export function summaryText(run) {
  switch (run.status) {
    case "pending": case "running": return "AI 판정 중…";
    case "failed": return "AI 판정: 근거 판정을 하지 못했습니다(일시적 오류). 답변은 판정 없이 표시됩니다.";
    case "limited": return "AI 판정: 오늘 판정 한도에 도달해 판정을 생략했습니다.";
    case "skipped": return "AI 판정: 개인정보로 보이는 내용이 있어 외부 판정을 생략했습니다.";
  }
  const c = run.counts || {};
  const n = (run.passages || []).length;
  let t = `AI 판정: ✅ ${c.supported || 0} · ⚠️ ${c.contradicted || 0} · ❔ ${c.no_evidence || 0} — 검색된 ${REPORT_LABEL} 문단 ${n}개 기준`;
  if (PROVISIONAL.has(run.policy_version)) t += " (시험 기준)";
  if (run.status === "partial") t += ". 일부 문장은 판정하지 못했습니다";
  if ((run.claims || []).some(cl => cl.reason === "claim_cap")) t += ". 긴 답변의 뒷부분은 판정하지 않았습니다";
  return t;
}

/** 다시 판정 버튼: failed·partial이고 서버가 retryable(최신 실행·키 오류 아님)이라고 할 때만. */
export function showRetry(run) {
  return (run.status === "failed" || run.status === "partial") && run.retryable === true;
}

export function passageHeader(run, p) {
  return `근거 문단 · ${REPORT_LABEL}(접수번호 ${run.rcept_no || "-"}) · ${p.section || "-"} · 문단 ${p.idx ?? "-"}`;
}

function badgeTitle(kind, claim, run) {
  const parts = [BADGES[kind].label, BADGE_TOOLTIP];
  const conf = confidenceText(claim);
  if (conf) parts.push(`AI 판정 확신도: ${conf}`);
  return parts.join(" — ");
}

/** POST /api/evidence/chat·retry 응답을 실행 모양으로 바꾼다(문단·개수는 첫 조회에서 채워진다). */
export function runFromStarted(res) {
  return { id: res.run_id, chat_id: res.chat_id, conversation_id: res.conversation_id, status: "pending",
           claims: res.claims || [], passages: [], counts: {}, retryable: false,
           poll_interval_ms: res.poll_interval_ms, poll_until_s: res.poll_until_s };
}

// ── 화면: 답변 말풍선 ─────────────────────────────────────────────
let seq = 0;
const messages = new Set();

function passageBlock(run, p, claimText, numberOk) {
  return `<div class="ev-passage-head">${escHtml(passageHeader(run, p))}</div>
    <div class="ev-passage-body">${highlightNumbers(p.text, claimText, numberOk)}</div>`;
}

export function panelHtml(run, claim, kind) {
  const passages = run.passages || [];
  const src = Number.isInteger(claim.source_idx) ? passages[claim.source_idx] : null;
  const foot = `<div class="ev-panel-foot">${escHtml(BADGE_TOOLTIP)}</div>`;
  if ((kind === "supported" || kind === "contradicted") && src) {
    const ok = Array.isArray(claim.number_ok) && claim.number_ok[claim.source_idx] === true;
    const conf = confidenceText(claim);
    return passageBlock(run, src, claim.text, ok)
      + (conf ? `<div class="ev-panel-meta">AI 판정 확신도 ${escHtml(conf)}</div>` : "") + foot;
  }
  if (kind === "supported" || kind === "contradicted") {  // 판정은 있는데 근거 문단을 찾지 못함(스냅샷 누락 등)
    return `<div>근거 문단 정보 없음: 판정 기록에서 이 문장의 근거 문단을 찾지 못했습니다.</div>${foot}`;
  }
  if (kind === "no_evidence") {
    const items = passages.map(p => `<li>${passageBlock(run, p, "", false)}</li>`).join("");
    return `<div>검색된 문단 ${passages.length}개에서 이 문장을 확인하지 못했습니다.</div>
      <details class="ev-list"><summary>검색된 문단 ${passages.length}개 보기</summary><ol>${items}</ol></details>${foot}`;
  }
  const why = UNJUDGED_REASONS[claim.reason] || "판정하지 못했습니다(일시적 오류).";
  return `<div>판정 불가: ${escHtml(why)} 이 문장은 검색 문단과 비교하지 못했습니다.</div>${foot}`;
}

function renderAnswer(msg) {
  const { run } = msg;
  const html = segmentsFromClaims(msg.answer, run.claims).map(seg => {
    if (!seg.claim) return escHtml(seg.text);
    const kind = badgeKind(run.status, seg.claim.status);
    const muted = seg.claim.status === "not_claim" ? " ev-muted" : "";
    const span = `<span class="ev-claim${muted}" data-idx="${seg.claim.idx}">${escHtml(seg.text)}</span>`;
    if (!kind) return span;
    const b = BADGES[kind];
    const title = escHtml(badgeTitle(kind, seg.claim, run));
    if (kind === "pending") {
      return `${span}<span class="ev-badge ${b.cls}" role="img" aria-label="${title}" title="${title}">${b.glyph}</span>`;
    }
    const pid = `${msg.id}-p${seg.claim.idx}`;
    const open = msg.open.has(seg.claim.idx);
    return `${span}<button type="button" class="ev-badge ${b.cls}" data-idx="${seg.claim.idx}" data-kind="${kind}"
      aria-expanded="${open}" aria-controls="${pid}" aria-label="${title}" title="${title}">${b.glyph}</button>`;
  }).join("");
  msg.answerEl.innerHTML = html;
  // 펼친 칸은 판정 결과가 바뀌면 다시 그린다(같은 문장만 다시 연다)
  msg.panelsEl.innerHTML = "";
  for (const idx of msg.open) {
    const btn = msg.answerEl.querySelector(`button.ev-badge[data-idx="${idx}"]`);
    if (btn) mountPanel(msg, btn); else msg.open.delete(idx);
  }
}

function mountPanel(msg, btn) {
  const idx = Number(btn.dataset.idx);
  const claim = msg.run.claims.find(c => c.idx === idx);
  const div = document.createElement("div");
  div.id = btn.getAttribute("aria-controls");
  div.className = "ev-panel";
  div.setAttribute("role", "region");
  div.setAttribute("aria-label", `문장 ${idx + 1} 근거`);
  div.innerHTML = panelHtml(msg.run, claim, btn.dataset.kind);
  // 문장 순서대로 둔다
  const after = [...msg.panelsEl.children].find(el => Number(el.dataset.idx) > idx);
  div.dataset.idx = idx;
  msg.panelsEl.insertBefore(div, after || null);
}

function togglePanel(msg, btn) {
  const idx = Number(btn.dataset.idx);
  const opened = btn.getAttribute("aria-expanded") === "true";
  if (opened) {
    msg.open.delete(idx);
    document.getElementById(btn.getAttribute("aria-controls"))?.remove();
  } else {
    msg.open.add(idx);
    mountPanel(msg, btn);
  }
  btn.setAttribute("aria-expanded", String(!opened));
}

function renderSummary(msg, delayed = false) {
  const { run } = msg;
  let html = `<span class="ev-summary-text">${escHtml(summaryText(run))}</span>`;
  if (delayed) {
    html = `<span class="ev-summary-text">AI 판정: 판정이 지연되고 있습니다.</span>
      <button type="button" class="ev-refresh btn-secondary text-xs">새로고침</button>`;
  } else if (showRetry(run)) {
    html += ` <button type="button" class="ev-retry btn-secondary text-xs">다시 판정</button>`;
  }
  msg.summaryEl.innerHTML = html;
}

function setRun(msg, run) {
  msg.run = run;
  renderAnswer(msg);
  renderSummary(msg);
}

/** 근거 모드 답변 말풍선을 그리고(진행 중이면 폴링 시작) 메시지 객체를 돌려준다. */
export function appendEvidenceMsg(container, answer, run) {
  const d = document.createElement("div");
  d.className = "flex justify-start";
  const id = `ev${Date.now()}-${++seq}`;
  d.innerHTML = `<div class="ev-msg max-w-[88%] px-4 py-3 text-sm leading-relaxed" style="background:var(--surf);border:1px solid var(--border);border-radius:4px 18px 18px 18px;box-shadow:0 1px 4px rgba(0,0,0,0.06);color:var(--text);min-width:0;">
    <div class="ai-label">AI 생성 답변</div>
    <div class="ev-answer"></div>
    <div class="ev-panels"></div>
    <div class="ev-summary" aria-live="polite"></div>
  </div>`;
  container.appendChild(d);
  const root = d.firstElementChild;
  const msg = { id, answer, run, open: new Set(), timer: null, dead: false, root,
                answerEl: root.querySelector(".ev-answer"), panelsEl: root.querySelector(".ev-panels"),
                summaryEl: root.querySelector(".ev-summary") };
  messages.add(msg);
  msg.answerEl.addEventListener("click", e => {
    const btn = e.target.closest("button.ev-badge");
    if (btn) togglePanel(msg, btn);
  });
  msg.summaryEl.addEventListener("click", e => {
    if (e.target.closest(".ev-retry")) retry(msg);
    if (e.target.closest(".ev-refresh")) refresh(msg);
  });
  setRun(msg, run);
  if (ACTIVE.has(run.status)) startPolling(msg);
  return msg;
}

function startPolling(msg) {
  clearTimeout(msg.timer);
  const runId = msg.run.id;
  const t0 = Date.now();
  const tick = async () => {
    if (msg.dead || msg.run.id !== runId) return;
    try {
      setRun(msg, await api(`/api/evidence/runs/${encodeURIComponent(runId)}`));
    } catch { /* 일시적 조회 실패: 상한 안에서 다시 본다 */ }
    if (msg.dead || msg.run.id !== runId) return;
    const next = pollDecision(msg.run.status, pollElapsedMs(msg.run, t0, Date.now()), msg.run.poll_until_s);
    if (next === "continue") msg.timer = setTimeout(tick, pollInterval(msg.run.poll_interval_ms));
    else if (next === "timeout") renderSummary(msg, true);
  };
  msg.timer = setTimeout(tick, pollInterval(msg.run.poll_interval_ms));
}

async function refresh(msg) {
  try {
    setRun(msg, await api(`/api/evidence/runs/${encodeURIComponent(msg.run.id)}`));
  } catch (e) {
    setToast(e.message, "error");
  }
  if (ACTIVE.has(msg.run.status)) renderSummary(msg, true);
}

async function retry(msg) {
  const btn = msg.summaryEl.querySelector(".ev-retry");
  if (btn) btn.disabled = true;
  try {
    const res = await api(`/api/evidence/runs/${encodeURIComponent(msg.run.id)}/retry`, { method: "POST" });
    msg.open.clear();
    setRun(msg, { ...runFromStarted(res), passages: msg.run.passages, rcept_no: msg.run.rcept_no,
                  policy_version: msg.run.policy_version });
    startPolling(msg);
  } catch (e) {
    if (btn) btn.disabled = false;
    setToast(e.message, "error");
  }
}

/** 화면 초기화 시 진행 중 폴링을 멈춘다. */
export function stopAllEvidence() {
  for (const m of messages) { m.dead = true; clearTimeout(m.timer); }
  messages.clear();
  state.conversationId = null;  // 초기화한 뒤 질문은 스레드 id 없이 보낸다(서버가 활성 스레드를 정한다)
}

// ── 화면: 모드 토글·회사 선택·고지 ─────────────────────────────────
const state = { available: false, acked: null, company: null, conversationId: null, sending: false };

export function isEvidenceMode() {
  return state.available && document.getElementById("ev-mode")?.checked === true;
}

export function setConversationId(cid) {
  if (cid) state.conversationId = cid;
}

/** 기능 플래그 확인: 가벼운 GET /api/evidence/notice로 본다. 404(꺼짐)·401 등 200이 아니면 토글을 숨긴다.
 *  응답의 고지 확인 여부도 함께 기억한다. 저장소 준비 여부(503)는 회사를 찾을 때 알린다. */
async function probe() {
  let res;
  try { res = await fetch("/api/evidence/notice", { credentials: "include" }); } catch { return; }
  if (res.status !== 200) return;
  try { state.acked = (await res.json()).acknowledged === true; } catch { state.acked = null; }
  state.available = true;
  document.getElementById("ev-bar").classList.remove("hidden");
}

async function noticeAcked() {
  if (state.acked !== null) return state.acked;
  try { state.acked = (await api("/api/evidence/notice")).acknowledged === true; } catch { state.acked = false; }
  return state.acked;
}

function setModeUi(on) {
  document.getElementById("ev-mode").checked = on;
  document.getElementById("ev-company-wrap").classList.toggle("hidden", !on);
  const inp = document.getElementById("chat-input");
  inp.placeholder = on ? "예) 주요 제품과 매출 비중은? (질문마다 공시 문단을 새로 검색)"
                       : inp.dataset.agentPlaceholder;
}

function openNotice() {
  const dlg = document.getElementById("ev-notice");
  if (typeof dlg.showModal === "function") dlg.showModal(); else dlg.setAttribute("open", "");
  document.getElementById("ev-notice-ok").focus();
}

function closeNotice() {
  const dlg = document.getElementById("ev-notice");
  if (typeof dlg.close === "function" && dlg.open) dlg.close(); else dlg.removeAttribute("open");
}

async function onToggle(e) {
  const on = e.target.checked;
  if (!on) return setModeUi(false);
  e.target.checked = false;  // 고지 확인 전에는 켜지 않는다
  if (await noticeAcked()) return setModeUi(true);
  openNotice();
}

async function onNoticeOk() {
  closeNotice();
  try {
    await api("/api/evidence/notice", { method: "POST" });
    state.acked = true;
  } catch (e) {
    // 기록 실패(Redis 장애 등): 이번 화면에서만 켜고, 다음에 다시 묻는다
    setToast(`고지 확인을 저장하지 못했습니다. 다음에 다시 확인합니다. (${e.message})`, "error");
  }
  setModeUi(true);
  document.getElementById("ev-company").focus();
}

// 회사 자동완성(combobox)
let suggestTimer = null;
let options = [];
let activeOpt = -1;

function renderOptions() {
  const list = document.getElementById("ev-company-list");
  const inp = document.getElementById("ev-company");
  list.innerHTML = options.length
    ? options.map((c, i) => `<li id="ev-opt-${i}" role="option" class="ev-opt${i === activeOpt ? " active" : ""}"
        aria-selected="${i === activeOpt}" data-i="${i}">${escHtml(c.corp_name)} <span class="ev-opt-code">${escHtml(c.stock_code)}</span></li>`).join("")
    : `<li class="ev-opt-empty" role="option" aria-disabled="true">근거 문단이 적재된 회사 중 결과가 없습니다</li>`;
  list.classList.remove("hidden");
  inp.setAttribute("aria-expanded", "true");
  if (activeOpt >= 0) inp.setAttribute("aria-activedescendant", `ev-opt-${activeOpt}`);
  else inp.removeAttribute("aria-activedescendant");
}

function closeOptions() {
  document.getElementById("ev-company-list").classList.add("hidden");
  const inp = document.getElementById("ev-company");
  inp.setAttribute("aria-expanded", "false");
  inp.removeAttribute("aria-activedescendant");
  activeOpt = -1;
}

async function suggest() {
  const q = document.getElementById("ev-company").value.trim();
  try {
    const { companies } = await api(`/api/evidence/companies?q=${encodeURIComponent(q)}`);
    options = companies || [];
  } catch (e) {
    options = [];
    setToast(e.message, "error");
  }
  activeOpt = -1;
  renderOptions();
}

function choose(i) {
  const c = options[i];
  if (!c) return;
  state.company = c;
  document.getElementById("ev-company").value = c.corp_name;
  closeOptions();
}

function onCompanyInput() {
  state.company = null;
  clearTimeout(suggestTimer);
  suggestTimer = setTimeout(suggest, 250);
}

function onCompanyKey(e) {
  const listOpen = !document.getElementById("ev-company-list").classList.contains("hidden");
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    if (!listOpen) { suggest(); return; }
    if (!options.length) return;
    activeOpt = e.key === "ArrowDown" ? (activeOpt + 1) % options.length : (activeOpt <= 0 ? options.length - 1 : activeOpt - 1);
    renderOptions();
  } else if (e.key === "Enter" && listOpen) {
    e.preventDefault();
    choose(activeOpt >= 0 ? activeOpt : 0);
  } else if (e.key === "Escape") {
    closeOptions();
  }
}

function toggleHelp() {
  const btn = document.getElementById("ev-help-btn");
  const open = btn.getAttribute("aria-expanded") === "true";
  btn.setAttribute("aria-expanded", String(!open));
  document.getElementById("ev-help").classList.toggle("hidden", open);
}

let inited = false;
/** 채팅 화면이 처음 열릴 때 한 번: 플래그 확인과 이벤트 연결. */
export async function initEvidence() {
  if (inited) return;
  inited = true;
  const inp = document.getElementById("chat-input");
  inp.dataset.agentPlaceholder = inp.placeholder;
  document.getElementById("ev-mode").addEventListener("change", onToggle);
  document.getElementById("ev-notice-ok").addEventListener("click", onNoticeOk);
  document.getElementById("ev-notice-cancel").addEventListener("click", () => { closeNotice(); setModeUi(false); });
  document.getElementById("ev-notice").addEventListener("cancel", () => setModeUi(false));  // Esc
  document.getElementById("ev-help-btn").addEventListener("click", toggleHelp);
  const company = document.getElementById("ev-company");
  company.addEventListener("input", onCompanyInput);
  company.addEventListener("keydown", onCompanyKey);
  company.addEventListener("focus", () => { if (!state.company) suggest(); });
  company.addEventListener("blur", () => setTimeout(closeOptions, 150));
  const list = document.getElementById("ev-company-list");
  list.addEventListener("mousedown", e => e.preventDefault());  // blur보다 먼저 고르게
  list.addEventListener("click", e => {
    const li = e.target.closest("li[data-i]");
    if (li) choose(Number(li.dataset.i));
  });
  await probe();
}

/** 근거 모드 질문 전송. 성공하면 true(말풍선을 그렸다), 보내지 않았거나 실패하면 false. */
export async function sendEvidenceChat(question, { appendUserMsg, container, scroll }) {
  if (state.sending) return false;  // 요청 중 Enter 연타: 생성·판정을 두 번 하지 않는다(한도 이중 차감 방지)
  if (!state.company) {
    setToast("공시 근거 모드에서는 회사를 먼저 선택하세요.", "error");
    document.getElementById("ev-company").focus();
    return false;
  }
  state.sending = true;
  const sendBtn = document.getElementById("chat-send");
  sendBtn.disabled = true;
  appendUserMsg(question);
  const thinking = document.createElement("div");
  thinking.className = "flex justify-start ev-thinking";
  thinking.innerHTML = `<div class="px-4 py-3 text-sm animate-pulse" style="background:var(--surf);border:1px solid var(--border);border-radius:4px 18px 18px 18px;color:var(--text-mute);display:inline-block;"><i class="fa-solid fa-circle-notch fa-spin" style="margin-right:6px;color:var(--accent);"></i>공시 문단 검색·답변 생성 중…</div>`;
  container.appendChild(thinking);
  scroll();
  const body = { question, company: state.company.corp_name, corp_code: state.company.corp_code };
  if (state.conversationId) body.conversation_id = state.conversationId;
  try {
    const res = await api("/api/evidence/chat", { method: "POST", body });
    thinking.remove();
    setConversationId(res.conversation_id);
    appendEvidenceMsg(container, res.answer, runFromStarted(res));
    scroll();
    return true;
  } catch (e) {
    thinking.remove();
    setToast(e.message, "error");
    return false;
  } finally {
    state.sending = false;
    sendBtn.disabled = false;
  }
}
