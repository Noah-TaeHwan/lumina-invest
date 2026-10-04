/* 투자 판단 일지 화면(모듈 C spec 3·4절, 10절 P3): 요약줄 '판단 기록' 버튼, 기록 양식, 일지 탭(목록·거르기·상세 세 칸·
 * 변화 칸·다시 보기·삭제·내보내기), 고정 고지와 AI 출처 표시.
 * - 기능 확인은 GET /api/journal 한 번. 404(JOURNAL_ENABLED 꺼짐)·모양이 다른 응답이면 탭과 버튼을 그리지 않는다.
 * - 배지는 "당시 AI 판정"으로만 보이고 판단을 끌어내지 않는다(기본 선택 없음, 배지 요약 숫자·색 없음, 결정 4-1).
 * - 가격·수익률을 붙이지 않는다(결정 4-3). 이 파일은 /api/journal·/api/evidence/notice 밖을 부르지 않는다.
 * 분기가 있는 부분은 순수 함수로 두고 tests/e2e/journal_views.py에서 확인한다.
 * 의존은 한 방향이다: journal.js → evidence.js(배지·요약줄 확장 등록). evidence.js는 이 파일을 import하지 않는다. */
import { escHtml, setToast } from "/js/common.js";
import { navigate } from "/js/core.js";
import {
  badgeKind, badgeMark, badgeTitle, forgetJournalEntries, panelHtml, patchRun, prefillEvidenceChat,
  segmentsFromClaims, setSummaryExtension,
} from "/js/evidence.js";

// ── 상수·문구 ─────────────────────────────────────────────────────
// 고정 고지(spec 결정 4-2). app/routes/journal.py NOTICE와 같은 글자다
export const JOURNAL_NOTICE = "판단 일지는 내가 쓴 기록입니다. 답변과 배지는 AI가 만든 것으로, 배지는 검색된 공시 문단 기준 AI 판정"
  + "(TypeSafe의 JEV 모델)이며 사실 여부를 보증하지 않습니다. 이 서비스는 투자 권유나 수익 예측을 하지 않으며, "
  + "투자 판단과 그 결과는 본인에게 있습니다. 이 프로젝트는 TypeSafe와 제휴 관계가 아닙니다.";
const DECISIONS = ["consider_buy", "watch", "exclude"];
const DECISION_LABELS = { consider_buy: "매수 검토", watch: "관망", exclude: "제외" };
const MEMO_MAX = 2000;
const PAGE = 50;
const ACTIVE = new Set(["pending", "running"]);
const NO_JUDGE = { failed: "실패", limited: "한도 초과로 생략", skipped: "개인정보로 보여 생략" };
const REPORT_STATES = new Set(["same", "replaced", "company_gone"]);
const FIELD_NAMES = { decision: "내 판단", conviction: "내 확신", memo: "내 메모", relied_claims: "기댄 문장",
                      review_on: "다시 볼 날짜", run_id: "판정 기록" };

const MSG = {
  saveFail: "저장하지 못했습니다. 입력은 그대로 있습니다.",
  runGone: "원래 판정 기록을 찾을 수 없어 기록할 수 없습니다",
  notDone: "판정이 끝난 뒤 기록할 수 있습니다",
  entryGone: "기록을 찾을 수 없습니다(지워졌을 수 있습니다).",
  empty: "아직 기록한 판단이 없습니다. 근거 모드 답변에서 판단 기록을 눌러 시작합니다.",
  emptyFiltered: "조건에 맞는 기록이 없습니다.",
  checking: "공시 변화를 확인하는 중…",
  unavailable: "지금은 공시 변화를 확인할 수 없습니다",
  scope: "이 비교는 이 앱에 적재된 사업보고서 문단만 봅니다. 그 밖의 새 공시는 확인하지 않습니다.",
  sourceGone: "원래 대화는 삭제되었습니다(이 기록의 스냅샷은 남아 있습니다)",
};

// ── 순수 함수 ─────────────────────────────────────────────────────

export function decisionLabel(d) {
  return Object.hasOwn(DECISION_LABELS, d ?? "") ? DECISION_LABELS[d] : String(d ?? "-");
}

/** ISO 시각 → KST 날짜(YYYY-MM-DD). 서버의 다시 볼 날짜·today_kst와 같은 기준이다. */
export function kstDate(iso) {
  const t = Date.parse(iso ?? "");
  if (!Number.isFinite(t)) return "-";
  return new Date(t + 9 * 3600_000).toISOString().slice(0, 10);
}

export function todayKst() {
  return kstDate(new Date().toISOString());
}

/** YYYY-MM-DD에 n개월을 더한다. 그 달에 같은 날이 없으면 말일. */
export function addMonths(ymd, n) {
  const [y, m, d] = String(ymd).split("-").map(Number);
  const mi = (m - 1) + n;
  const y2 = y + Math.floor(mi / 12), m2 = ((mi % 12) + 12) % 12;
  const last = new Date(Date.UTC(y2, m2 + 1, 0)).getUTCDate();
  const p = v => String(v).padStart(2, "0");
  return `${y2}-${p(m2 + 1)}-${p(Math.min(d, last))}`;
}

