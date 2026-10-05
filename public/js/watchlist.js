/* 관심종목 패널·별 버튼(모듈 D spec 3절, 결정 7-1~7-3·8절, 11절 P2).
 * - 기업 지표 화면(company-dashboard) 맨 위 패널: 불러오는 중·비어 있음·목록·불러오기 실패(다시 시도)·로그인 안 됨(숨김).
 * - 줄의 연결 버튼: (a) 지표 보기 (b) 근거 모드로 질문 (c) 판단 기록 N · 다시 볼 때 M. 꺼진 기능·corp_code 없는 줄은
 *   버튼을 그리지 않는다. 켜짐 판단은 각 모듈의 기존 확인(evidence.js initEvidence, journal.js initJournal)을 기다린다.
 * - 이름·메모는 textContent로만 넣는다(HTML 삽입 없음). 메모는 근거 모드 질문 칸 등 어디로도 보내지 않는다(결정 8-1).
 * - 이 파일은 /api/watchlist·/api/journal(개수) 밖을 부르지 않는다. 가격·등락을 담지 않는다.
 * 의존은 한 방향이다: watchlist.js → company.js·evidence.js·journal.js. 그쪽은 이 파일을 import하지 않는다. */
import { setToast } from "/js/common.js";
import { addAndSelectCompany, onCompanySelected } from "/js/company.js";
import { navigate } from "/js/core.js";
import { evidenceModeAvailable, prefillEvidenceChat } from "/js/evidence.js";
import { initJournal, openJournalForCompany } from "/js/journal.js";

// ── 상수·문구(spec 3.2·결정 7-2) ──────────────────────────────────
const MAX_ITEMS = 100;            // 서버 MAX_ITEMS(app/routes/watchlist.py)와 같은 값
const NOTE_MAX = 200;             // 서버 메모 상한과 같은 값
const COUNT_CONCURRENCY = 4;      // (c) 개수 요청 동시 실행 상한
const MSG = {
  loading: "관심종목을 불러오는 중…",
  empty: "관심종목이 없습니다. 종목을 검색해 ☆를 누르면 여기에 모입니다.",
  failed: "관심종목을 불러오지 못했습니다",
  limit: `관심종목은 ${MAX_ITEMS}개까지 담을 수 있습니다`,
  addFail: "관심종목에 담지 못했습니다. 잠시 뒤 다시 시도해 주세요.",
  removeFail: "관심종목에서 지우지 못했습니다. 잠시 뒤 다시 시도해 주세요.",
  noteFail: "메모를 저장하지 못했습니다. 입력은 그대로 있습니다.",
  noteTooLong: `메모는 ${NOTE_MAX}자 이하입니다.`,
  noteConfirm: "메모도 함께 지워집니다. 관심종목에서 지울까요?",
  evidenceOff: "지금은 근거 모드를 쓸 수 없습니다",
};
const STAR_ON = "★ 관심종목";
const STAR_OFF = "☆ 관심종목";

// ── 순수 함수 ─────────────────────────────────────────────────────

/**
 * 비동기 작업을 동시에 limit개까지만 돌린다. 결과는 입력 순서다.
 * @template T, R
 * @param {T[]} items 입력 목록
 * @param {number} limit 동시 실행 상한(1 이상)
 * @param {(item: T, index: number) => Promise<R>} fn 하나를 처리하는 함수
 * @returns {Promise<R[]>} 입력 순서의 결과
 */
export async function mapLimit(items, limit, fn) {
  const out = new Array(items.length);
  let next = 0;
  async function worker() {
    while (next < items.length) {
      const i = next++;
      out[i] = await fn(items[i], i);
    }
  }
  await Promise.all(Array.from({ length: Math.min(Math.max(1, limit), items.length) }, worker));
  return out;
}

/**
 * (c) 버튼 문구. 개수를 아직 모르면 "판단 기록", 조회 실패면 "판단 기록 보기", 0건이면 "판단 기록 0".
 * @param {{total: number, due: number}|null|undefined} count corp_code별 개수(undefined 조회 중, null 실패)
 * @returns {string} 버튼 문구
 */
