# AI 투자 리서치 경쟁·참고 제품 조사

- 날짜: 2026-10-06
- **작성: AI(Claude Code)가 공개 자료로 조사·작성했다. 사람 검증 전이다.**
- 목적: lumina-invest 서비스 기획(대상 사용자, 첫 사용 흐름, 메뉴, 차별점, 핵심 메시지)의 근거 자료.
- 판정에 쓰는 JEV는 TypeSafe의 모델이다. 이 프로젝트는 TypeSafe와 제휴 관계가 아니며, 이 문서에는 TypeSafe 가격·금액을 쓰지 않는다.
- 유료 가입·로그인·결제·데모 신청·연락은 하지 않았다. 공개 페이지와 검색 결과만 봤다.

## 0. 조사 방법과 한계 (먼저 읽을 것)

**원문 페이지를 직접 연 것은 한 건뿐이다.** 이 클라우드 세션의 네트워크 정책(egress 프록시)이 조사 대상 도메인을 모두 차단했다(`EGRESS_BLOCKED`/403). 차단된 곳은 terminal-x.ai, hebbia.com, alpha-sense.com, rogo.ai, bloomberg.com, perplexity.ai, fiscal.ai, deepsearch.com, arxiv.org, huggingface.co, techcrunch.com, hankyung.com, etnews.com, youtube.com, web.archive.org 등이다. 그래서 아래 사실은 거의 모두 **웹 검색 결과의 제목·스니펫·요약**에서 왔다.

- 검색 스니펫은 검색 도구가 요약한 문장이다. 따옴표 인용도 원문과 글자 그대로 같다고 보장하지 못한다.
- 붙인 URL은 해당 스니펫의 출처로 표시된 페이지다. 그 본문을 직접 열어 확인하지는 않았다. Hebbia·AlphaSense 항목의 URL 일부는 검색 결과 목록에서 그 내용이 들어 있을 가능성이 가장 높은 후보 페이지다.
- 직접 확인한 것은 GitHub 저장소 `dartpointai/dartpoint-mcp`의 메타데이터 한 건(GitHub API)뿐이다.

표기:

| 표기 | 뜻 |
|---|---|
| `[스니펫·회사]` | 2026-10-06 검색 스니펫에서 본 회사 자체 주장 |
| `[스니펫·제3자]` | 2026-10-06 검색 스니펫에서 본 언론·리뷰·투자사 등 제3자 서술(제3자가 회사 주장을 옮긴 경우 포함) |
| `[제목만]` | 검색 결과 제목만 봤다 |
| `[직접 확인]` | 2026-10-06 직접 조회했다 |
| **미확인** | 찾지 못했거나 출처끼리 엇갈려 판단할 수 없다. "없다"는 뜻이 아니다 |