/** 앞 n자(코드포인트) + "…". */
export function truncate(s, n) {
  const cps = Array.from(String(s ?? ""));
  return cps.length > n ? cps.slice(0, n).join("") + "…" : cps.join("");
}

/** 요약줄 버튼 상태(spec 3.2): "hidden" | "disabled"(진행 중) | "record" | "recorded"(이 실행의 기록 있음). */
export function recordButtonState(enabled, run) {
  if (!enabled || !run) return "hidden";
  if (ACTIVE.has(run.status)) return "disabled";
  return run.journal_entry_id ? "recorded" : "record";
}

/** 판정 없이 기록한 판단 띠(결정 3-2). 판정이 있는 실행이면 null. */
export function noJudgeBand(status) {
  return Object.hasOwn(NO_JUDGE, status ?? "") ? `판정 없이 기록한 판단(당시 판정: ${NO_JUDGE[status]})` : null;
}

export function snapshotHead(snap) {
  const r = snap?.run || {};
  return `당시 판정 정책 ${r.policy_version || "-"} · ${kstDate(r.finished_at || r.created_at)} 판정`;
}

/** 입력 확인. 문제가 있으면 사용자에게 보일 문구, 없으면 null. */
export function validateJudgment(v) {
  if (!DECISIONS.includes(v?.decision)) return "내 판단을 고르세요.";
  if (!(Number.isInteger(v.conviction) && v.conviction >= 1 && v.conviction <= 5)) return "내 확신(1~5)을 고르세요.";
  if (Array.from(String(v.memo ?? "")).length > MEMO_MAX) return "내 메모는 2,000자 이하로 씁니다.";
  return null;
}

/** 변화 비교 응답(GET /api/journal/{id}/changes)을 화면 모델로. 모양이 조금이라도 어긋나면 확인 불가로 본다
 *  ("변화 없음"으로 보이지 않게, spec 3.2·결정 6-1). ✅·⚠️ 근거 문단을 맨 위에(문장 순서), 나머지는 원래 순서. */
export function changesModel(raw, snap) {
  const UN = { status: "unavailable" };
  if (!raw || raw.status !== "ok" || !raw.report || !REPORT_STATES.has(raw.report.status)
      || !Array.isArray(raw.passages)) return UN;
  for (const p of raw.passages) {
    if (!p || !Number.isInteger(p.passage_idx) || (p.status !== "same" && p.status !== "gone")) return UN;
  }
  const rank = new Map();
  for (const c of [...(snap?.claims || [])].sort((a, b) => a.idx - b.idx)) {
    if ((c.status === "supported" || c.status === "contradicted") && Number.isInteger(c.source_idx)
        && !rank.has(c.source_idx)) rank.set(c.source_idx, rank.size);
  }
  const passages = raw.passages.map((p, i) => ({ ...p, cited: rank.has(p.passage_idx), _o: i }))
    .sort((a, b) => (rank.get(a.passage_idx) ?? Infinity) - (rank.get(b.passage_idx) ?? Infinity) || a._o - b._o);
  const pol = raw.policy;
  const policy = pol && typeof pol.snapshot === "string" && typeof pol.current === "string"
    ? { before: pol.snapshot, after: pol.current, changed: pol.snapshot !== pol.current } : null;
  const r = raw.report;
  return { status: "ok", passages, policy,
           report: { status: r.status, before: r.snapshot_rcept_no ?? snap?.rcept_no ?? "-",
                     after: Array.isArray(r.current_rcept_nos) ? r.current_rcept_nos : [] } };
}

function isListShape(d) {
  return !!d && Array.isArray(d.items) && Number.isInteger(d.due_count) && Number.isInteger(d.total);
}

/** 422 detail([{loc, msg}]) → 칸 이름 목록. 서버는 입력값을 되돌려 주지 않는다(결정 5-5). */
function invalidFields(detail) {
  if (!Array.isArray(detail)) return [];
  return [...new Set(detail.map(e => {
    const loc = Array.isArray(e?.loc) ? e.loc : [];
    const f = loc.find(x => Object.hasOwn(FIELD_NAMES, String(x)));
    return f ? FIELD_NAMES[f] : null;
  }).filter(Boolean))];
}

// ── 요청 ──────────────────────────────────────────────────────────
// common.js api()는 상태 코드를 버린다. 409(기존 기록 id)·404·5xx를 구분해야 해서 따로 둔다
async function call(path, { method = "GET", body } = {}) {
  let res;
  try {
    res = await fetch(path, { method, credentials: "include",
                              headers: body ? { "Content-Type": "application/json" } : {},
                              body: body ? JSON.stringify(body) : undefined });
  } catch {
    return { status: 0, data: null };
  }
  let data = null;
  try { data = await res.json(); } catch { /* 본문 없음 */ }
  return { status: res.status, data };
}

// ── 기능 확인 ─────────────────────────────────────────────────────
const jr = {
  enabled: false, probe: null, evidence: null, wired: false,
  filters: { due: false, corp: "", decision: "" }, companies: new Map(),
  items: [], total: 0, listSeq: 0,
  detail: null, detailSeq: 0, pendingOpen: null,
};