export function journalLabel(count) {
  if (count === undefined) return "판단 기록";
  if (count === null) return "판단 기록 보기";
  if (count.total === 0) return "판단 기록 0";
  return `판단 기록 ${count.total} · 다시 볼 때 ${count.due}`;
}

/**
 * 줄에 보일 연결 버튼 종류. 꺼진 기능과 corp_code 없는 줄의 (b)·(c)는 넣지 않는다(결정 7-2).
 * @param {{corp_code: string|null}} item 관심종목 항목
 * @param {{evidence: boolean, journal: boolean}} on 근거 모드·일지 켜짐
 * @returns {string[]} "view" | "ask" | "journal" 목록
 */
export function linkKinds(item, on) {
  const kinds = ["view"];
  if (item.corp_code && on.evidence) kinds.push("ask");
  if (item.corp_code && on.journal) kinds.push("journal");
  return kinds;
}

/**
 * GET /api/watchlist 응답 모양 확인.
 * @param {any} d 응답 본문
 * @returns {boolean} items 배열이 있으면 true
 */
function isListShape(d) {
  return !!d && Array.isArray(d.items)
    && d.items.every(i => i && typeof i.id === "string" && typeof i.symbol === "string");
}

/**
 * 심볼 비교용 정규화(서버와 같이 앞뒤 공백 제거·대문자).
 * @param {string} s 심볼
 * @returns {string} 정규화한 심볼
 */
function norm(s) {
  return String(s ?? "").trim().toUpperCase();
}

// ── 요청 ──────────────────────────────────────────────────────────

/**
 * 상태 코드를 함께 돌려주는 요청(common.js api()는 상태 코드를 버린다. 401·409 code를 구분해야 한다).
 * @param {string} path 경로
 * @param {{method?: string, body?: object}} [opts] 메서드·JSON 본문
 * @returns {Promise<{status: number, data: any}>} 네트워크 오류면 status 0
 */
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
  try { data = await res.json(); } catch { /* 본문 없음(204) */ }
  return { status: res.status, data };
}

// ── 상태 ──────────────────────────────────────────────────────────
const wl = {
  state: "idle",        // idle | loading | list | error | unauthorized
  items: [], seq: 0,
  on: { evidence: false, journal: false },
  counts: new Map(),    // corp_code → {total, due} | null(실패). 없으면 조회 중
  current: null,        // 지표 화면에서 고른 종목 {symbol, name, exchange?}
  busy: false,          // 별 버튼 요청 중
  editing: null,        // 메모 편집 중 {id, draft}
  wired: false,
};

const $ = id => document.getElementById(id);

/**
 * 지금 목록에서 심볼이 같은 항목.
 * @param {string} symbol 심볼
 * @returns {object|undefined} 항목
 */
function findItem(symbol) {
  const s = norm(symbol);
  return wl.items.find(i => i.symbol === s);
}

// ── 그리기 ────────────────────────────────────────────────────────

/**
 * 패널 상태를 바꾸고 그린다.
 * @param {"loading"|"list"|"error"|"unauthorized"} state 패널 상태
 * @returns {void}
 */
function setState(state) {
  wl.state = state;
  const card = $("wl-card");
  card.dataset.state = state;
  card.classList.toggle("wl-hidden", state === "unauthorized");
  $("wl-retry").classList.toggle("wl-hidden", state !== "error");
  render();
}

/**
 * 상태 문구·줄·별 버튼을 다시 그린다.
 * @returns {void}
 */
function render() {
  const status = $("wl-status");
  const list = $("wl-list");
  const count = $("wl-count");
  if (wl.state === "list") {
    status.textContent = wl.items.length ? "" : MSG.empty;
    count.textContent = `${wl.items.length} / ${MAX_ITEMS}`;
    list.replaceChildren(...wl.items.map(rowEl));
  } else {
    status.textContent = wl.state === "loading" ? MSG.loading : wl.state === "error" ? MSG.failed : "";
    count.textContent = "";
    list.replaceChildren();
  }
  syncStar();
}