머지 전 원문을 열어 확인할 항목은 [9절](#9-머지-전-원문-검증-목록)에 모았다.

## 1. 한눈에 보기

| 제품 | 주 대상 | 인용 단위(주장) | 근거 없을 때 | 불확실성 표시 | 정확도 주장 방법 공개 | 한국 공시(DART) | 가격 공개 |
|---|---|---|---|---|---|---|---|
| Terminal X | 기관, 증권사 통한 리테일 | 소스 표(문서 단위 클릭), "문장 단위 검증" 주장 | "환각 없음" 단정(회사) | 미확인 | 일부(표본 500, 7차원). 가중치·채점자 세부·데이터 미확인 | 미확인(미국 주식 특화라고 회사가 밝힘) | 비공개 |
| Hebbia | PE·헤지펀드·IB | 문장·칸(cell) | 미확인 | 미확인 | 기반 모델 비교만, 채점자 미확인 | 미확인 | 비공개 |
| AlphaSense | 기관·기업 | 문장·스니펫 | 미확인 | 미확인 | "인용 정확도 98%", 방법 미확인 | 미확인(삼성·KB증권 리서치, 연합뉴스는 회사 주장) | 비공개(무료 체험 있음) |
| Rogo | IB·PE | 답변 요소별 인라인, 스프레드시트 칸별 | "출처가 없으면 답하지 않는다"(회사) | 미확인 | 자체 벤치마크, 50문항만 공개, LLM 채점 | 미확인 | 비공개 |
| Bloomberg AI | 터미널 사용자 | 요약 항목 → 녹취 발췌 | 미확인 | 미확인 | 정확도 수치 없음 | 해당 없음 | 기능별 비공개 |
| Perplexity Finance | 개인 | 문장 옆 번호 [N] → URL | 미확인 | 일괄 고지 | 금융 제품 수치 없음(DRACO는 별개) | 미확인(한국 종목 페이지는 있음) | 공개 |
| Fiscal.ai | 개인·소규모 전문가 | 숫자 → 공시 PDF 페이지 | 미확인 | 미확인 | "91% vs 31%", 방법 미확인 | 미확인 | 공개(등급 구성은 출처끼리 엇갈림) |
| 딥서치 | 기관·개인 | 수치 → 원천 추적(주장) | "근거 미확인 내용은 생성하지 않음"(회사) | 미확인 | "환각 5% 미만", 방법 미확인 | 지원(회사 주장) | 앱 일부 공개 |
| DartPoint AI | 개인 투자자·분석가·취업준비생 | 답변 → 공시 문서 페이지 | 미확인 | 미확인 | 수치 없음 | 지원(DART 중심) | 무료(회사) |
| **lumina-invest** | 개인 투자자·리서치 입문자(가설) | **문장 → DART 문단, 문장마다 ✅⚠️❔** | ❔ 표시 | 문장별 배지 | 사전등록·원장, 결과 공개(관문 미통과도 공개) | 지원 | 해당 없음(포트폴리오) |

lumina-invest 행의 ✅는 "검색된 공시 문단 기준 AI 판정"이다. 사실을 보증하지 않는다. A-2 평가에서 ✅ 정밀도는 0.903이었지만 사전등록 관문(✅ 예측 150건 이상)에 못 미쳐 "확인됨"이 아니다([`docs/lab/evidence-a2-interpretation.md`](../lab/evidence-a2-interpretation.md)).

---

## 2. Terminal X (terminal-x.ai) — 중심 비교 대상

### 2.1 정의·대상 고객·가격

| 항목 | 내용 | 출처 |
|---|---|---|
| 정의 | "AI Agent Platform for Investment Managers". 사내 데이터와 외부 소스를 합쳐 "reduces research time by 80-90%" | [terminal-x.ai](https://www.terminal-x.ai/) `[스니펫·회사]` |
| 외부 소스 규모 | "100 million+"와 "100,000+"가 서로 다른 스니펫에 나온다. **미확인(출처끼리 엇갈림)** | [terminal-x.ai](https://www.terminal-x.ai/), [terminal-x.ai/company](https://www.terminal-x.ai/company) `[스니펫·회사]` |
| 법인·창업자 | 법인 Project Pluto, Inc.(국내 표기 플루토프로젝트). CEO 홍현(Hyun Hong), 뉴욕. 미국 IB M&A, 부동산 PE, 헤지펀드 경력 | [thevc.kr/plutoproject](https://thevc.kr/plutoproject), [한국경제 2026-09-04](https://www.hankyung.com/article/202609045313i) `[스니펫·제3자]` |
| 설립연도 | THE VC는 "2022년 8월 설립", 한국경제 2026-08-27 기사는 "2023년 뉴욕에서 설립". 브리프의 2022년과 한쪽만 일치한다. **미확인(출처끼리 엇갈림)** | [thevc.kr/plutoproject](https://thevc.kr/plutoproject), [한국경제 2026-08-27](https://www.hankyung.com/article/202608276874i) `[스니펫·제3자]` |
| 투자 | Series A(2026-04, DG Daiwa Ventures 참여, 금액 비공개). 기존 투자사로 두나무앤파트너스, 미래에셋벤처투자 등이 거론된다 | [FinTech Observer](https://www.fintechobserver.com/financial-ai-agent-terminal-x-sees-40x-growth-as-dg-daiwa-ventures-backs-series-a/), [thevc.kr](https://thevc.kr/plutoproject) `[스니펫·제3자]` |
| 성장 지표 | "8개월간 엔터프라이즈 사용량 약 40배, 쿼리 100만 건 이상" | [FinTech Observer](https://www.fintechobserver.com/financial-ai-agent-terminal-x-sees-40x-growth-as-dg-daiwa-ventures-backs-series-a/) `[스니펫·회사]` |
| 대상 고객 | 헤지펀드, PE, 자산운용, 웰스매니지먼트, 기업 재무팀. 2026년에는 대체투자 운용사 집중 | [terminal-x.ai/apis](https://www.terminal-x.ai/apis) `[스니펫·회사]`, [한국경제 2026-08-27](https://www.hankyung.com/article/202608276874i) `[스니펫·제3자]` |
| 리테일 경로 | 증권사 B2B2C. NH투자증권 MTS에서 NH 고객에게 "별도의 신청 및 비용 없이" 제공(2025-08-11 출시) | [파이낸셜뉴스](https://www.fnnews.com/news/202508111101184556), [시사저널](https://www.sisajournal.com/news/articleView.html?idxno=342497) `[스니펫·제3자]` |
| 가격 | **비공개.** 엔터프라이즈는 "유료 PoC → 본계약" 방식 | [FinTech Observer](https://www.fintechobserver.com/financial-ai-agent-terminal-x-sees-40x-growth-as-dg-daiwa-ventures-backs-series-a/) `[스니펫·회사]` |

> 주의: terminalxapp.com의 "Terminal X — One command. Every AI."(월 $5~49 요금제)는 **이름만 같은 다른 제품**이다([terminalxapp.com](https://terminalxapp.com/) `[스니펫·제3자]`). 이 가격을 Terminal X 가격으로 인용하지 않는다.

### 2.2 가입 후 첫 사용 흐름

출처는 모두 회사 사용자 가이드 [terminal-x.ai/blog/terminal-x-user-guide](https://www.terminal-x.ai/blog/terminal-x-user-guide) `[스니펫·회사]`다. 기관용 기준이며, YouTube 데모·Product Hunt·독립 리뷰는 찾지 못했거나 열 수 없었다(미확인).

| 단계 | 내용 |
|---|---|
| 온보딩 | 엔터프라이즈는 "white-glove support". 회사가 사내 드라이브·이메일·문서를 PDR(Private Data Room)에 연동하고 부서별 권한을 설정해 준다 |
| 첫 화면 | "Landing Page & Shortcuts". 단축키로 에이전트를 바꾼다 |
| 핵심 작업 | 기본은 Deep Research 모드. 에이전트의 추론 단계, 참조 소스, 쓴 분석 도구를 보여 준다. 가벼운 Lite Agent도 있다 |
| 결과물 | 대화 중 `/report`로 스레드를 Word 리포트로 바꾼다. PPT·Excel 출력 여부는 **미확인**(스니펫 문구가 Terminal X 설명인지 다른 제품 설명인지 불분명) |
| 재방문 이유 | "Performance Dashboard"(질문 수, 동기화 데이터, 워크플로, 절약 시간) |

리테일(NH MTS)에서는 답변을 본 뒤 MTS에서 간편 주문으로 이어진다([뉴스웰](https://www.newswell.co.kr/news/articleView.html?idxno=12270) `[스니펫·제3자]`).

### 2.3 근거·출처 표기

| 항목 | 내용 | 출처 |
|---|---|---|
| 인용 UI | 답변 오른쪽 "Source Table". 행을 클릭하면 원문 문서가 열린다. 답변에 직접 쓰인 소스는 ID 옆 파란 세로줄로 강조 | [user-guide](https://www.terminal-x.ai/blog/terminal-x-user-guide) `[스니펫·회사]` |
| 인용 단위 | 원문 연결은 문서 단위. "every fact verifiable at the sentence level"이라고 주장한다. 실제 문장 하이라이트 여부는 **미확인** | 같은 곳 `[스니펫·회사]` |
| 환각 대응 | "indexing accuracy is code-based and independent of the LLM, there are no hallucination or reliability issues" — 절대적 단정이며 검증 자료는 찾지 못했다 | 같은 곳 `[스니펫·회사]` |
| 불확실성 표시 | **미확인** | — |
| 검색 정확도 | 검색 F1 0.68 → 0.91, 2천만+ 청크. 측정 방법 비공개 | [Pinecone 고객 사례](https://www.pinecone.io/customers/terminal-x/) `[스니펫·제3자]`(벤더 사례, 수치는 회사 데이터) |

### 2.4 정확도·벤치마크 주장: "Claude 70.6%·GPT 63.3%"

출처: 회사 블로그 "Terminal X AI Outperforms Claude, GPT, Gemini on Finance" — [terminal-x.ai/blog/terminal-x-outperforms-claude-gpt-and-gemini-on-retail-investment-queries](https://www.terminal-x.ai/blog/terminal-x-outperforms-claude-gpt-and-gemini-on-retail-investment-queries) `[스니펫·회사]`

| 항목 | 내용 |
|---|---|
| 결과 | "Terminal X delivered 89.9% composite accuracy, outpacing Claude (70.6%), ChatGPT (63.3%), and Gemini (52.7%)" |
| 세부 | Temporal relevance: Terminal X 99.8%, Claude 72.4%, ChatGPT 46.6%, Gemini 43.8%. Ticker identification: 88.4%, ChatGPT 79.8%, Claude 79.6% |
| 표본 | "500 natural language financial queries from real retail investors". 의도 유형은 사실 요청·설명·전망·비교·계산·매수/매도 의향 |
| 비교 조건 | ChatGPT 5.0, Claude 4.5 Sonnet, Gemini 2.5 Pro를 "via their web interfaces with default settings"로 질의 |
| 채점자 | "a team of independent human evaluators". 인원·소속·블라인드 여부·평가자 간 일치도 **미확인** |
| 채점 차원 | Ticker Identification, Subject Identification, Query Intent, Source Authority, Temporal Relevance, Actionability, Contextual Awareness의 7개. composite 가중치 **미확인** |
| Source Authority | "Tier 1" = SEC 공시, 실적 콜 녹취, 대형 IB 리서치, Bloomberg, FactSet. Tier 1 인용 비율이 Terminal X 49%, ChatGPT 5.8%, Claude 0.4%로 나온다(스니펫 기준) |
| 데이터셋 공개 | **미확인** |
| 게시일 | **미확인** |

이 숫자를 읽을 때의 한계(위 사실에서 바로 나오는 것만):

1. 벤더가 자기 제품을 직접 평가했다.
2. "accuracy"는 사실 정답률이 아니라 7차원 루브릭의 합성 점수다.
3. Source Authority 차원은 Terminal X가 라이선스한 Tier 1 소스를 쓰면 점수가 오르는 구조다. 범용 챗봇은 같은 소스에 접근할 수 없다.
4. 비교 대상은 기본 설정의 웹 UI다. 검색·브라우징 설정과 모델 능력이 섞여 있다.
5. 질문 출처, 데이터셋, 채점 세부가 공개되지 않아 제3자가 재현할 수 없다.

회사는 별도로 "Terminal X Bench"(처리·검색·답변 생성 단계별 모델 스코어카드)를 소개한다. 구체 점수는 스니펫에 없었다([introducing-terminal-x-bench](https://www.terminal-x.ai/blog/introducing-terminal-x-bench) `[스니펫·회사]`).

### 2.5 데이터 원천과 한국 시장

| 항목 | 내용 | 출처 |
|---|---|---|
| 외부 데이터 | SEC 공시, 실적 콜 녹취, IR 자료, 브로커 리서치, 뉴스, 소셜미디어 | [terminal-x.ai](https://www.terminal-x.ai/), 벤치마크 블로그 `[스니펫·회사]` |
| 사내 데이터 | Excel·PDF·PPT·Word·이메일·CSV 인덱싱. 업로드 데이터를 영구 저장하거나 학습에 쓰지 않는다고 함 | [user-guide](https://www.terminal-x.ai/blog/terminal-x-user-guide) `[스니펫·회사]` |
| 한국 시장 | 회사 한국어 Substack: "Terminal X는 미국증시(US public equities)에 특화된 AI에이전트 서비스입니다". 한국 시장 질문이 많았다고 언급 | [terminalxkr.substack.com/p/2025](https://terminalxkr.substack.com/p/2025) `[스니펫·회사]` |
| NH 서비스 범위 | "미국 주식시장 투자와 관련한 질문"이 핵심 | [글로벌이코노믹](https://www.g-enews.com/article/Securities/2025/08/202508111214473619edf69f862c_1) `[스니펫·제3자]` |
| DART·KRX 연동 | **미확인** | — |
| 아시아 확장 | 일본을 첫 시장으로, 이후 한국·싱가포르 | [한국경제 2026-08-27](https://www.hankyung.com/article/202608276874i) `[스니펫·제3자]` |

### 2.6 우리와의 차이·배울 점

- **차이.** Terminal X는 미국 주식·기관 워크플로(사내 데이터 연동, IC 메모, 리포트)에 집중한다. 근거 표시는 소스 표 + 문서 클릭이다. 우리는 국내 상장사·DART에 집중하고, 근거를 문장 단위 판정으로 보여 준다.
- **배울 점.**
  - 리테일에 직접 팔지 않고 증권사 앱 안에 들어가는 유통 경로(NH MTS).
  - "답변에 실제로 쓰인 소스"를 따로 강조하는 표시. 우리 배지 옆에 "이 문장이 기댄 문단"을 같은 방식으로 보여 줄 수 있다.
  - 대화를 한 번에 문서로 만드는 출력(`/report`).
- **따라 하지 않을 점.** "환각이 없다"는 절대 단정. 그리고 방법을 공개하지 않은 단일 합성 점수.

---

## 3. 기관용 비교

### 3.1 Hebbia (Matrix)

| 틀 | 내용 | 출처 |
|---|---|---|
| 정의 | 표(spreadsheet-like) 인터페이스로 대량 문서에 LLM 에이전트를 돌리는 도구. 회사는 Matrix를 "AI associate", "multi-agent AI platform"이라 부른다 | [hebbia.com/blog/introducing-matrix-2-0](https://www.hebbia.com/blog/introducing-matrix-2-0) `[스니펫·회사]` |
| 대상 | PE, 헤지펀드, 주식 리서치, IB. 고객 운용자산(AUM)은 "$30 trillion"과 "$14 trillion"이 서로 다른 스니펫에 나온다(**미확인**) | [hebbia.com](https://www.hebbia.com/) `[스니펫·회사]` |
| 가격 | **비공개.** 셀프 가입·무료 체험 없음, 데모 요청 후 영업 경유라는 서술이 있다. 제3자 추정치는 인용하지 않는다 | [eesel.ai/blog/hebbia-ai](https://www.eesel.ai/blog/hebbia-ai) `[스니펫·제3자]` |
| 첫 사용 흐름 | 데모 요청 → 1:1 온보딩 → 파일 업로드·데이터 연결 → 질문을 **열 머리글**로 입력 → 에이전트가 각 칸을 "cited, reasoned answer"로 채움 → 칸마다 메모·덮어쓰기·플래그. 실제 첫 화면 UI는 **미확인** | [eesel.ai](https://www.eesel.ai/blog/hebbia-ai) `[스니펫·제3자]` |
| 결과물 | Matrix 2.0부터 "final models, memos, decks, and emails" | [introducing-matrix-2-0](https://www.hebbia.com/blog/introducing-matrix-2-0) `[스니펫·회사]` |
| 재방문 이유 | 고객사 딜 이력·내부 자료를 색인해 그 회사 방식의 워크플로를 만든다 | 같은 곳 `[스니펫·회사]` |
| 인용 단위 | "inline citations that highlight the precise sentence, cell, or data point". 제3자 리뷰도 문장 단위 인용을 언급 | [hebbia.com/resources/best-ai-for-document-analysis](https://www.hebbia.com/resources/best-ai-for-document-analysis) `[스니펫·회사]`, [neurons-lab.com](https://neurons-lab.com/articles/hebbia-vs-rogo/) `[스니펫·제3자]` |
| 환각 대응 | 자체 기법 Iterative Source Decomposition(ISD)으로 "sentence-level citations, full audit trails" | [hebbia.com/resources/rag-architecture](https://www.hebbia.com/resources/rag-architecture) `[스니펫·회사]` |
| 불확실성 표시 | **미확인** | — |
| 제3자 반론 | 인용 구절을 사람이 확인하지 않으면 최종 근거로 쓸 수 없다는 평가 | [neurons-lab.com](https://neurons-lab.com/articles/hebbia-vs-rogo/) `[스니펫·제3자]` |
| 정확도 주장 | (1) 2020년 자사 첫 RAG가 실제 질의의 84%에서 실패했다 — 자체 관찰, 표본·기준 **미확인**. (2) Financial Services Benchmark: 600+ 질문으로 OpenAI·Google·Anthropic 등 **기반 모델**을 비교 — Hebbia 제품의 정확도가 아니며 채점자·데이터셋 공개 **미확인** | [hebbia.com/blog/introducing-matrix-the-interface-to-agi](https://www.hebbia.com/blog/introducing-matrix-the-interface-to-agi), [hebbia.com/blog/which-model-will-give-me-the-edge](https://www.hebbia.com/blog/which-model-will-give-me-the-edge) `[스니펫·회사]` |
| 설문 | 금융 전문가 500+명 중 59%가 "AI 결과를 대부분 또는 전부 수동 재확인한다" — 자체 설문, 표본 추출 방법 **미확인** | [hebbia.com/resources/diligence-paradox-survey](https://www.hebbia.com/resources/diligence-paradox-survey) `[스니펫·회사]` |
| 데이터 원천 | SEC, S&P CapIQ, FactSet, PitchBook, Preqin, Fitch, Third Bridge·Guidepoint 전문가 인터뷰, 사내 Snowflake·SharePoint 등 | [hebbia.com/blog/every-data-integration-one-view](https://www.hebbia.com/blog/every-data-integration-one-view) `[스니펫·회사]` |
| 한국 | **미확인** | — |

**우리와의 차이·배울 점.**
- 표 형식(질문 = 열, 문서 = 행)으로 여러 공시에 같은 질문을 던지는 화면은 "여러 회사 같은 질문 비교"나 "같은 회사 연도별 비교"에 그대로 쓸 수 있다.
- 회사 자체 설문조차 "사람이 다시 확인한다"고 말한다. 그러니 정답률보다 **확인 시간 단축**(배지 클릭 → 원문 문단)을 가치로 내세우는 게 맞다.

### 3.2 AlphaSense (Generative Search · Deep Research · Tegus)

| 틀 | 내용 | 출처 |
|---|---|---|
| 정의 | 공개·유료·사내 문서 "500M+"건, "10,000+" 소스 위의 AI 검색·시장 인텔리전스 플랫폼. 다른 곳은 "300M+"이라고 해 엇갈림(**미확인**) | [alpha-sense.com/resources/product-articles/what-is-alphasense](https://www.alpha-sense.com/resources/product-articles/what-is-alphasense/) `[스니펫·회사]` |
| 대상 | 고객사 "6,500+", S&P 500의 약 70% | [alpha-sense.com/press/alphasense-surpasses-500m-in-arr](https://www.alpha-sense.com/press/alphasense-surpasses-500m-in-arr/) `[스니펫·회사]` |
| 가격 | **비공개(맞춤 견적).** 2주 무료 체험, 카드 불필요 | [alpha-sense.com/trial-request](https://www.alpha-sense.com/trial-request/) `[스니펫·회사]` |
| Tegus | 2024-06 인수 발표, $930M | [Axios Pro](https://www.axios.com/pro/fintech-deals/2024/06/11/alphasense-buys-private-market-rival-tegus-930m) `[스니펫·제3자]` |
| 첫 사용 흐름 | Help Center "Getting Started" 영상·온보딩 기초 → 템플릿 또는 직접 구성하는 **대시보드**, Company·Keyword 검색 → Generative Search(자연어 질문, 후속 질문)·Generative Grid(여러 문서에 같은 질문 세트)·Deep Research(보고서) → 인용 달린 요약·표, 공유·내보내기 | [help.alpha-sense.com Getting Started](https://help.alpha-sense.com/en/articles/5422498-getting-started-in-alphasense), [Leveraging Generative Grid](https://help.alpha-sense.com/hc/en-us/articles/41680141048979-Leveraging-Generative-Grid) `[스니펫·회사]` |
| 재방문 이유 | **Watchlist**(기업 목록 × 주제 검색 반복)와 **Alert**(실시간~주간 공시·녹취·뉴스 알림) | [Building Watchlists](https://help.alpha-sense.com/hc/en-us/articles/41814520504851-Building-Watchlists), [Monitoring Tools](https://help.alpha-sense.com/hc/en-us/articles/41815509396371-Maximizing-Your-Monitoring-Tools) `[스니펫·회사]` |
| 인용 단위 | "every claim traceable to the exact sentence", "citations to the exact snippet of text". Grid는 칸 클릭 → "View Citations" → 문서 안 근거 스니펫 | [alpha-sense.com/blog/product/combat-generative-ai-hallucination](https://www.alpha-sense.com/blog/product/combat-generative-ai-hallucination/) `[스니펫·회사]` |
| 환각 대응 | 검증된 콘텐츠 안에서만 생성하는 "source-constrained" RAG. 동시에 "All generative AI models are capable of hallucination"이라고 인정 | 같은 곳 `[스니펫·회사]` |
| 불확실성 표시 | **미확인** | — |
| 정확도 주장 | "98% of the time a result is cited correctly". 표본·채점자·데이터셋·시점 **미확인**. "인용이 맞다"와 "답이 맞다"는 다른 지표다. Forrester·Gartner Leader 선정은 애널리스트 평가로, 정확도 벤치마크가 아니다 | [alpha-sense.com/security/generative-ai-security](https://www.alpha-sense.com/security/generative-ai-security/) `[스니펫·회사]` |
| 데이터 원천 | SEC 공시, 실적 녹취, 뉴스 90,000+ 소스, 브로커 리서치 1,700+곳, 전문가 인터뷰 녹취(Tegus), 특허, 비상장사, 사내 문서 | [what-is-alphasense](https://www.alpha-sense.com/resources/product-articles/what-is-alphasense/) `[스니펫·회사]` |
| 한국 | "Samsung Securities and KB Securities" 리서치와 "Yonhap News Agency" 헤드라인 제공이라고 주장. 37개 언어 지원이라는데 한국어 포함 여부는 **미확인**. DART 직접 지원 **미확인** | [alpha-sense.com/solutions/emerging-markets-research](https://www.alpha-sense.com/solutions/emerging-markets-research/) `[스니펫·회사]` |

**우리와의 차이·배울 점.**
- 재방문 구조(관심종목 × 주제 × 알림)가 가장 잘 정리된 사례다. 우리 관심종목 + 판단 일지의 "다시 볼 날" + "공시 변화 비교"를 같은 축으로 묶을 근거가 된다.
- 칸 클릭 → 근거 스니펫이라는 2단계 확인 동선은 작은 제품도 그대로 만들 수 있다.

### 3.3 Rogo

| 틀 | 내용 | 출처 |
|---|---|---|
| 정의 | IB·PE·상장주식 투자자의 리서치와 딜 업무(비교기업 분석, CIM, 피치 자료)를 자동화하는 AI 플랫폼 | [Kleiner Perkins](https://www.kleinerperkins.com/perspectives/rogo-the-ai-platform-for-global-finance/) `[스니펫·제3자]`(투자사 글) |
| 대상 | Rothschild, Jefferies, Lazard, Moelis, Nomura 등. 사용자 수 "35,000+"·"50,000+"가 서로 다른 스니펫에 나옴 | [TipRanks](https://www.tipranks.com/news/private-companies/rogo-lands-160-million-series-d-to-accelerate-ai-agent-platform-in-global-finance) `[스니펫·제3자]` |
| 가격 | **비공개(견적제)** | [investables.ai](https://investables.ai/blog/rogo-ai-pricing) `[스니펫·제3자]` |
| 첫 사용 흐름 | 온보딩·첫 화면 **미확인**. 공개 자료로는 채팅형 리서치, Excel 애드인, 이메일로 일을 맡기는 Felix 에이전트가 확인된다 | [Kleiner Perkins](https://www.kleinerperkins.com/perspectives/rogo-the-ai-platform-for-global-finance/) `[스니펫·제3자]` |
| 결과물 | 회사 템플릿·서식에 맞춘 PPT·Excel·Word | 같은 곳 `[스니펫·회사]` |
| 재방문 이유 | 제품 장치로서는 **미확인**. 회사 템플릿·맞춤 에이전트로 업무가 묶이는 구조 | 같은 곳 `[스니펫·회사]` |
| 인용 단위 | "in-line citations for each aspect of the answer", "every cell in a Rogo-generated spreadsheet now cites its original source" | [rogo.ai/news/rogo-faqs…](https://rogo.ai/news/rogo-faqs-learn-more-about-how-were-disrupting-financial-research) `[스니펫·회사]` |
| 근거 없을 때 | "If Rogo can't find a source, it won't provide an answer." — 근거 없으면 답을 거절한다는 정책 | 같은 곳 `[스니펫·회사]` |
| 불확실성 표시 | **미확인** | — |
| 정확도 주장 | (1) FinanceBench 기준 "2.42x better than ChatGPT" — 표본·채점자·비교 버전 **미확인**. (2) 자체 Big Finance Bench: 928문항, 15,000+ 루브릭 기준, 10개 **기반 모델** 비교. 공개는 50문항 하위 집합뿐이고 공개 궤적은 LLM이 채점 | [rogo FAQ](https://rogo.ai/news/rogo-faqs-learn-more-about-how-were-disrupting-financial-research), [rogo.ai/news/introducing-the-big-finance-benchmark](https://rogo.ai/news/introducing-the-big-finance-benchmark), [huggingface.co/datasets/RogoAI/big-finance-benchmark](https://huggingface.co/datasets/RogoAI/big-finance-benchmark) `[스니펫·회사]` |
| 벤치마크 시사점 | 회사 스스로 루브릭 점수가 최종 정답 정확도보다 약 16%p 높다고 밝힌다. 부분 점수가 숫자를 얼마나 부풀리는지 보여 준다 | [introducing-the-big-finance-benchmark](https://rogo.ai/news/introducing-the-big-finance-benchmark) `[스니펫·회사]` |
| 데이터 원천 | SEC·해외 공시, 녹취, 뉴스, 비상장사 정보, 사내 데이터. PitchBook, S&P Capital IQ 연동 | [Rogo × S&P CapIQ](https://rogo.ai/news/rogo-integrates-s-p-capital-iq-data-into-its-ai-powered-workflows) `[스니펫·회사]` |
| 한국 | **미확인**(아시아 거점은 싱가포르) | [vibetrader.com](https://www.vibetrader.com/news/911cf32d-b5e7-4935-a058-74ab3c1da554) `[스니펫·제3자]` |

**우리와의 차이·배울 점.**
- "근거가 없으면 답하지 않는다"는 정책을 전면에 내세운다. 우리는 거절 대신 ❔로 **보여 준다.** 이 차이를 사용자에게 설명할 문구가 필요하다.
- 루브릭 점수와 최종 정답 정확도를 함께 공개한 점. 우리도 지표를 하나로 합치지 않고 나눠 보고한다.

### 3.4 Bloomberg 터미널의 AI 기능 (한 단락)

Bloomberg는 2024-01 Russell 1000 등의 **AI 실적 콜 요약**을 내놓았다. 요약 항목을 누르면 녹취의 해당 발췌로 이동한다([Bloomberg 보도자료](https://www.bloomberg.com/company/press/bloomberg-launches-ai-powered-earnings-call-summaries) `[스니펫·회사]`). 2025-04 **Document Insights**는 회사 문서에 자연어로 묻고 "transparency links"로 원문 발췌를 하이라이트한다([보도자료](https://www.bloomberg.com/company/press/bloomberg-accelerates-financial-analysis-with-gen-ai-document-insights/) `[스니펫·회사]`). 2026-02에는 에이전트형 **ASKB**(베타)를 소개했다([Bloomberg](https://www.bloomberg.com/company/stories/meet-askb-bloomberg-introduces-agentic-ai-to-the-bloomberg-terminal/) `[스니펫·회사]`). 전문가가 검토하는 뉴스 AI 요약에서도 2025년 초 "at least 36 errors"를 정정했다는 보도가 있다([Slashdot](https://news.slashdot.org/story/25/03/30/1946224/bloombergs-ai-generated-news-summaries-had-at-least-36-errors-since-january) `[스니펫·제3자]`). 공개된 정확도 수치는 찾지 못했다(**미확인**). BloombergGPT 논문은 50B 파라미터 금융 모델이다([arXiv:2303.17564](https://arxiv.org/abs/2303.17564) `[스니펫·제3자]`). 현재 터미널 기능이 어떤 모델을 쓰는지는 **미확인**이다.

**배울 점.** "요약 항목 → 원문 발췌" 연결은 업계 기본이 됐다. 사람이 검토해도 오류가 생기니, 정정 기록을 숨기지 않는 편이 신뢰에 낫다.

---

## 4. 개인 투자자용 비교

### 4.1 Perplexity Finance

| 틀 | 내용 | 출처 |
|---|---|---|
| 정의 | 범용 AI 답변 엔진 안의 금융 섹션(시세, 재무, 실적, 스크리너, 알림, 워치리스트) | [perplexity.ai/finance/earnings](https://www.perplexity.ai/finance/earnings) `[스니펫·회사]` |
| 가격 | Pro $20/월, Max $200/월. Free는 하루 Pro Search 3회 | [perplexity.ai/hub/pricing](https://www.perplexity.ai/hub/pricing), [요금제 도움말](https://www.perplexity.ai/help-center/en/articles/11187416-which-perplexity-subscription-plan-is-right-for-you) `[스니펫·회사]` |
| 첫 사용 흐름 | 가입 직후 온보딩 **미확인**. 금융 홈의 Earnings hub(실적 캘린더, 콜 중 실시간 요약) → 자연어 스크리너 → 종목 질문 | [changelog 7/18](https://www.perplexity.ai/changelog/what-we-shipped-july-18th), [changelog 7/25](https://www.perplexity.ai/en-GB/changelog/what-we-shipped-july-25th) `[스니펫·회사]` |
| 재방문 이유 | 가격 알림이 목표가에 닿으면 **사용자가 정해 둔 질의를 실행**해 이메일·푸시로 보냄. 워치리스트 AI 요약 | [changelog 7/18](https://www.perplexity.ai/changelog/what-we-shipped-july-18th) `[스니펫·회사]` |
| 인용 단위 | 답변 문장 옆 번호 [N] → 출처 URL(호버 미리보기, 클릭 시 원문). 문단 하이라이트 **미확인** | [How does Perplexity work](https://www.perplexity.ai/help-center/en/articles/10352895-how-does-perplexity-work) `[스니펫·회사]` |
| 불확실성·환각 | 일괄 고지("informational purposes only…"). 회사 블로그도 출처 링크가 옆 문장의 정확성을 보장하지 않는다는 취지로 한계를 인정 | [perplexity.ai/hub/blog/ai-hallucination](https://www.perplexity.ai/hub/blog/ai-hallucination) `[스니펫·회사]` |
| 정확도 주장 | 금융 제품 수치는 찾지 못함. 자사 Deep Research 벤치마크 DRACO(100개 과제, 전문가 루브릭, 데이터 공개)가 있으나 금융 제품 평가가 아니며 자사 출제 | [DRACO 블로그](https://www.perplexity.ai/hub/blog/evaluating-deep-research-performance-in-the-wild-with-the-draco-benchmark) `[스니펫·회사]` |
| 데이터 원천 | 재무 Financial Modeling Prep, 녹취 Quartr, 추정치 S&P Global, 차트 TradingView. SEC 공시 연동 | [perplexity.ai/finance/earnings](https://www.perplexity.ai/finance/earnings), [answers-for-every-investor](https://www.perplexity.ai/hub/blog/answers-for-every-investor) `[스니펫·회사]` |
| 한국 | 한국 종목 페이지는 있다(예: 005930.KS). DART 연동·한국어 공시·한국 실적 녹취는 **미확인**. 인도(BSE·NSE)는 별도 지원 발표가 있다 | [005930.KS financials](https://www.perplexity.ai/app/finance/005930.KS/financials), [changelog 8/15](https://www.perplexity.ai/changelog/what-we-shipped-august-15th) `[스니펫·회사]` |

**우리와의 차이·배울 점.**
- 근거는 문장 → URL 수준이고, 그 문장이 원문과 맞는지는 판정하지 않는다. 우리 "문장별 판정"이 바로 이 빈칸을 채운다.
- "알림이 저장된 질의를 실행" 구조는 판단 일지와 붙이기 좋다. 예: 다시 볼 날이나 새 공시 때 당시 질문을 다시 판정.

### 4.2 Fiscal.ai (구 FinChat)

| 틀 | 내용 | 출처 |
|---|---|---|
| 정의·이력 | 2025-06-17 FinChat에서 Fiscal.ai로 이름 변경, Series A $10M. 등록 사용자 35만+ | [fiscal.ai/blog/series-a-announcement](https://fiscal.ai/blog/series-a-announcement/) `[스니펫·회사]` |
| 가격 | Pro $49/월, Max $99/월(연간 결제 할인). Free·Enterprise 구성과 "숫자 클릭 감사"가 어느 등급에 있는지는 출처끼리 엇갈림(**미확인**) | [fiscal.ai/help](https://fiscal.ai/help/) `[스니펫·회사]`, [wallstreetzen 리뷰](https://www.wallstreetzen.com/blog/finchat-io-fiscal-ai-review/) `[스니펫·제3자]` |
| 첫 사용 흐름 | 로그인 → 대시보드 → 티커 추가·지표 선택 → Copilot(문서 요약, 녹취 질의, 자연어 스크리닝). 재방문 장치 **미확인** | [Ultimate Guide](https://fiscal.ai/blog/ultimate-guide-to-using-fiscal-AI/) `[스니펫·회사]` |
| 인용 단위 | **숫자마다** 클릭 링크 → 규제 공시 PDF의 해당 페이지를 하이라이트해 연다. Copilot 문장형 답변의 인용 단위는 **미확인** | [fiscal.ai/changelog](https://fiscal.ai/changelog/) `[스니펫·회사]` |
| 불확실성 표시 | **미확인** | — |
| 정확도 주장 | "Copilot Scores 91% Accuracy in FinanceBench vs. 31% for GPT-4o (w/ Internet Access)". 문항 선택("Top 100"), 채점자, 산출물 공개 **미확인** | [fiscal.ai/changelog](https://fiscal.ai/changelog/) `[스니펫·회사]` |
| 데이터 원천 | 미국·캐나다·ADR·영국·EU 핵심 커버리지. 그 밖 지역 펀더멘털은 S&P Capital IQ | [fiscal.ai/products/api](https://fiscal.ai/products/api) `[스니펫·회사]` |
| 한국 | KRX·DART·한국어 **미확인** | — |

**우리와의 차이·배울 점.**
- "숫자 → 공시 PDF 페이지 하이라이트"는 신뢰 UX의 좋은 기준이다. 우리 문장 → 문단 연결도 원문(DART 뷰어) 위치까지 이어 주면 좋다.
- 검증 기능을 상위 요금제에만 둘 수 있다는 서술은 출처끼리 엇갈린다. 그래도 반면교사로 삼는다. 우리는 검증 표시를 기본 기능으로 둔다.

### 4.3 국내: 딥서치 (DeepSearch)

| 틀 | 내용 | 출처 |
|---|---|---|
| 정의 | 2013년 설립 금융 데이터·AI 기업. 기관에 금융 특화 엔진을 공급하고, 국민연금공단 LLM 질의응답 서비스를 구축(2025-05 보도) | [전자신문](https://www.etnews.com/20250527000397) `[스니펫·제3자]` |
| 가격 | 개인 앱(App Store id1547759190) 인앱: 딥서치 프로 1개월 ₩49,000, 베이직 ₩9,900. ai.deepsearch.com 가격 **미확인** | [App Store](https://apps.apple.com/kr/app/%EB%94%A5%EC%84%9C%EC%B9%98/id1547759190) `[스니펫·제3자]` |
| 첫 사용 흐름 | **미확인**. "시장 전반 또는 특정 종목을 자유 질문"하는 형태라는 소개만 확인 | [ai.deepsearch.com](https://ai.deepsearch.com/) `[스니펫·회사]` |
| 근거 표시 | 모든 수치를 원천으로 추적 가능, 근거가 확인되지 않는 내용은 생성하지 않음, 숫자는 LLM이 아닌 결정론 엔진으로 계산. 인용 단위·클릭 방식 **미확인** | [ai.deepsearch.com](https://ai.deepsearch.com/) `[스니펫·회사]` |
| 정확도 주장 | "환각 발생률 5% 미만". 측정 방법·표본·채점자 **미확인** | 같은 곳 `[스니펫·회사]` |
| 데이터·한국 | DART, 한국거래소 시세, 증권사 리포트, 뉴스, 비상장사 데이터 | 같은 곳 `[스니펫·회사]` |
| 계획 | 금융 에이전트 '딥서치 AI' 2026년 하반기 출시 계획(2026-05 보도). 실제 출시 여부 **미확인** | [한국경제TV](https://www.wowtv.co.kr/NewsCenter/News/Read?articleId=A202605070595) `[스니펫·제3자]` |

**우리와의 차이·배울 점.** "숫자는 결정론 계산, 문장은 LLM"으로 역할을 나눈다. 우리 판정 정책에서 숫자 일치를 코드로 확인하는 것(`number_ok`)과 같은 방향이다. 반면 "환각 5% 미만"처럼 방법 없는 단일 수치는 우리가 피할 것이다.

### 4.4 국내: DartPoint AI (사이냅소프트) — 가장 가까운 비교 대상

| 틀 | 내용 | 출처 |
|---|---|---|
| 정의·대상 | DART 전자공시를 AI로 분석하는 기업정보 서비스. 대상은 주식 투자자, 기업 분석가, 취업준비생. 2024-12-19 오픈 베타 | [디지털데일리](https://www.ddaily.co.kr/page/view/2024121909421232276) `[스니펫·제3자]` |
| 가격 | 무료. 개인화·전문가용 유료 기능 추가 계획 | [인공지능신문](https://www.aitimes.kr/news/articleView.html?idxno=33299) `[스니펫·제3자]` |
| 첫 사용 흐름 | 회원가입 → 기업 페이지(개요·재무·직원·주주·건전성 그래프와 표) → LLM 어시스턴트 질문 → '지능형 기업분석 보고서', 표 Excel 내보내기. 재방문 장치 **미확인**(사용 사례 글 "내가 보유한 주식, 이번 분기 실적 괜찮은 걸까?"가 실적 시즌을 겨냥한 것으로 보이나 제목만 봤다) | [사이냅 블로그 34570](https://www.synapsoft.co.kr/blog/34570/) `[스니펫·회사]`, [블로그 37191](https://www.synapsoft.co.kr/blog/37191/) `[제목만]` |
| 근거 표시 | RAG로 여러 공시를 찾아 답변과 함께 근거를 붙이고, 근거 링크를 누르면 "근거가 된 문서의 페이지로 연결". 문장 단위 판정·불확실성 표시 **미확인** | [사이냅 블로그 34570](https://www.synapsoft.co.kr/blog/34570/) `[스니펫·회사]` |
| 정확도 주장 | 찾지 못함 | — |
| 데이터·한국 | DART 중심, 한국어 | 같은 곳 `[스니펫·회사]` |
| MCP | 2025-06 MCP 서버 공개. GitHub `dartpointai/dartpoint-mcp`: "MCP Server for public disclosure information of Korean companies, powered by the dartpoint.ai API.", 생성 2025-06-11, 최근 갱신 2025-09-15 | [github.com/dartpointai/dartpoint-mcp](https://github.com/dartpointai/dartpoint-mcp) `[직접 확인]` |

**우리와의 차이·배울 점.**
- 대상(국내 개인 투자자)과 원천(DART)이 우리와 가장 겹친다. 근거 연결이 **공시 문서 페이지** 수준이라는 점까지는 확인됐다.
- 우리의 차별점은 세 가지가 된다: 문장 → 문단 연결, 문장마다의 판정(✅⚠️❔), 판단 기록과 재방문.
- 무료 공개와 MCP 배포로 접근성을 넓힌 방식은 참고할 만하다.

---

## 5. 우리 서비스 기획에 대한 시사점

이 절은 1~4절의 사실을 바탕으로 한 **AI의 해석·제안**이다. 결정은 사람이 한다.

### 5.1 대상 사용자

- 기관용(Hebbia, AlphaSense, Rogo, Terminal X 기관판)은 사내 데이터 연동, 문서 출력, 영업 경유 가입이 중심이다. 포트폴리오 규모의 우리가 겨룰 축이 아니다.
- 국내 개인 + DART라는 조합에서 확인된 서비스는 DartPoint(무료)와 딥서치 정도였다. Terminal X는 국내 리테일 경로가 있지만 회사 스스로 미국 주식 특화라고 밝힌다.
- 그래서 가설 "개인 투자자·리서치 입문자"를 유지하되, 한 단계 좁힌다: **"공시를 직접 읽어 보려는데 어디부터 볼지 모르는 사람."** 근거 문단을 보여 주는 가치가 이 사람에게 가장 크다.

### 5.2 첫 사용 흐름(가입 직후)

경쟁 제품의 첫 화면은 대시보드(AlphaSense, Fiscal), 기업 페이지(DartPoint), 실적 허브(Perplexity)가 많다. 질문 하나로 바로 결과를 보게 하는 곳은 드물었다. 제안:

1. 가입 직후 **예시 종목 1개 + 예시 질문 2~3개**를 보여 주고, 하나를 누르면 바로 답변을 낸다.
2. 답변에서 배지(✅⚠️❔) 하나를 눌러 **근거 문단을 펼치는 동작**을 첫 경험의 핵심으로 둔다(Bloomberg·AlphaSense의 "항목 → 원문 발췌"와 같은 동선).
3. 답변 아래에서 "이 판단을 일지에 기록 + 다시 볼 날"을 제안한다. 재방문 이유를 첫 세션에서 만든다(AlphaSense Watchlist·Alert, Perplexity 저장 질의 알림의 역할).
4. 관심종목 추가는 일지 기록 다음에 둔다.

### 5.3 남길 메뉴

| 메뉴 | 남길 이유(경쟁 근거) |
|---|---|
| 질문하기(문장별 판정) | 확인된 범위에서 문장마다 판정을 붙이는 곳은 없었다. 핵심 차별점 |
| 판단 일지(당시 근거 스냅샷, 다시 볼 날, 공시 변화 비교) | 경쟁사의 재방문 장치는 알림·대시보드다. "내가 그때 무엇을 근거로 판단했나"를 보존하는 곳은 확인하지 못했다 |
| 관심종목 | AlphaSense·Perplexity의 재방문 축. 일지와 묶는다 |
| 지표 출처·조회 시각 | Fiscal의 숫자 → 원문 감사처럼 신뢰의 기본기 |
| (뺄 후보) 리포트·문서 출력, 사내 데이터 업로드 | 기관용 기능이다. 우리 대상과 규모에 맞지 않는다 |

### 5.4 차별점 메시지

1. **"문장별 판정 + 판단 기록·재방문"** — 경쟁 제품의 근거 표시는 문서·페이지·URL·숫자 단위였다. 문장마다 지지/부분/근거 없음을 보여 주고, 그 화면을 일지로 보존하는 조합은 확인되지 않았다. 단, "확인되지 않았다"는 공개 자료 기준이다. 머지 전 원문 검증(9절)으로 다시 확인한다.
2. **"작은 모델 + 판단 모델의 비용 효율"** — 경쟁사는 대부분 프런티어 모델이나 프리미엄 데이터를 앞세운다. 우리 메시지는 "싸고 정확하다"보다 **"얼마나 싸고, 어디까지 정확한지 사전등록한 방법으로 잰다"**가 안전하다. A-2에서 ✅ 정밀도 목표가 확인되지 않았고 주체 교체 문장의 정확도가 0.57이었기 때문이다(`docs/lab/evidence-a2-interpretation.md`). 비교 실험 결과가 나오기 전에는 우위를 주장하지 않는다.
3. **"검증 방법 자체를 공개한다"** — 경쟁사 정확도 주장(98%, 91%, 89.9%, 2.42x, 5% 미만)은 모두 자체 측정이고 방법 공개가 부족하다. 사전등록 + 원장 + 관문 실패까지 공개하는 태도가 그 자체로 차별점이다.

### 5.5 피해야 할 것

- 방법 없는 단일 수치(AlphaSense 98%, 딥서치 5% 미만, Fiscal 91%, Rogo 2.42x).
- "환각이 없다" 같은 절대 단정(Terminal X 사용자 가이드).
- 검증 기능을 유료 등급에 가두는 것(Fiscal.ai 관련 서술. 등급 배치는 미확인).
- 우리에게만 유리한 채점 차원을 넣는 것(Terminal X Source Authority처럼).
- ✅를 "사실 확인됨"으로 읽히게 하는 문구. 배지는 "검색된 공시 문단 기준 AI 판정"이다.
- 매매 권유로 읽히는 흐름. Terminal X × NH는 답변에서 주문으로 이어지지만, 우리는 판단 기록에서 멈춘다.

---

## 6. Terminal X 비교 주장 vs 우리가 계획한 비교 실험

우리 계획(리드가 제시한 계획 기준, 저장소에는 아직 사전등록이 없다): **8B 단독 / 8B + JEV / Claude / GPT** 네 조건을 비교한다. 지표는 정확도, 근거 없는 문장 비율, 지연, 상대 총비용이다.

| 항목 | Terminal X 블로그(89.9 / 70.6 / 63.3 / 52.7) | 우리 계획 |
|---|---|---|
| 평가 단위 | 질의(답변 전체) | 답변 안의 **문장(주장)** |
| "정확도"의 뜻 | 7차원 루브릭 합성 점수, 가중치 미확인 | 문장 판정의 정밀도·재현율·AUC와 95% 신뢰구간(A-2 방식). "근거 없는 문장 비율"은 따로 보고 |
| 정답(채점자) | "independent human evaluators"(인원·블라인드·일치도 미확인) | 지금까지는 AI 참조 라벨(Claude Opus·Codex 독립 라벨 → 조정). **사람 감사는 하지 않았다.** 비교 실험에서도 이 한계는 그대로이니, 사람 감사 표본을 넣을지 사전등록에서 정해야 한다 |
| 사전등록·원장 | 공개된 것 없음 | `lab/evidence/prereg*.json`, `lab/evidence/attempts.jsonl`. 관문 실패도 공개(A-2 ✅ 정밀도 0.903이지만 예측 144건 < 150건으로 관문 실패) |
| 데이터 공개 | 미확인 | 질문·라벨·결과 원자료를 저장소에 둔다. DART 원문·벡터는 gitignore |
| 비교 조건 | 경쟁 모델은 기본 설정 웹 UI → 검색 능력과 모델 능력이 섞인다 | **정할 것:** Claude·GPT 조건에 같은 DART 검색 문단 8개를 주는가(생성기 비교), 각자 검색하게 두는가(제품 비교). 이름부터 어느 쪽인지 밝혀야 한다 |
| 구조적 유리함 | Source Authority 차원이 자사 라이선스 소스를 우대 | 우리 판정기(JEV)로 Claude·GPT 답변을 채점하면 판정기가 채점자를 겸한다. 정답은 판정기와 독립인 라벨로 정해야 한다 |
| 측정하는 것 | 티커 식별, 시의성, 출처 권위 등 | 근거 일치, 근거 없는 문장 비율, 지연, 상대 비용 |
| 측정하지 않는 것 | 문장 단위 근거 일치, 지연, 비용 | 티커 식별, 시의성, 답변 유용성(사용자 관점) |
| 비용 | 다루지 않음 | **상대 비율로만** 보고(예: 8B+JEV를 1로 둔 배수). TypeSafe 가격·금액은 공개 문서에 쓰지 않는다 |
| 지연 | 다루지 않음 | p50·p95. A-2 8B 생성은 p50 약 15초, p95 약 26초였다(확인 세트, 첫 호출 제외) |

**정리.** Terminal X 숫자는 "실제 리테일 질문에 대한 답변 전체의 체감 품질"을 잰다. 우리 실험은 "답변 문장이 공시 근거와 맞는가, 얼마에, 얼마나 빨리"를 잰다. 둘은 같은 축이 아니다. 그러니 우리 결과를 "Claude 70.6%"와 같은 줄에 놓고 비교하지 않는다. 대신 보도할 때 위 표의 차이를 함께 적는다.

---

## 7. 이 문서의 한계

- 사실 대부분이 검색 스니펫 기준이다(0절). 스니펫 요약이 원문과 다를 수 있다.
- 첫 사용 흐름은 공개 문서·보도에 나온 범위만 적었다. 실제 가입 화면은 보지 않았다(가입 금지).
- 국내 서비스는 2곳만 다뤘다. 증권사 앱 내 AI 기능(예: NH × Terminal X 외 다른 증권사)은 다루지 않았다.
- 출처끼리 엇갈린 수치는 둘 다 적고 미확인으로 남겼다. 하나를 고르지 않았다.

## 8. 참고: 우리 쪽 근거 문서

- A-2 평가 해석: [`docs/lab/evidence-a2-interpretation.md`](../lab/evidence-a2-interpretation.md)
- 판단 일지 설계: [`docs/superpowers/specs/2026-10-04-judgment-journal-c-design.md`](../superpowers/specs/2026-10-04-judgment-journal-c-design.md)
- 관심종목 설계: [`docs/superpowers/specs/2026-10-05-watchlist-d-design.md`](../superpowers/specs/2026-10-05-watchlist-d-design.md)

## 9. 머지 전 원문 검증 목록

이 문서의 핵심 주장을 받치는 출처다. 원문을 열 수 있는 환경(로컬 브라우저)에서 문구·수치·날짜를 확인하고, 맞으면 해당 표기를 `[확인 YYYY-MM-DD]`로 바꾼다.

| # | 확인할 것 | URL |
|---|---|---|
| 1 | Terminal X 벤치마크: 게시일, 표본, 채점자, 가중치, 데이터 공개 여부, 수치 | https://www.terminal-x.ai/blog/terminal-x-outperforms-claude-gpt-and-gemini-on-retail-investment-queries |
| 2 | Terminal X 사용자 가이드: Source Table, "no hallucination" 문구, `/report`, PPT·Excel 출력 여부 | https://www.terminal-x.ai/blog/terminal-x-user-guide |
| 3 | Terminal X 설립연도(2022 vs 2023)와 한국 시장 범위 | https://thevc.kr/plutoproject , https://www.hankyung.com/article/202608276874i , https://terminalxkr.substack.com/p/2025 |
| 4 | Hebbia 기반 모델 벤치마크 방법 | https://www.hebbia.com/blog/which-model-will-give-me-the-edge |
| 5 | AlphaSense "98% citation accuracy" 문구와 방법 | https://www.alpha-sense.com/security/generative-ai-security/ |
| 6 | Rogo "won't provide an answer" 정책과 "2.42x" | https://rogo.ai/news/rogo-faqs-learn-more-about-how-were-disrupting-financial-research |
| 7 | Fiscal.ai "91% vs 31%" 항목과 방법, 등급별 기능 | https://fiscal.ai/changelog/ , https://fiscal.ai/help/ |
| 8 | 딥서치 "환각 5% 미만"과 근거 표시 방식 | https://ai.deepsearch.com/ |
| 9 | DartPoint 근거 링크 단위(문서 페이지) | https://www.synapsoft.co.kr/blog/34570/ |
| 10 | Bloomberg 실적 콜 요약의 "항목 → 녹취 발췌" 연결 | https://www.bloomberg.com/company/press/bloomberg-launches-ai-powered-earnings-call-summaries |