function setDue(n) {
  document.body.classList.toggle("jr-due", n > 0);
  const banner = document.getElementById("jr-due-banner");
  if (!banner) return;
  banner.textContent = n > 0 ? `다시 볼 날짜가 지난 기록 ${n}건` : "";
  banner.classList.toggle("hidden", !(n > 0));
}

/** 앱 시작 때 한 번: GET /api/journal?limit=1로 기능을 확인하고 탭·요약줄 버튼을 켠다. 결과(bool) 약속을 돌려준다. */
export function initJournal() {
  jr.probe ??= (async () => {
    const { status, data } = await call("/api/journal?limit=1");
    jr.enabled = status === 200 && isListShape(data);
    document.body.classList.toggle("jr-on", jr.enabled);
    if (jr.enabled) {
      setDue(data.due_count);
      setSummaryExtension(SUMMARY_EXT);
    }
    return jr.enabled;
  })();
  return jr.probe;
}

/** 근거 모드를 쓸 수 있는가(같은 질문 다시 묻기 표시용). 일지를 열 때만 묻는다. */
function evidenceAvailable() {
  jr.evidence ??= call("/api/evidence/notice").then(r => r.status === 200);
  return jr.evidence;
}

// ── 기록 양식(답변 아래·다시 보기 공용) ────────────────────────────
let formSeq = 0;

function reliedOptions(claims, runStatus) {
  const picks = (claims || []).filter(c => c.status !== "not_claim");
  if (!picks.length) return `<p class="jr-hint">고를 수 있는 문장이 없습니다.</p>`;
  return `<ul class="jr-relied-list">${picks.map(c => {
    const kind = badgeKind(runStatus, c.status);
    const mark = kind ? badgeMark(kind) : null;
    const badge = mark ? ` <span class="ev-badge ${mark.cls}" title="${escHtml(`당시 AI 판정 — ${mark.label}`)}">${mark.glyph}</span>` : "";
    return `<li><label><input type="checkbox" class="jr-relied" value="${c.idx}" /><span>${escHtml(c.text)}${badge}</span></label></li>`;
  }).join("")}</ul>`;
}

function formHtml(claims, runStatus) {
  const n = `jrf${++formSeq}`;
  return `<form class="jr-form" novalidate>
    <p class="jr-form-notice">${escHtml(JOURNAL_NOTICE)}</p>
    <fieldset class="jr-field"><legend>내 판단<span class="jr-req">필수</span></legend>
      <div class="jr-opts">${DECISIONS.map(d => `<label class="jr-decision-opt"><input type="radio" class="jr-decision" name="${n}-d" value="${d}" />${escHtml(DECISION_LABELS[d])}</label>`).join("")}</div>
    </fieldset>
    <fieldset class="jr-field"><legend>기댄 문장<span class="jr-optional">선택 · 문장 옆은 당시 AI 판정</span></legend>
      ${reliedOptions(claims, runStatus)}
    </fieldset>
    <label class="jr-field"><span class="jr-field-name">내 메모<span class="jr-optional">선택 · 어디로도 보내지 않습니다</span></span>
      <textarea class="jr-memo-input input" rows="3" maxlength="${MEMO_MAX}"></textarea>
      <span class="jr-memo-count">0 / 2,000</span>
    </label>
    <fieldset class="jr-field jr-conviction-row"><legend>내 확신<span class="jr-req">필수 · 1 낮음 ~ 5 높음</span></legend>
      <div class="jr-opts">${[1, 2, 3, 4, 5].map(v => `<label><input type="radio" class="jr-conviction" name="${n}-c" value="${v}" />${v}</label>`).join("")}</div>
    </fieldset>
    <div class="jr-field"><span class="jr-field-name">다시 볼 날짜<span class="jr-optional">선택 · 알림은 이 앱의 일지 탭에서만</span></span>
      <div class="jr-review-row">
        <input type="date" class="jr-review input" aria-label="다시 볼 날짜" />
        <button type="button" class="jr-quick btn-secondary text-xs" data-months="1">1개월 뒤</button>
        <button type="button" class="jr-quick btn-secondary text-xs" data-months="3">3개월 뒤</button>
      </div>
    </div>
    <div class="jr-form-msg" role="alert"></div>
    <div class="jr-form-actions">
      <button type="button" class="jr-cancel btn-secondary text-xs">취소</button>
      <button type="button" class="jr-save btn-primary text-xs">저장</button>
    </div>
  </form>`;
}

function readForm(form) {
  const conv = form.querySelector("input.jr-conviction:checked");
  return {
    decision: form.querySelector("input.jr-decision:checked")?.value,
    conviction: conv ? Number(conv.value) : undefined,
    memo: form.querySelector("textarea.jr-memo-input").value,
    relied_claims: [...form.querySelectorAll("input.jr-relied:checked")].map(e => Number(e.value)).sort((a, b) => a - b),
    review_on: form.querySelector("input.jr-review").value || null,
  };
}

