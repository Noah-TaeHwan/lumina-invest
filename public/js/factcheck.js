// public/js/factcheck.js
// 공시 팩트체커 화면(설계 T3, 3단계 결과 표시 — LLM 없이 코드로).
// - 회사 선택·출처 선택·붙여넣기 → POST /api/factcheck → GET /api/factcheck/{job_id} 폴링(문장별로 끝나는 대로 표시).
// - 결과: 상단 개수 요약, ⚠️·❔를 먼저, 펼치면 근거 문단·보고서명·기간·XBRL 값. 건너뛴 문장은 접어서 이유 + 수동 검수 버튼.
// - 서버·사용자 문자열은 모두 textContent로만 넣는다(innerHTML 금지).

const MAX_CHARS = 2000;
const BADGE = {
  supported: { icon: '✅', label: '검색된 공시 문단과 일치' },
  contradicted: { icon: '⚠️', label: '공시와 어긋날 수 있음 — 직접 확인 필요' },
  no_evidence: { icon: '❔', label: '검색 범위 안에서 못 찾음' },
  unjudged: { icon: '⊘', label: '판정 불가(오류·한도·혼잡)' },
  skipped: { icon: '·', label: '검수 안 함' },
};
// ⚠️·❔를 먼저 보인다
const ORDER = { contradicted: 0, no_evidence: 1, unjudged: 2, supported: 3 };
const CATEGORY = {
  opinion: '의견·전망',
  out_of_scope: '공시 범위 밖',
  other_company: '다른 회사가 주어',
  derived: '파생 지표(범위 밖)',
};
const FS_DIV = { CFS: '연결', OFS: '별도' };
// 서버 사유 코드 → 한국어(쉼표로 이어진 여러 코드, ':' 뒤 세부값). 모르는 코드는 원문 그대로 보인다
const REASONS = {
  xbrl_partial: '숫자는 XBRL과 일치, 나머지는 공시에서 못 찾음',
  xbrl_mismatch: 'XBRL 재무 수치와 다름',
  period_ambiguous: '기간이 분명하지 않아 판정하지 않음',
  no_passages: '검색 범위에서 관련 공시 문단을 찾지 못함',
  busy: '요청이 몰려 판정하지 못함(잠시 뒤 다시)',
  timeout: '판정이 시간 안에 끝나지 않음',
  request_too_large: '문장·문단이 너무 길어 판정하지 않음',
  budget: '검수 한도 안에서 판정하지 못함',
  unmetered: '판정 설정 오류',
  no_api_key: '판정 API 설정 오류',
  no_fact_marker: '숫자·회사명·기간이 없어 검수 대상 아님',
  opinion: '의견·전망',
  market: '주가·시장 전망(공시 범위 밖)',
  forecast: '추정·목표치(공시 범위 밖)',
  future_period: '아직 공시되지 않은 기간',
  not_claim: '사실 주장이 아님',
  other_company: '다른 회사가 주어',
  derived: '파생 지표',
  rule: '규칙으로 검수 대상',
  jev: '판정 모델 분류',
  jev_failed: '분류 실패로 검수',
  triage_error: '분류 실패로 검수',
  forced: '직접 검수 요청',
  restated: '재작성된 비교값과 일치',
  separate_only: '연결과 다르고 별도 재무제표 값과 일치',
  no_period: 'XBRL에 그 기간 값 없음',
  no_fact: 'XBRL에 그 계정 값 없음',
  candidate_missing: 'XBRL 후보 값 일부 없음',
  http_5xx: '판정 서버 오류',
  http_429: '판정 서버 요청 한도',
  http_4xx: '판정 요청 오류',
  invalid_response: '판정 응답 오류',
  error: '판정 중 오류',
};
const REASON_DETAIL = {
  'not_claim:question': '질문 문장',
  'not_claim:phrase': '자료 언급·답변 불가 표현',
  'not_claim:lead': '목록 머리말',
  'not_claim:short': '짧은 문장',
  'derived:growth': '증감률(잠정실적으로 대조)',
  'derived:growth_unsupported': '증감률(대조할 수 없는 형태)',
  'derived:margin': '영업이익률(XBRL로 계산해 대조)',
  'derived:other': '그 밖의 파생 지표(범위 밖)',
  'rule:number': '숫자', 'rule:company': '회사명', 'rule:period': '기간',
  'jev:fact': '사실', 'jev:uncertain': '애매해서 검수',
};