/**
 * 글자만 담은 요소를 만든다(textContent).
 * @param {string} tag 태그
 * @param {string} cls 클래스
 * @param {string} text 글자
 * @returns {HTMLElement} 요소
 */
function el(tag, cls, text) {
  const e = document.createElement(tag);
  e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

/**
 * 버튼 요소를 만든다.
 * @param {string} cls 클래스(동작 구분에 쓴다)
 * @param {string} text 버튼 글자
 * @returns {HTMLButtonElement} 버튼
 */
function btn(cls, text) {
  const b = el("button", cls, text);
  b.type = "button";
  return b;
}

/**
 * 관심종목 한 줄: 이름 · 심볼 · 시장 · 메모와 연결 버튼, 메모 편집 칸.
 * @param {object} it 관심종목 항목
 * @returns {HTMLLIElement} 줄 요소
 */
function rowEl(it) {
  const li = el("li", "wl-row");
  li.dataset.id = it.id;
  li.dataset.symbol = it.symbol;
  const main = el("div", "wl-main");
  main.append(el("span", "wl-name", it.name || it.symbol),
              el("span", "wl-meta", ["", it.symbol, it.market].filter(v => v !== null && v !== undefined).join(" · ")));
  if (it.note) {
    const note = el("span", "wl-note", `· ${it.note}`);
    note.title = "내 메모";
    main.append(note);
  }
  const links = el("div", "wl-links");
  for (const kind of linkKinds(it, wl.on)) {
    if (kind === "view") links.append(btn("wl-view", "지표 보기"));
    if (kind === "ask") links.append(btn("wl-ask", "근거 모드로 질문"));
    if (kind === "journal") links.append(btn("wl-journal", journalLabel(wl.counts.get(it.corp_code))));
  }
  links.append(btn("wl-note-edit", "메모"), btn("wl-delete", "삭제"));
  li.append(main, links);
  if (wl.editing?.id === it.id) {
    const form = el("div", "wl-note-form");
    const input = el("input", "wl-note-input input");
    input.type = "text";
    input.maxLength = NOTE_MAX;
    input.setAttribute("aria-label", "메모");
    input.value = wl.editing.draft;
    input.addEventListener("input", () => { if (wl.editing) wl.editing.draft = input.value; });
    form.append(input, btn("wl-note-save", "저장"), btn("wl-note-cancel", "취소"));
    li.append(form);
  }
  return li;
}

/**
 * 별 버튼: 목록을 불러온 뒤에만 보인다. 지금 종목이 목록에 있으면 ★, 없으면 ☆.
 * @returns {void}
 */
function syncStar() {
  const star = $("wl-star");
  if (!star) return;
  const show = wl.state === "list" && !!wl.current;
  star.classList.toggle("wl-hidden", !show);
  if (!show) return;
  const on = !!findItem(wl.current.symbol);
  star.textContent = on ? STAR_ON : STAR_OFF;
  star.setAttribute("aria-pressed", String(on));
  star.disabled = wl.busy;
}

// ── 불러오기 ──────────────────────────────────────────────────────

/**
 * 기업 지표 화면이 열릴 때마다: 목록을 불러오고, 연결 버튼(근거 모드·일지 켜짐)과 일지 개수를 채운다.
 * @returns {Promise<void>}
 */
export async function loadWatchlistPanel() {
  wire();
  const seq = ++wl.seq;
  wl.editing = null;
  $("wl-card").dataset.links = "pending";
  setState("loading");
  const { status, data } = await call("/api/watchlist");
  if (seq !== wl.seq) return;  // 그사이 다시 열었다
  if (status === 401) return setState("unauthorized");
  if (status !== 200 || !isListShape(data)) return setState("error");
  wl.items = data.items;
  wl.counts = new Map();
  setState("list");
  const [evidence, journal] = await Promise.all([
    evidenceModeAvailable().catch(() => false),
    initJournal().then(r => r === "on").catch(() => false),
  ]);
  if (seq !== wl.seq) return;
  wl.on = { evidence, journal };
  render();
  await fillCounts(seq);
  if (seq === wl.seq) $("wl-card").dataset.links = "ready";
}

/**
 * 조용히 목록만 다시 받는다(이미 있음 409 뒤). 실패하면 지금 목록을 그대로 둔다.
 * @returns {Promise<void>}
 */
async function reloadQuietly() {
  const { status, data } = await call("/api/watchlist");
  if (status === 401) return setState("unauthorized");
  if (status === 200 && isListShape(data)) {
    wl.items = data.items;
    render();
    await fillCounts(wl.seq);
  }
}

/**
 * 아직 개수를 모르는 corp_code마다 기록 수(total)와 다시 볼 때 된 수(due=true의 total)를 묻는다.
 * 같은 corp_code는 한 번만, 동시 요청은 COUNT_CONCURRENCY개까지. 일지가 꺼져 있으면 묻지 않는다.
 * @param {number} seq 패널 세대(그사이 다시 열었으면 버린다)
 * @returns {Promise<void>}
 */
async function fillCounts(seq) {
  if (!wl.on.journal) return;
  const codes = [...new Set(wl.items.map(i => i.corp_code).filter(Boolean))].filter(c => !wl.counts.has(c));
  if (!codes.length) return;
  const tasks = codes.flatMap(c => [[c, false], [c, true]]);
  const totals = await mapLimit(tasks, COUNT_CONCURRENCY, async ([code, due]) => {
    const q = new URLSearchParams({ corp_code: code });
    if (due) q.set("due", "true");
    q.set("limit", "1");
    const { status, data } = await call(`/api/journal?${q}`);
    return status === 200 && Number.isInteger(data?.total) ? data.total : null;
  });
  if (seq !== wl.seq) return;
  codes.forEach((code, k) => {
    const total = totals[2 * k], due = totals[2 * k + 1];
    wl.counts.set(code, total === null || due === null ? null : { total, due });
  });
  render();
}

// ── 동작 ──────────────────────────────────────────────────────────

/**
 * 메모가 있으면 확인을 받는다(결정 7-3).
 * @param {object} it 지울 항목
 * @returns {boolean} 지워도 되면 true
 */
function confirmRemove(it) {
  return !it.note || window.confirm(MSG.noteConfirm);
}

/**
 * 지금 고른 종목을 관심종목에 더한다. 409 duplicate는 조용히 ★로, 409 limit은 토스트.
 * @returns {Promise<void>}
 */
async function addCurrent() {
  const cur = wl.current;
  const body = { symbol: cur.symbol, name: cur.name || cur.symbol };
  if (cur.exchange) body.exchange = cur.exchange;
  const { status, data } = await call("/api/watchlist", { method: "POST", body });
  if (status === 201 && data?.id) {
    wl.items = [...wl.items.filter(i => i.id !== data.id), data];
    render();
    await fillCounts(wl.seq);
  } else if (status === 409 && data?.code === "duplicate") {
    await reloadQuietly();
  } else if (status === 409 && data?.code === "limit") {
    setToast(MSG.limit, "error");
  } else if (status === 401) {
    setState("unauthorized");
  } else {
    setToast(MSG.addFail, "error");
  }
}

/**
 * 항목 하나를 지운다. 이미 없으면(404) 지운 것으로 본다.
 * @param {object} it 지울 항목
 * @returns {Promise<void>}
 */
async function removeItem(it) {
  const { status } = await call(`/api/watchlist/${encodeURIComponent(it.id)}`, { method: "DELETE" });
  if (status === 204 || status === 404) {
    wl.items = wl.items.filter(i => i.id !== it.id);
    if (wl.editing?.id === it.id) wl.editing = null;
    render();
  } else if (status === 401) {
    setState("unauthorized");
  } else {
    setToast(MSG.removeFail, "error");
  }
}

/**
 * 별 버튼: ☆면 더하고 ★면 지운다. 요청 중에는 버튼을 비활성화하고, 실패하면 원래 상태로 그린다.
 * @returns {Promise<void>}
 */
async function toggleStar() {
  if (wl.busy || !wl.current || wl.state !== "list") return;
  const hit = findItem(wl.current.symbol);
  if (hit && !confirmRemove(hit)) return;
  wl.busy = true;
  syncStar();
  try {
    if (hit) await removeItem(hit); else await addCurrent();
  } finally {
    wl.busy = false;
    syncStar();
  }
}

/**
 * 메모 저장(PATCH, 메모만). 실패하면 입력을 그대로 두고 토스트.
 * @param {object} it 항목
 * @returns {Promise<void>}
 */
async function saveNote(it) {
  const draft = wl.editing?.draft ?? "";
  if (Array.from(draft).length > NOTE_MAX) return setToast(MSG.noteTooLong, "error");
  const { status, data } = await call(`/api/watchlist/${encodeURIComponent(it.id)}`,
                                      { method: "PATCH", body: { note: draft } });
  if (status === 200 && data?.id) {
    wl.items = wl.items.map(i => (i.id === data.id ? data : i));
    wl.editing = null;
    render();
  } else if (status === 401) {
    setState("unauthorized");
  } else {
    setToast(status === 422 ? MSG.noteTooLong : MSG.noteFail, "error");
  }
}

/**
 * (b) 근거 모드로 질문: 채팅으로 가서 회사만 고른다. 질문 칸은 비우고 보내지 않는다. 메모는 넣지 않는다.
 * @param {object} it 항목(corp_code 있음)
 * @returns {Promise<void>}
 */
async function askEvidence(it) {
  navigate("agent-chat");
  const ok = await prefillEvidenceChat({ corp_name: it.name || it.symbol, corp_code: it.corp_code }, "");
  if (!ok) setToast(MSG.evidenceOff, "error");
}

/**
 * 줄 버튼 클릭(이벤트 위임).
 * @param {MouseEvent} e 클릭
 * @returns {void}
 */
function onListClick(e) {
  const b = e.target.closest("button");
  const li = e.target.closest(".wl-row");
  if (!b || !li) return;
  const it = wl.items.find(i => i.id === li.dataset.id);
  if (!it) return;
  if (b.classList.contains("wl-view")) {
    addAndSelectCompany(it.symbol, it.name || it.symbol);
  } else if (b.classList.contains("wl-ask")) {
    askEvidence(it);
  } else if (b.classList.contains("wl-journal")) {
    openJournalForCompany(it.corp_code, it.name || it.symbol);
  } else if (b.classList.contains("wl-note-edit")) {
    wl.editing = wl.editing?.id === it.id ? null : { id: it.id, draft: it.note ?? "" };
    render();
    $("wl-list").querySelector(`.wl-row[data-id="${CSS.escape(it.id)}"] .wl-note-input`)?.focus();
  } else if (b.classList.contains("wl-note-cancel")) {
    wl.editing = null;
    render();
  } else if (b.classList.contains("wl-note-save")) {
    b.disabled = true;
    saveNote(it).finally(() => { b.disabled = false; });
  } else if (b.classList.contains("wl-delete")) {
    if (!confirmRemove(it)) return;
    b.disabled = true;
    removeItem(it).finally(() => { b.disabled = false; });
  }
}

/**
 * 버튼 연결(한 번).
 * @returns {void}
 */
function wire() {
  if (wl.wired) return;
  wl.wired = true;
  $("wl-list").addEventListener("click", onListClick);
  $("wl-star").addEventListener("click", toggleStar);
  $("wl-retry").addEventListener("click", () => loadWatchlistPanel());
}

// 지표 화면에서 종목을 고를 때마다 별 버튼을 맞춘다
onCompanySelected(cur => {
  wl.current = cur;
  syncStar();
});