/** host 안에 양식을 그린다. submit(payload) → {msg?, lock?} 또는 null(성공: 호출자가 양식을 치운다). */
function mountForm(host, { claims, runStatus, submit, cancel }) {
  host.innerHTML = formHtml(claims, runStatus);
  const form = host.querySelector(".jr-form");
  const msgEl = form.querySelector(".jr-form-msg");
  const save = form.querySelector(".jr-save");
  const memo = form.querySelector("textarea.jr-memo-input");
  const count = form.querySelector(".jr-memo-count");
  memo.addEventListener("input", () => {
    count.textContent = `${Array.from(memo.value).length.toLocaleString("ko-KR")} / 2,000`;
  });
  form.addEventListener("submit", e => e.preventDefault());
  form.querySelectorAll(".jr-quick").forEach(b => b.addEventListener("click", () => {
    form.querySelector("input.jr-review").value = addMonths(todayKst(), Number(b.dataset.months));
  }));
  form.querySelector(".jr-cancel").addEventListener("click", cancel);
  save.addEventListener("click", async () => {
    const v = readForm(form);
    const bad = validateJudgment(v);
    msgEl.textContent = bad || "";
    if (bad) return;
    save.disabled = true;
    const res = await submit(v);
    if (!res) return;
    msgEl.textContent = res.msg || "";
    save.disabled = !!res.lock;
  });
  form.querySelector("input.jr-decision")?.focus();
  return form;
}

/** 저장 응답의 공통 실패 처리(spec 3.2 기록 양식 상태). */
function failure(status, data, { goneMsg }) {
  if (status === 404) return { msg: goneMsg, lock: true };
  if (status === 422) {
    const f = invalidFields(data?.detail);
    return { msg: `입력을 확인해 주세요${f.length ? `: ${f.join(", ")}` : "."}` };
  }
  if (status === 401) return { msg: "로그인이 필요합니다. 입력은 그대로 있습니다." };
  return { msg: MSG.saveFail };
}

// ── 요약줄 버튼(근거 모드 답변) ────────────────────────────────────
const SUMMARY_EXT = {
  html(msg) {
    switch (recordButtonState(jr.enabled, msg.run)) {
      case "disabled":
        return ` <span class="ev-journal-wrap" title="${MSG.notDone}"><button type="button" class="ev-journal btn-secondary text-xs" disabled>판단 기록</button></span>`;
      case "record": {
        const open = msg.extraEl.dataset.runId === msg.run.id;
        return ` <span class="ev-journal-wrap"><button type="button" class="ev-journal btn-secondary text-xs" aria-expanded="${open}">판단 기록</button></span>`;
      }
      case "recorded":
        return ` <a href="#journal" class="ev-journal-link">기록됨 · 일지에서 보기</a>`;
      default:
        return "";
    }
  },
  onClick(msg, e) {
    if (e.target.closest(".ev-journal")) toggleRecordForm(msg);
    if (e.target.closest(".ev-journal-link")) {
      e.preventDefault();
      openEntry(msg.run.journal_entry_id);
    }
  },
};

function closeRecordForm(msg) {
  msg.extraEl.innerHTML = "";
  delete msg.extraEl.dataset.runId;
  patchRun(msg, {});
}

function toggleRecordForm(msg) {
  if (msg.extraEl.dataset.runId === msg.run.id) return closeRecordForm(msg);
  const run = msg.run;
  msg.extraEl.dataset.runId = run.id;
  mountForm(msg.extraEl, {
    claims: run.claims, runStatus: run.status,
    cancel: () => closeRecordForm(msg),
    submit: async v => {
      const { status, data } = await call("/api/journal", { method: "POST", body: { run_id: run.id, ...v } });
      if (msg.run.id !== run.id) return null;  // 기다리는 사이 다른 실행으로 바뀌었다(양식은 이미 닫혔다)
      if (status === 201 && data?.id) {
        msg.extraEl.innerHTML = "";
        delete msg.extraEl.dataset.runId;
        patchRun(msg, { journal_entry_id: data.id });
        setToast("판단을 기록했습니다 · 일지에서 보기");
        return null;
      }
      if (status === 409 && data?.entry_id) {  // 같은 실행의 기록이 이미 있다(결정 5-2 유일 조건)
        msg.extraEl.innerHTML = "";
        delete msg.extraEl.dataset.runId;
        patchRun(msg, { journal_entry_id: data.entry_id });
        setToast("이 판정에는 이미 기록이 있습니다 · 일지에서 보기");
        return null;
      }
      if (status === 409 && String(data?.detail ?? "").includes("판정이 끝난 뒤")) return { msg: MSG.notDone };
      if (status === 409) return { msg: MSG.runGone, lock: true };
      return failure(status, data, { goneMsg: MSG.runGone });
    },
  });
  patchRun(msg, {});  // aria-expanded
}

// ── 일지 탭: 목록 ─────────────────────────────────────────────────
const $ = id => document.getElementById(id);