/** 표에 그 키가 직접 있을 때만 값(객체가 물려받은 __proto__·constructor·toString 같은 이름에 걸리지 않게). */
function own(table, key) {
  return Object.prototype.hasOwnProperty.call(table, key) ? table[key] : undefined;
}

/** 사유 코드 하나를 한국어로: 정확히 맞는 표 → 'X:세부' → 앞부분 표 + (세부) → 원문. */
function reasonLabel(code) {
  const c = String(code).trim();
  if (!c) return '';
  if (own(REASONS, c)) return own(REASONS, c);
  const i = c.indexOf(':');
  if (i > 0) {
    const head = c.slice(0, i);
    const tail = c.slice(i + 1);
    if (own(REASON_DETAIL, c)) return `${own(REASONS, head) || head} — ${own(REASON_DETAIL, c)}`;
    if (head === 'other_company') return `다른 회사(${tail})가 주어`;
    if (own(REASONS, head)) return `${own(REASONS, head)}(${tail})`;
  }
  return c;
}

/** 쉼표로 이어진 사유 코드를 한국어로 이어 붙인다. 없으면 빈 문자열. */
function reasonText(reason) {
  if (reason === null || reason === undefined || reason === '') return '';
  return String(reason).split(',').map(reasonLabel).filter(Boolean).join(' · ');
}

/** XBRL 값: 단위가 %면 퍼센트(영업이익률), 아니면 원. */
function xbrlAmount(x) {
  if (x.unit === '%') {
    const n = Number(x.amount);
    return Number.isFinite(n) ? `${n.toLocaleString('ko-KR', { maximumFractionDigits: 2 })}%` : `${x.amount}%`;
  }
  return won(x.amount);
}

const $ = (id) => document.getElementById(id);

/** 태그와 글자로 요소를 만든다(textContent만 쓴다). */
function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

/** 숫자를 원 단위 천 단위 쉼표로. 숫자가 아니면 그대로. */
function won(amount) {
  const n = Number(amount);
  return Number.isFinite(n) ? `${n.toLocaleString('ko-KR')}원` : String(amount ?? '');
}

/** 붙여넣기 칸 글자 수 표시(공백 앞뒤 제외, 서버와 같은 기준). */
function updateCount() {
  const n = $('fc-text').value.trim().length;
  const node = $('fc-count');
  node.textContent = `${n.toLocaleString('ko-KR')} / ${MAX_CHARS.toLocaleString('ko-KR')}자`;
  node.classList.toggle('over', n > MAX_CHARS);
}

/** 입력 칸 아래 안내(오류)를 보이거나 지운다. */
function showError(message) {
  const node = $('fc-error');
  node.textContent = message || '';
  node.classList.toggle('hidden', !message);
}

/** 결과 영역의 job 오류 안내. */
function showJobError(message) {
  const node = $('fc-job-error');
  node.textContent = message || '';
  node.classList.toggle('hidden', !message);
}

/** 응답 본문에서 안내 문구를 꺼낸다(detail.message → detail 문자열 → 기본 문구). */
async function messageOf(res, fallback) {
  try {
    const data = await res.json();
    const d = data && data.detail;
    if (d && typeof d === 'object' && d.message) return String(d.message);
    if (typeof d === 'string') return d;
  } catch (_) { /* 본문이 JSON이 아니다 */ }
  return fallback;
}

/** 근거 문단 하나: 보고서명·기간·절·접수번호 + 문단. */
function evidenceNode(ev) {
  const box = el('div', null, 'ev');
  const meta = [ev.report_nm, ev.period && `기간 ${ev.period}`, ev.section, ev.rcept_no && `접수번호 ${ev.rcept_no}`]
    .filter(Boolean).join(' · ');
  box.appendChild(el('div', meta, 'meta'));
  box.appendChild(el('div', ev.text || ''));
  return box;
}

