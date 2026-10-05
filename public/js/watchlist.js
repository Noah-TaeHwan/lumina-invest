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
const DEFAULT_LIMIT = 100;        // 서버 응답에 limit이 없을 때만 쓰는 값(서버 MAX_ITEMS)
const NOTE_MAX = 200;             // 서버 메모 상한과 같은 값
const COUNT_CONCURRENCY = 4;      // (c) 개수 요청 동시 실행 상한
const MSG = {
  loading: "관심종목을 불러오는 중…",
  empty: "관심종목이 없습니다. 종목을 검색해 ☆를 누르면 여기에 모입니다.",
  failed: "관심종목을 불러오지 못했습니다",
  limit: n => `관심종목은 ${n}개까지 담을 수 있습니다`,
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
 * 동시 실행 상한을 나눠 쓰는 실행기를 만든다. 같은 실행기로 넣은 작업은 어느 호출에서 왔든 합쳐서 limit개까지만 돈다.
 * @param {number} limit 동시 실행 상한(1 이상)
 * @returns {<R>(fn: () => Promise<R>) => Promise<R>} 자리가 나면 fn을 돌리고 그 결과(또는 실패)를 돌려주는 함수
 */
export function createLimiter(limit) {
  const max = Math.max(1, limit);
  const waiting = [];
  let running = 0;
  const pump = () => {
    while (running < max && waiting.length) {
      const { fn, resolve, reject } = waiting.shift();
      running++;
      Promise.resolve().then(fn).then(resolve, reject).finally(() => { running--; pump(); });
    }
  };
  return fn => new Promise((resolve, reject) => { waiting.push({ fn, resolve, reject }); pump(); });
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
const countSlot = createLimiter(COUNT_CONCURRENCY);  // 개수 요청은 fillCounts 호출이 겹쳐도 합쳐서 상한
const wl = {
  state: "idle",        // idle | loading | list | error | unauthorized
  items: [], seq: 0,
  limit: DEFAULT_LIMIT, // 서버가 알려 준 상한(목록 응답 limit)
  on: { evidence: false, journal: false },
  counts: new Map(),    // corp_code → {total, due} | null(실패). 없으면 조회 중
  pending: new Set(),   // 개수를 묻는 중인 corp_code(이번 세대). 호출 사이 중복 요청을 막는다
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
    count.textContent = `${wl.items.length} / ${wl.limit}`;
    renderRows(list);
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
 * 줄들을 다시 그린다. 메모를 편집 중인 줄은 편집 칸 요소를 DOM에서 떼지 않고 나머지만 바꾼다
 * (떼면 포커스·커서·한글 입력 조합이 끊긴다).
 * @param {HTMLElement} list 목록 요소
 * @returns {void}
 */
function renderRows(list) {
  const id = wl.editing?.id;
  const keep = id ? [...list.children].find(li => li.dataset.id === id && li.querySelector(".wl-note-form")) : null;
  const at = keep ? wl.items.findIndex(i => i.id === id) : -1;
  if (at < 0) {
    list.replaceChildren(...wl.items.map(rowEl));
    return;
  }
  const it = wl.items[at];
  const form = keep.querySelector(".wl-note-form");
  for (const c of [...keep.children]) if (c !== form) c.remove();
  form.before(...rowParts(it));
  keep.dataset.symbol = it.symbol;
  for (const c of [...list.children]) if (c !== keep) c.remove();
  keep.before(...wl.items.slice(0, at).map(rowEl));
  keep.after(...wl.items.slice(at + 1).map(rowEl));
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
  li.append(...rowParts(it));
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
 * 줄의 편집 칸 밖 부분: 이름·심볼·시장·메모 글자와 연결 버튼.
 * @param {object} it 관심종목 항목
 * @returns {HTMLElement[]} [본문, 버튼 묶음]
 */
function rowParts(it) {
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
  return [main, links];
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
  wl.limit = limitOf(data);
  wl.counts = new Map();
  wl.pending = new Set();
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
 * 응답의 상한(limit). 없거나 이상하면 기본값.
 * @param {any} data 목록 응답 또는 409 limit 응답
 * @returns {number} 상한
 */
function limitOf(data) {
  return Number.isInteger(data?.limit) && data.limit > 0 ? data.limit : wl.limit || DEFAULT_LIMIT;
}

/**
 * 조용히 목록만 다시 받는다(이미 있음 409 뒤). 실패하면 지금 목록을 그대로 둔다.
 * @returns {Promise<boolean>} 다시 받았으면 true(401이면 unauthorized로 바꾸고 false)
 */
async function reloadQuietly() {
  const { status, data } = await call("/api/watchlist");
  if (status === 401) {
    setState("unauthorized");
    return false;
  }
  if (status !== 200 || !isListShape(data)) return false;
  wl.items = data.items;
  wl.limit = limitOf(data);
  render();
  await fillCounts(wl.seq);
  return true;
}

/**
 * 아직 개수를 모르고 묻는 중도 아닌 corp_code마다 기록 수(total)와 다시 볼 때 된 수(due=true의 total)를 묻는다.
 * 같은 corp_code는 호출이 겹쳐도 한 번만(wl.pending), 동시 요청은 모든 호출을 합쳐 COUNT_CONCURRENCY개까지.
 * 일지가 꺼져 있으면 묻지 않는다. 하나라도 401이면 다른 경로처럼 로그인 안 됨으로 바꾼다.
 * @param {number} seq 패널 세대(그사이 다시 열었으면 버린다)
 * @returns {Promise<void>}
 */
async function fillCounts(seq) {
  if (!wl.on.journal) return;
  const pending = wl.pending;
  const codes = [...new Set(wl.items.map(i => i.corp_code).filter(Boolean))]
    .filter(c => !wl.counts.has(c) && !pending.has(c));
  if (!codes.length) return;
  codes.forEach(c => pending.add(c));
  let unauthorized = false;
  const ask = (code, due) => countSlot(async () => {
    if (seq !== wl.seq || unauthorized) return null;  // 기다리는 사이 다시 열었거나 로그인이 풀렸다
    const q = new URLSearchParams({ corp_code: code });
    if (due) q.set("due", "true");
    q.set("limit", "1");
    const { status, data } = await call(`/api/journal?${q}`);
    if (status === 401) unauthorized = true;
    return status === 200 && Number.isInteger(data?.total) ? data.total : null;
  });
  const results = await Promise.all(codes.map(c => Promise.all([ask(c, false), ask(c, true)])));
  codes.forEach(c => pending.delete(c));
  if (seq !== wl.seq) return;
  if (unauthorized) return setState("unauthorized");
  codes.forEach((code, k) => {
    const [total, due] = results[k];
    wl.counts.set(code, total === null || due === null ? null : { total, due });
  });
  if (wl.state === "list") render();
}

// ── 동작 ──────────────────────────────────────────────────────────

/**
 * 메모가 있거나 메모를 모르면(목록 재조회 실패로 item_id만 아는 줄) 확인을 받는다(결정 7-3).
 * @param {object} it 지울 항목
 * @returns {boolean} 지워도 되면 true
 */
function confirmRemove(it) {
  return (!it.note && !it.noteUnknown) || window.confirm(MSG.noteConfirm);
}

/**
 * 지금 고른 종목을 관심종목에 더한다. 409 duplicate는 조용히 ★로(목록 재조회가 실패해도 item_id로), 409 limit은
 * 서버가 알려 준 상한으로 토스트.
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
    const reloaded = await reloadQuietly();
    if (!reloaded && wl.state === "list" && typeof data.item_id === "string" && !findItem(cur.symbol)) {
      // 다시 받지 못해도 조용히 ★로(spec 3.2). 줄은 응답의 item_id로 만들고 메모는 모른다고 표시한다
      wl.items = [...wl.items, { id: data.item_id, symbol: norm(cur.symbol), name: cur.name || cur.symbol,
                                 corp_code: null, market: null, note: null, noteUnknown: true }];
      render();
    }
  } else if (status === 409 && data?.code === "limit") {
    wl.limit = limitOf(data);
    setToast(MSG.limit(wl.limit), "error");
    render();
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