function rowHtml(it) {
  const cur = it.current || {};
  const review = cur.review_on ? `다시 볼 날짜 ${escHtml(cur.review_on)}` : "다시 볼 날짜 없음";
  return `<li><button type="button" class="jr-row" data-id="${escHtml(it.id)}">
    <span class="jr-row-main">
      <span class="jr-row-company">${it.due ? `<span class="jr-due-dot" role="img" aria-label="다시 볼 날짜가 지남" title="다시 볼 날짜가 지났습니다"></span> ` : ""}${escHtml(it.company)}</span>
      <span class="jr-row-q">${escHtml(truncate(it.question, 40))}</span>
    </span>
    <span class="jr-row-meta">
      <span class="jr-chip">${escHtml(decisionLabel(cur.decision))}</span>
      <span>내 확신 ${escHtml(cur.conviction ?? "-")}</span>
      <span>기록 ${escHtml(kstDate(it.created_at))}</span>
      <span>${review}</span>
    </span>
  </button></li>`;
}

function renderCompanies() {
  const sel = $("jr-f-company");
  const names = [...jr.companies.entries()].sort((a, b) => (a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0));
  sel.innerHTML = `<option value="">전체 회사</option>`
    + names.map(([code, name]) => `<option value="${escHtml(code)}">${escHtml(name)}</option>`).join("");
  sel.value = jr.filters.corp;
}

function filtered() {
  const f = jr.filters;
  return f.due || f.corp || f.decision;
}

async function loadList({ append = false } = {}) {
  const seq = ++jr.listSeq;
  const f = jr.filters;
  const q = new URLSearchParams();
  if (f.due) q.set("due", "true");
  if (f.corp) q.set("corp_code", f.corp);
  if (f.decision) q.set("decision", f.decision);
  q.set("limit", String(PAGE));
  q.set("offset", String(append ? jr.items.length : 0));
  const { status, data } = await call(`/api/journal?${q}`);
  if (seq !== jr.listSeq) return;  // 그사이 거르기를 바꿨다
  const empty = $("jr-empty");
  if (status !== 200 || !isListShape(data)) {
    if (!append) $("jr-list").innerHTML = "";
    empty.textContent = "일지를 불러오지 못했습니다. 잠시 뒤 다시 열어 주세요.";
    empty.classList.remove("hidden");
    return;
  }
  jr.items = append ? [...jr.items, ...data.items] : data.items;
  jr.total = data.total;
  setDue(data.due_count);
  let added = false;
  for (const it of data.items) {
    if (it.corp_code && !jr.companies.has(it.corp_code)) { jr.companies.set(it.corp_code, it.company); added = true; }
  }
  if (added) renderCompanies();
  $("jr-list").innerHTML = jr.items.map(rowHtml).join("");
  empty.textContent = jr.items.length ? "" : (filtered() ? MSG.emptyFiltered : MSG.empty);
  empty.classList.toggle("hidden", jr.items.length > 0);
  $("jr-more").classList.toggle("hidden", jr.items.length >= jr.total);
}

function showList() {
  jr.detail = null;
  jr.detailSeq++;
  $("jr-detail").classList.add("hidden");
  $("jr-detail").innerHTML = "";
  $("jr-list-wrap").classList.remove("hidden");
}

// ── 일지 탭: 상세 ─────────────────────────────────────────────────

function updateHtml(u, snap) {
  const claims = new Map((snap.claims || []).map(c => [c.idx, c]));
  const relied = (u.relied_claims || []).map(i => claims.get(i)).filter(Boolean);
  const reliedHtml = relied.length
    ? `<div class="jr-relied"><span class="jr-src">기댄 문장</span><ul>${relied.map(c => {
        const kind = badgeKind(snap.run?.status, c.status);
        const mark = kind ? badgeMark(kind) : null;
        return `<li>${escHtml(c.text)}${mark ? ` <span class="ev-badge ${mark.cls}" title="${escHtml(`당시 AI 판정 — ${mark.label}`)}">${mark.glyph}</span>` : ""}</li>`;
      }).join("")}</ul></div>`
    : "";
  return `<li class="jr-update">
    <div class="jr-update-head">${u.kind === "initial" ? "처음 판단" : "다시 보기"} · ${escHtml(kstDate(u.created_at))}</div>
    <div><span class="jr-src">내 판단</span><span class="jr-chip">${escHtml(decisionLabel(u.decision))}</span> · 내 확신 ${escHtml(u.conviction)}</div>
    <div><span class="jr-src">내 메모</span>${u.memo ? `<p class="jr-memo">${escHtml(u.memo)}</p>` : `<span class="jr-hint">없음</span>`}</div>
    ${reliedHtml}
    <div class="jr-hint">다시 볼 날짜 ${u.review_on ? escHtml(u.review_on) : "없음"}</div>
  </li>`;
}

function snapshotRun(snap) {
  return { ...(snap.run || {}), passages: snap.passages || [], rcept_no: snap.rcept_no };
}