/** 확인한 문장 하나(펼치면 근거). */
function resultNode(r) {
  const b = own(BADGE, r.status) || BADGE.unjudged;
  const li = el('li', null, r.status);
  li.dataset.idx = r.idx;
  li.dataset.status = r.status;
  const det = el('details');
  const sum = el('summary');
  sum.appendChild(el('span', b.icon, 'badge'));
  const sent = el('span', null, 'sent');
  sent.appendChild(el('span', r.text));
  sent.appendChild(el('span', b.label, 'label'));
  sum.appendChild(sent);
  det.appendChild(sum);
  const body = el('div', null, 'body');
  const why = reasonText(r.reason);
  if (why) body.appendChild(el('p', why, 'muted reason'));
  if (r.xbrl) {
    const x = r.xbrl;
    const parts = [x.account_nm, x.period && `기간 ${x.period}`, own(FS_DIV, x.fs_div) || x.fs_div, xbrlAmount(x)].filter(Boolean);
    body.appendChild(el('div', `XBRL 재무 수치: ${parts.join(' · ')}`, 'xbrl'));
    const note = reasonText(x.note);
    if (note) body.appendChild(el('div', `비고: ${note}`, 'xbrl muted xbrl-note'));
  }
  const evs = Array.isArray(r.evidence) ? r.evidence : [];
  evs.forEach((ev) => body.appendChild(evidenceNode(ev || {})));
  if (!why && !r.xbrl && !evs.length) body.appendChild(el('p', '표시할 근거 문단이 없습니다.', 'muted'));
  det.appendChild(body);
  li.appendChild(det);
  return li;
}

/** 건너뛴 문장 하나: 이유 + 수동 검수 버튼. */
function skippedNode(r, state) {
  const li = el('li', null, 'skipped');
  li.dataset.idx = r.idx;
  const wrap = el('div', null, 'body');
  wrap.style.paddingTop = '10px';
  wrap.appendChild(el('div', r.text, 'sent'));
  const why = [own(CATEGORY, r.category), reasonText(r.reason)].filter(Boolean).join(' — ') || '검수 대상이 아니라고 분류됨';
  wrap.appendChild(el('span', `이유: ${why}`, 'label'));
  const btn = el('button', '이 문장 검수 요청', 'secondary fc-recheck');
  btn.type = 'button';
  btn.disabled = state.running || state.rechecked.has(r.idx);
  btn.addEventListener('click', () => recheck(state, r.idx, btn));
  const row = el('div', null, 'row');
  row.appendChild(btn);
  wrap.appendChild(row);
  li.appendChild(wrap);
  return li;
}

/** job 응답으로 결과 영역을 다시 그린다. */
function render(state, job) {
  $('fc-result').classList.remove('hidden');
  const results = Array.isArray(job.results) ? job.results : [];
  const c = job.counts || {};
  $('fc-summary').textContent =
    `✅ ${c.supported || 0} · ⚠️ ${c.contradicted || 0} · ❔ ${c.no_evidence || 0} · ⊘ ${c.unjudged || 0} · 건너뜀 ${c.skipped || 0}`;
  const running = job.status === 'running';
  $('fc-progress').textContent = running
    ? `검수 중… ${results.length} / ${job.total ?? '?'}문장`
    : `${job.status === 'done' ? '완료' : '멈춤'} · ${results.length} / ${job.total ?? results.length}문장`;
  showJobError(job.error && job.error.message);

  const checked = results.filter((r) => r.status !== 'skipped')
    .sort((a, b) => ((own(ORDER, a.status) ?? 9) - (own(ORDER, b.status) ?? 9)) || (a.idx - b.idx));
  const list = $('fc-list');
  // 폴링으로 다시 그려도 사용자가 펼친 문장은 펼친 채로 둔다
  const open = new Set([...list.querySelectorAll('li')].filter((li) => li.querySelector('details')?.open)
    .map((li) => li.dataset.idx));
  list.replaceChildren(...checked.map(resultNode));
  list.querySelectorAll('li').forEach((li) => {
    if (open.has(li.dataset.idx)) li.querySelector('details').open = true;
  });

  const skipped = results.filter((r) => r.status === 'skipped');
  const box = $('fc-skipped');
  box.classList.toggle('hidden', !skipped.length);
  $('fc-skipped-title').textContent = `건너뛴 문장 ${skipped.length}개`;
  $('fc-skipped-list').replaceChildren(...skipped.map((r) => skippedNode(r, state)));
}

/** 끝날 때까지 폴링한다(세대 번호가 바뀌면 멈춘다). */
async function poll(state, gen) {
  while (gen === state.gen) {
    let res;
    try {
      res = await fetch(`/api/factcheck/${encodeURIComponent(state.jobId)}`, { credentials: 'same-origin' });
    } catch (_) {
      if (gen !== state.gen) return;
      showJobError('서버에 연결하지 못했습니다. 잠시 뒤 다시 확인합니다.');
      await new Promise((r) => setTimeout(r, 2000));
      continue;
    }
    if (gen !== state.gen) return;
    if (!res.ok) {
      showJobError(await messageOf(res, '검수 결과를 불러오지 못했습니다.'));
      return finish(state);
    }
    const job = await res.json();
    state.running = job.status === 'running';
    render(state, job);
    if (!state.running) return finish(state);
    await new Promise((r) => setTimeout(r, job.poll_interval_ms || 1000));
  }
}