function snapshotAnswerHtml(snap) {
  const run = snapshotRun(snap);
  return segmentsFromClaims(snap.answer, snap.claims).map(seg => {
    if (!seg.claim) return escHtml(seg.text);
    const kind = badgeKind(run.status, seg.claim.status);
    const muted = seg.claim.status === "not_claim" ? " ev-muted" : "";
    const span = `<span class="ev-claim${muted}" data-idx="${seg.claim.idx}">${escHtml(seg.text)}</span>`;
    if (!kind) return span;
    const m = badgeMark(kind);
    const title = escHtml(`당시 AI 판정 — ${badgeTitle(kind, seg.claim, run)}`);
    return `${span}<button type="button" class="ev-badge ${m.cls}" data-idx="${seg.claim.idx}" data-kind="${kind}"
      aria-expanded="false" aria-label="${title}" title="${title}">${m.glyph}</button>`;
  }).join("");
}

function detailHtml(d) {
  const snap = d.snapshot;
  const band = noJudgeBand(snap.run?.status);
  return `<div class="jr-detail-bar">
      <button id="jr-back" type="button" class="btn-secondary text-xs">← 목록</button>
      <span class="jr-actions"><span class="jr-reask-slot"></span>
        <button id="jr-delete" type="button" class="btn-secondary text-xs">기록 삭제</button></span>
    </div>
    <div class="jr-cols">
      <section class="jr-col jr-col-judgment" aria-label="당시 판단">
        <h3>당시 판단</h3>
        <ol class="jr-updates">${d.updates.map(u => updateHtml(u, snap)).join("")}</ol>
        <div><button id="jr-revisit-open" type="button" class="btn-secondary text-xs">다시 보기 기록 추가</button></div>
        <div class="jr-revisit-host"></div>
        <p class="jr-hint">판단은 고치지 않고 덧붙입니다. 지우려면 기록 전체를 지웁니다.</p>
      </section>
      <section class="jr-col jr-col-snapshot" aria-label="당시 근거">
        <h3>당시 근거(스냅샷)</h3>
        <div class="jr-snap-head">${escHtml(snapshotHead(snap))}</div>
        ${band ? `<div class="jr-nojudge">${escHtml(band)}</div>` : ""}
        <div><span class="jr-src">질문</span>${escHtml(snap.question)} <span class="jr-hint">· ${escHtml(snap.company)}</span></div>
        <div><div class="ai-label">AI 생성 답변</div><div class="ev-answer jr-snap-answer">${snapshotAnswerHtml(snap)}</div></div>
        <div class="ev-panels jr-snap-panels"></div>
        <p class="jr-snap-foot">배지는 당시 AI 판정입니다 · 검색된 공시 문단 기준이며 사실 여부를 보증하지 않습니다. 당시 기준으로 보존되며 다시 판정하지 않습니다.</p>
        ${d.source_available === false ? `<p class="jr-source-gone">${MSG.sourceGone}</p>` : ""}
      </section>
      <section class="jr-col jr-col-changes" aria-label="그 뒤 바뀐 것" aria-live="polite">
        <h3>그 뒤 바뀐 것</h3>
        <div class="jr-ch-body">${MSG.checking}</div>
      </section>
    </div>`;
}

function reaskButton() {
  return `<button type="button" class="jr-reask btn-secondary text-xs" title="근거 모드에 같은 회사·질문을 채웁니다(보내기는 직접 누릅니다)">같은 질문 다시 묻기</button>`;
}

function changesHtml(model, evidenceOn) {
  if (model.status !== "ok") {
    return `<p class="jr-unavailable">${MSG.unavailable}</p><p class="jr-ch-help">${MSG.scope}</p>`;
  }
  const r = model.report;
  const report = r.status === "same"
    ? `적재된 보고서가 당시와 같습니다(접수번호 ${escHtml(r.before)}).`
    : r.status === "replaced"
      ? `적재된 보고서가 바뀌었습니다: 당시 ${escHtml(r.before)} → 현재 ${escHtml(r.after.join(", ") || "-")}`
      : "이 회사의 문단이 지금 적재되어 있지 않습니다. 당시 근거 문단을 모두 찾을 수 없습니다.";
  const snapPassages = jr.detail?.snapshot?.passages || [];
  const rows = model.passages.map(p => {
    const sp = snapPassages[p.passage_idx] || {};
    const what = p.status === "same" ? "당시 문단이 지금도 적재되어 있습니다" : "당시 문단과 같은 본문이 지금 적재된 문단에 없습니다";
    return `<li class="jr-ch-passage" data-status="${p.status}">문단 ${escHtml(sp.idx ?? p.passage_idx)} · ${escHtml(sp.section || "-")}${p.cited ? ` <span class="jr-ch-tag">(✅·⚠️ 판정 근거)</span>` : ""} — ${what}</li>`;
  }).join("");
  const policy = model.policy
    ? (model.policy.changed
      ? `당시 판정 기준 ${escHtml(model.policy.before)}, 현재 ${escHtml(model.policy.after)}. 당시 배지는 당시 기준으로 보존됩니다.`
      : `판정 기준은 당시와 같습니다(${escHtml(model.policy.after)}).`)
    : "";
  const gone = model.passages.some(p => p.status === "gone");
  return `<p class="jr-ch-report">${report}</p>
    <ul class="jr-ch-list">${rows}</ul>
    ${gone && evidenceOn ? `<div>${reaskButton()} <span class="jr-hint">바뀐 근거는 새 질문(새 판정)으로만 봅니다.</span></div>` : ""}
    ${policy ? `<p class="jr-ch-policy">${policy}</p>` : ""}
    <p class="jr-ch-help">${MSG.scope}</p>`;
}

async function loadChanges(id, seq) {
  const [{ status, data }, evidenceOn] = await Promise.all([
    call(`/api/journal/${encodeURIComponent(id)}/changes`), evidenceAvailable()]);
  if (seq !== jr.detailSeq) return;
  const model = status === 200 ? changesModel(data, jr.detail.snapshot) : { status: "unavailable" };
  const body = document.querySelector("#jr-detail .jr-ch-body");
  if (body) body.innerHTML = changesHtml(model, evidenceOn);
  const slot = document.querySelector("#jr-detail .jr-reask-slot");
  if (slot) slot.innerHTML = evidenceOn ? reaskButton() : "";
}

function toggleSnapshotPanel(btn) {
  const snap = jr.detail.snapshot;
  const idx = Number(btn.dataset.idx);
  const host = document.querySelector("#jr-detail .jr-snap-panels");
  const open = btn.getAttribute("aria-expanded") === "true";
  host.querySelector(`.ev-panel[data-idx="${idx}"]`)?.remove();
  btn.setAttribute("aria-expanded", String(!open));
  if (open) return;
  const claim = snap.claims.find(c => c.idx === idx);
  const div = document.createElement("div");
  div.className = "ev-panel";
  div.dataset.idx = idx;
  div.setAttribute("role", "region");
  div.setAttribute("aria-label", `문장 ${idx + 1} 당시 근거`);
  div.innerHTML = panelHtml(snapshotRun(snap), claim, btn.dataset.kind);
  host.insertBefore(div, [...host.children].find(el => Number(el.dataset.idx) > idx) || null);
}

function toggleRevisit() {
  const host = document.querySelector("#jr-detail .jr-revisit-host");
  if (host.firstChild) { host.innerHTML = ""; return; }
  const d = jr.detail;
  const id = d.id;
  mountForm(host, {
    claims: d.snapshot.claims, runStatus: d.snapshot.run?.status,
    cancel: () => { host.innerHTML = ""; },
    submit: async v => {
      const { status, data } = await call(`/api/journal/${encodeURIComponent(id)}/updates`, { method: "POST", body: v });
      if (jr.detail?.id !== id) return null;
      if (status === 201 && data?.id) {
        jr.detail.updates.push(data);
        document.querySelector("#jr-detail .jr-updates").innerHTML =
          jr.detail.updates.map(u => updateHtml(u, jr.detail.snapshot)).join("");
        host.innerHTML = "";
        setToast("다시 보기 기록을 덧붙였습니다");
        loadList();  // 최근 판단·다시 볼 날짜가 바뀐다
        return null;
      }
      return failure(status, data, { goneMsg: MSG.entryGone });
    },
  });
}

async function showDetail(id) {
  const seq = ++jr.detailSeq;
  $("jr-list-wrap").classList.add("hidden");
  const box = $("jr-detail");
  box.classList.remove("hidden");
  box.innerHTML = `<p class="jr-empty">불러오는 중…</p>`;
  const { status, data } = await call(`/api/journal/${encodeURIComponent(id)}`);
  if (seq !== jr.detailSeq) return;
  if (status !== 200 || !data?.snapshot || !Array.isArray(data.updates)) {
    box.innerHTML = `<div class="jr-detail-bar"><button id="jr-back" type="button" class="btn-secondary text-xs">← 목록</button></div>
      <p class="jr-empty">${status === 404 ? MSG.entryGone : "기록을 불러오지 못했습니다. 잠시 뒤 다시 열어 주세요."}</p>`;
    return;
  }
  jr.detail = data;
  box.innerHTML = detailHtml(data);
  box.scrollIntoView({ block: "start" });
  loadChanges(id, seq);
}

// ── 삭제·내보내기 ─────────────────────────────────────────────────

/** 확인 대화상자. typed가 있으면 그 글자를 정확히 입력해야 확인 버튼이 켜진다. */
function confirmDialog({ title, text, ok, typed = null }) {
  const dlg = $("jr-confirm");
  const input = $("jr-confirm-input");
  const okBtn = $("jr-confirm-ok");
  $("jr-confirm-title").textContent = title;
  $("jr-confirm-text").textContent = text;
  okBtn.textContent = ok;
  input.value = "";
  input.classList.toggle("hidden", !typed);
  input.placeholder = typed || "";
  okBtn.disabled = !!typed;
  return new Promise(resolve => {
    const done = v => {
      okBtn.removeEventListener("click", onOk);
      $("jr-confirm-cancel").removeEventListener("click", onCancel);
      dlg.removeEventListener("cancel", onCancel);
      input.removeEventListener("input", onInput);
      if (typeof dlg.close === "function" && dlg.open) dlg.close(); else dlg.removeAttribute("open");
      resolve(v);
    };
    const onOk = () => done(true);
    const onCancel = () => done(false);
    const onInput = () => { okBtn.disabled = input.value !== typed; };
    okBtn.addEventListener("click", onOk);
    $("jr-confirm-cancel").addEventListener("click", onCancel);
    dlg.addEventListener("cancel", onCancel);  // Esc
    input.addEventListener("input", onInput);
    if (typeof dlg.showModal === "function") dlg.showModal(); else dlg.setAttribute("open", "");
    (typed ? input : $("jr-confirm-cancel")).focus();
  });
}