/** 실행이 끝났을 때 입력을 다시 연다. */
function finish(state) {
  state.running = false;
  $('fc-submit').disabled = false;
  document.querySelectorAll('.fc-recheck').forEach((b) => {
    b.disabled = state.rechecked.has(Number(b.closest('li').dataset.idx));
  });
}

/** 검수 시작. */
async function submit(state, ev) {
  ev.preventDefault();
  showError('');
  const text = $('fc-text').value.trim();
  if (!text) return showError('검수할 글을 붙여 넣어 주세요.');
  if (text.length > MAX_CHARS) {
    return showError(`익명 검수는 한 번에 ${MAX_CHARS.toLocaleString('ko-KR')}자까지입니다. ` +
      `지금 ${text.length.toLocaleString('ko-KR')}자입니다. 나눠서 붙여 넣어 주세요.`);
  }
  const source = (document.querySelector('input[name="source"]:checked') || {}).value;
  const payload = { corp_code: $('fc-company').value, source, text };
  $('fc-submit').disabled = true;
  let res;
  try {
    res = await fetch('/api/factcheck', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    });
  } catch (_) {
    $('fc-submit').disabled = false;
    return showError('서버에 연결하지 못했습니다. 잠시 뒤 다시 시도해 주세요.');
  }
  if (res.status !== 202) {
    $('fc-submit').disabled = false;
    return showError(await messageOf(res, '검수를 시작하지 못했습니다.'));
  }
  const started = await res.json();
  state.gen += 1;
  state.jobId = started.job_id;
  state.running = true;
  state.rechecked = new Set();
  render(state, { status: 'running', total: started.total, results: [], counts: {} });
  poll(state, state.gen);
}

/** 건너뛴 문장 하나 수동 검수 요청. */
async function recheck(state, idx, btn) {
  if (state.running) return;
  btn.disabled = true;
  showJobError('');
  let res;
  try {
    res = await fetch(`/api/factcheck/${encodeURIComponent(state.jobId)}/recheck/${idx}`,
      // 서버는 교차 출처 요청을 막으려고 JSON Content-Type만 받는다
      { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: '{}' });
  } catch (_) {
    btn.disabled = false;
    return showJobError('서버에 연결하지 못했습니다. 잠시 뒤 다시 시도해 주세요.');
  }
  if (res.status !== 202) {
    if (res.status === 409) state.rechecked.add(idx); else btn.disabled = false;
    return showJobError(await messageOf(res, '검수를 요청하지 못했습니다.'));
  }
  state.rechecked.add(idx);
  state.running = true;
  $('fc-submit').disabled = true;
  state.gen += 1;
  poll(state, state.gen);
}

/** 회사 목록·상시 문구를 서버 값으로 맞춘다(실패하면 HTML 기본값을 그대로 둔다). */
async function loadCompanies() {
  try {
    const res = await fetch('/api/factcheck/companies', { credentials: 'same-origin' });
    if (!res.ok) return;
    const data = await res.json();
    if (Array.isArray(data.companies) && data.companies.length) {
      const sel = $('fc-company');
      const keep = sel.value;
      sel.replaceChildren(...data.companies.map((c) => {
        const o = el('option', c.corp_name);
        o.value = c.corp_code;
        return o;
      }));
      if ([...sel.options].some((o) => o.value === keep)) sel.value = keep;
    }
    if (data.scope) $('fc-scope').textContent = data.scope;
    if (Array.isArray(data.weaknesses) && data.weaknesses.length) {
      $('fc-weak').replaceChildren(...data.weaknesses.map((w) => el('li', w)));
    }
  } catch (_) { /* 기본 문구 유지 */ }
}

/** 화면 시작. */
function init() {
  const state = { gen: 0, jobId: null, running: false, rechecked: new Set() };
  $('fc-text').addEventListener('input', updateCount);
  $('fc-form').addEventListener('submit', (ev) => submit(state, ev));
  updateCount();
  loadCompanies();
  document.body.dataset.fcReady = '1';
}

init();