async function deleteEntry() {
  const id = jr.detail?.id;
  if (!id) return;
  const yes = await confirmDialog({ title: "판단 기록 삭제", ok: "지우기",
    text: "이 기록과 다시 보기 기록을 모두 지웁니다. 지운 기록은 복구할 수 없습니다." });
  if (!yes) return;
  const { status, data } = await call(`/api/journal/${encodeURIComponent(id)}`, { method: "DELETE" });
  if (status !== 200 && status !== 404) return setToast(data?.detail || "기록을 지우지 못했습니다.", "error");
  forgetJournalEntries(e => e === id);
  setToast("기록을 지웠습니다");
  showList();
  loadList();
}

async function deleteAll() {
  const yes = await confirmDialog({ title: "판단 기록 전체 삭제", ok: "전체 삭제", typed: "전체 삭제",
    text: "내 판단 기록을 모두 지웁니다. 지운 기록은 복구할 수 없습니다. 필요하면 먼저 내보내기로 보관하세요. 계속하려면 '전체 삭제'를 입력하세요." });
  if (!yes) return;
  const { status, data } = await call("/api/journal?confirm=delete-all", { method: "DELETE" });
  if (status !== 200) return setToast(data?.detail || "기록을 지우지 못했습니다.", "error");
  forgetJournalEntries(() => true);
  setToast(`기록 ${data?.deleted ?? 0}건을 지웠습니다`);
  jr.companies.clear();
  renderCompanies();
  showList();
  loadList();
}

async function exportAll() {
  let res;
  try {
    res = await fetch("/api/journal/export", { credentials: "include" });
  } catch {
    return setToast("내보내지 못했습니다. 네트워크 상태를 확인해 주세요.", "error");
  }
  if (!res.ok) return setToast(`내보내지 못했습니다(HTTP ${res.status}).`, "error");
  const m = /filename="?([^";]+)"?/.exec(res.headers.get("Content-Disposition") || "");
  const name = m ? m[1] : `lumina-journal-${todayKst().replaceAll("-", "")}.json`;
  const url = URL.createObjectURL(await res.blob());
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function reask() {
  const snap = jr.detail?.snapshot;
  if (!snap) return;
  navigate("agent-chat");
  const ok = await prefillEvidenceChat({ corp_name: snap.company, corp_code: snap.corp_code }, snap.question);
  if (!ok) setToast("지금은 공시 근거 모드를 쓸 수 없습니다.", "error");
}

// ── 연결 ──────────────────────────────────────────────────────────

function wire() {
  jr.wired = true;
  $("jr-list").addEventListener("click", e => {
    const row = e.target.closest(".jr-row");
    if (row) showDetail(row.dataset.id);
  });
  $("jr-more").addEventListener("click", () => loadList({ append: true }));
  $("jr-f-due").addEventListener("change", e => { jr.filters.due = e.target.checked; loadList(); });
  $("jr-f-company").addEventListener("change", e => { jr.filters.corp = e.target.value; loadList(); });
  document.querySelector(".jr-f-decision").addEventListener("click", e => {
    const b = e.target.closest("button[data-decision]");
    if (!b) return;
    jr.filters.decision = b.dataset.decision;
    document.querySelectorAll(".jr-f-decision button").forEach(x =>
      x.setAttribute("aria-pressed", String(x === b)));
    loadList();
  });
  $("jr-export").addEventListener("click", exportAll);
  $("jr-delete-all").addEventListener("click", deleteAll);
  $("jr-detail").addEventListener("click", e => {
    if (e.target.closest("#jr-back")) { showList(); return; }
    if (e.target.closest("#jr-delete")) { deleteEntry(); return; }
    if (e.target.closest("#jr-revisit-open")) { toggleRevisit(); return; }
    if (e.target.closest(".jr-reask")) { reask(); return; }
    const badge = e.target.closest(".jr-snap-answer button.ev-badge");
    if (badge) toggleSnapshotPanel(badge);
  });
}

/** 요약줄 '기록됨' 링크에서: 일지 탭으로 옮겨 그 기록을 연다. */
function openEntry(id) {
  if (!id) return;
  jr.pendingOpen = id;
  navigate("journal");
}

/** 일지 탭이 열릴 때마다. 기능이 꺼져 있으면 상담 화면으로 돌린다(탭 없음, spec 3.2). */
export async function onJournalViewActivated() {
  const ok = await initJournal();
  if (!ok) { navigate("agent-chat"); return; }
  if (!jr.wired) wire();
  const pending = jr.pendingOpen;
  jr.pendingOpen = null;
  if (pending) showDetail(pending); else if (!jr.detail) showList();
  loadList();
}
