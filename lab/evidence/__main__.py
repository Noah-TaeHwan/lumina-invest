# lab/evidence/__main__.py
"""근거 판정 엔진 평가 명령줄. `python -m lab.evidence <명령>`.

호스트(uv)에서 돌리는 명령: split, passages, question-packets, questions-merge, claims, controlled-packets,
controlled-check, label-packets, labels-merge, judge, stage0-report.
컨테이너(Ollama 접근)에서 돌리는 명령: embed, retrieve, generate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class Paths:
    """저장소 루트 기준 경로. 테스트·검증은 임시 루트를 쓴다."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.ev = self.root / "lab/evidence"
        self.data = self.ev / "data"
        self.priv = self.root / "lab/data/evidence"
        self.split_json = self.ev / "split.json"
        self.prereg = self.ev / "prereg.json"
        self.prereg_holdout = self.ev / "prereg_holdout.json"
        self.attempts = self.ev / "attempts.jsonl"
        self.calls = self.priv / "jev_calls.jsonl"

    def jsonl(self, name: str) -> Path:
        """커밋하는 데이터 파일 경로."""
        return self.data / name


def read_jsonl(path: Path) -> list[dict]:
    """JSONL을 읽는다. 파일이 없으면 빈 목록."""
    path = Path(path)
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()] if path.exists() else []


def write_jsonl(path: Path, rows: list[dict]) -> None:
    """JSONL로 덮어쓴다(한글 그대로)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def log_attempt(P: Paths, event: str, **detail) -> None:
    """시도 원장에 한 줄을 남긴다."""
    P.attempts.parent.mkdir(parents=True, exist_ok=True)
    with P.attempts.open("a") as f:
        f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "event": event, **detail},
                           ensure_ascii=False) + "\n")


def load_split(P: Paths) -> dict:
    """split.json을 읽는다."""
    return json.loads(P.split_json.read_text())


SPLITS = ("tune", "check", "dev", "holdout", "all")


def holdout_frozen(P: Paths) -> bool:
    """동결 파일이 있고 분할·주장 해시를 담고 있어야 홀드아웃 봉인이 풀린다."""
    if not P.prereg_holdout.exists():
        return False
    data = json.loads(P.prereg_holdout.read_text() or "{}")
    return bool(data.get("split_sha256")) and bool(data.get("claims_sha256"))


def companies(P: Paths, split_name: str) -> list[dict]:
    """분할 이름에 속한 회사 목록. 홀드아웃이 포함되면(holdout·all) 동결 전까지 거부한다."""
    names = {"dev": {"tune", "check"}, "all": {"tune", "check", "holdout"}}.get(split_name, {split_name})
    if "holdout" in names and not holdout_frozen(P):
        raise SystemExit("holdout is frozen until prereg_holdout.json")
    return [c for c in load_split(P)["companies"] if c["split"] in names]


def passages_by_corp(P: Paths) -> dict[str, list[dict]]:
    """비공개 문단 파일을 회사별로 묶는다."""
    out: dict[str, list[dict]] = {}
    for r in read_jsonl(P.priv / "passages.jsonl"):
        out.setdefault(r["corp_code"], []).append(r)
    return out


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def load_vectors(P: Paths) -> dict[str, list[float]]:
    """현재 문단 본문 해시와 맞는 벡터만 돌려준다(본문이 바뀐 문단의 낡은 벡터는 버린다)."""
    current = {r["id"]: _sha(r["text"]) for r in read_jsonl(P.priv / "passages.jsonl")}
    out: dict[str, list[float]] = {}
    for r in read_jsonl(P.priv / "passage_vecs.jsonl"):
        if current.get(r["id"]) == r.get("sha256"):
            out[r["id"]] = r["vec"]
    return out


def retrieval_targets(questions: list[dict], answered: set[str]) -> list[dict]:
    """답변이 이미 동결된 질문은 검색을 다시 하지 않는다."""
    return [q for q in questions if q["qid"] not in answered]


def model_digest(tags: list[dict], model: str, expected: str) -> str:
    """Ollama /api/tags에서 모델 digest 앞 12자리를 찾아 사전등록 값과 대조한다."""
    digest = next((m["digest"][:12] for m in tags if m["name"] == model), None)
    if digest != expected:
        raise SystemExit(f"{model} digest {digest} != registered {expected}")
    return digest


def latency_rows(calls: list[dict]) -> tuple[list[dict], list[dict]]:
    """첫 시도 기록을 관문용(묶음·반복)과 단건(single)으로 나눈다."""
    first = [r for r in calls if r["attempt"] == 1]
    return [r for r in first if r["tag"] != "single"], [r for r in first if r["tag"] == "single"]


def session_stats(rows: list[dict]) -> list[dict]:
    """세션별 요청 수·실패 수·지연 p50/p95·입력 토큰 p50."""
    from lab.evidence.metrics import percentile

    out = []
    for times in sessions([r["called_at"] for r in rows]):
        ts = set(times)
        rs = [r for r in rows if datetime.fromisoformat(r["called_at"]).isoformat() in ts]
        lat = [r["latency_ms"] for r in rs]
        out.append({"start": times[0], "n": len(rs), "failures": sum(not r["ok"] for r in rs),
                    "p50_ms": percentile(lat, 50), "p95_ms": percentile(lat, 95),
                    "tokens_p50": percentile([r["input_tokens"] for r in rs], 50)})
    return out


def cmd_split(P: Paths, args) -> None:
    """상장사 목록에서 시드 15 + 무작위 25개사를 고르고 군집 단위로 분할한다. JEV 호출 뒤에는 거부."""
    import httpx

    from app.services.evidence import dart, passages
    from lab.evidence import split as sp

    if P.calls.exists():
        raise SystemExit("split is frozen once JEV calls exist")
    key = dart.load_dart_key()
    client = httpx.Client(timeout=60)
    xml_path = P.priv / "corpCode.xml"
    if not xml_path.exists():
        xml_path.parent.mkdir(parents=True, exist_ok=True)
        xml_path.write_bytes(dart.fetch_corp_codes(client, key))
    raw = xml_path.read_bytes()
    corps = dart.parse_corp_codes(raw)
    by_stock = {c.stock_code: c for c in corps}
    chosen: list[dict] = []
    texts: dict[str, str] = {}
    ledger: list[dict] = []

    def take(corp, source: str) -> str:
        rep = dart.pick_annual_report(dart.list_annual_reports(client, key, corp.corp_code))
        if rep is None:
            return "no_annual_report"
        if source == "random" and sp.is_finance(dart.company_info(client, key, corp.corp_code).get("induty_code", "")):
            return "finance"
        path = dart.download_document(client, key, rep["rcept_no"], P.priv / "docs", P.jsonl("dart_ledger.jsonl"))
        try:
            ps = passages.build_passages(corp.corp_code, rep["rcept_no"], path.read_text(encoding="utf-8", errors="ignore"))
        except ValueError as exc:
            return f"parse: {exc}"
        chars = sum(len(p.text) for p in ps)
        if source == "random" and chars < sp.MIN_CHARS:
            return "short"
        chosen.append({**asdict(corp), "rcept_no": rep["rcept_no"], "report_nm": rep["report_nm"],
                       "source": source, "chars": chars})
        texts[corp.corp_code] = " ".join(p.text for p in ps)
        return "ok"

    for group in sp.SEED_GROUPS.values():
        for stock in group:
            result = take(by_stock[stock], "seed")
            ledger.append({"corp_code": by_stock[stock].corp_code, "corp_name": by_stock[stock].corp_name,
                           "source": "seed", "result": result})
            if result != "ok":
                raise SystemExit(f"seed {stock} failed: {result}")
    for corp in sp.shuffled(corps):
        if sum(c["source"] == "random" for c in chosen) >= sp.RANDOM_N:
            break
        if not sp.is_candidate(corp):
            continue
        ledger.append({"corp_code": corp.corp_code, "corp_name": corp.corp_name, "source": "random",
                       "result": take(corp, "random")})
    names = {c["corp_code"]: c["corp_name"] for c in chosen}
    code_of = {c["stock_code"]: c["corp_code"] for c in chosen}
    groups = [[code_of[s] for s in g] for g in sp.SEED_GROUPS.values()]
    cl = sp.clusters(names, texts, groups)
    holdout, dev = sp.assign(cl, sp.HOLDOUT_CAP)
    tune, check = sp.assign(sorted(dev, key=lambda v: v[0]), sp.TUNE_CAP)
    label = {}
    for name, part in (("holdout", holdout), ("tune", tune), ("check", check)):
        for members in part:
            for code in members:
                label[code] = name
    index = {code: i for i, members in enumerate(cl) for code in members}
    for c in chosen:
        c.update(cluster=index[c["corp_code"]], split=label[c["corp_code"]])
    write_jsonl(P.jsonl("draw_ledger.jsonl"), ledger)
    counts = {s: sum(c["split"] == s for c in chosen) for s in ("tune", "check", "holdout")}
    write_jsonl(P.jsonl("mention_edges.jsonl"), [{"from": a, "to": b, "name": n} for a, b, n in sp.mention_edges(names, texts)])
    log_attempt(P, "split", companies=len(chosen), clusters=len(cl), random=sum(c["source"] == "random" for c in chosen), **counts)
    sp.check_split_sizes(counts)
    P.split_json.parent.mkdir(parents=True, exist_ok=True)
    P.split_json.write_text(json.dumps({"seed": sp.SEED, "corpcode_sha256": hashlib.sha256(raw).hexdigest(),
                                        "companies": sorted(chosen, key=lambda c: c["corp_code"]),
                                        "clusters": cl}, ensure_ascii=False, indent=1) + "\n")
    print(json.dumps(counts))


def cmd_passages(P: Paths, args) -> None:
    """분할된 모든 회사의 문단을 비공개 파일에 쓰고, ID·해시 매니페스트를 커밋용으로 쓴다."""
    from app.services.evidence import passages

    rows, manifest = [], []
    for c in load_split(P)["companies"]:
        xml = (P.priv / "docs" / f"{c['rcept_no']}.xml").read_text(encoding="utf-8", errors="ignore")
        for p in passages.build_passages(c["corp_code"], c["rcept_no"], xml):
            rows.append(asdict(p))
            manifest.append({"id": p.id, "sha256": p.sha256, "chars": len(p.text)})
    write_jsonl(P.priv / "passages.jsonl", rows)
    write_jsonl(P.jsonl("passages_manifest.jsonl"), manifest)
    print(f"{len(rows)} passages")


CATEGORIES = ["사업 개요", "주요 제품·서비스", "매출 구성·수치", "원재료·생산설비", "위험·파생", "연구개발·주요계약"]
VARIANTS = ["숫자 변경", "기간 바꾸기", "주체 교체", "부정", "다른 기업 사실"]
K = 8
GEN_MODEL = "llama3.2:1b"
GEN_DIGEST = "baf6a787fdff"
EMBED_MODEL = "nomic-embed-text"


def merge_questions(rows: list[dict], corp_code: str, split: str) -> list[dict]:
    """작성자 출력 6줄을 범주 순서로 정렬해 qid를 붙인다. 범주가 정확히 하나씩이 아니면 ValueError."""
    cats = [r["category"] for r in rows]
    if sorted(cats) != sorted(CATEGORIES):
        raise ValueError(f"{corp_code}: categories {cats}")
    by_cat = {r["category"]: r["question"].strip() for r in rows}
    return [{"qid": f"{corp_code}-q{i}", "corp_code": corp_code, "category": cat, "question": by_cat[cat],
             "split": split} for i, cat in enumerate(CATEGORIES, 1)]


def natural_claims(answer_rows: list[dict]) -> list[dict]:
    """답변마다 앞 5문장을 자연 주장으로 만든다."""
    from app.services.evidence.claims import split_sentences

    out = []
    for a in answer_rows:
        for i, s in enumerate(split_sentences(a["answer"])[:5], 1):
            out.append({"cid": f"{a['qid']}-n{i}", "qid": a["qid"], "source": "natural", "text": s,
                        "variant": None, "expected": None})
    return out


def variant_for(i: int) -> str:
    """질문 순번 i(0부터)에 순환 배정하는 변형 유형."""
    return VARIANTS[i % len(VARIANTS)]


def assigned_variant(qid: str, all_qids: list[str]) -> str:
    """전체 질문을 qid 순으로 정렬한 순번으로 변형 유형을 배정한다(분할 선택과 무관)."""
    return variant_for(sorted(all_qids).index(qid))


def unique_by_qid(rows: list[dict]) -> list[dict]:
    """같은 qid가 두 번 나오면 ValueError."""
    seen: set[str] = set()
    for r in rows:
        if r["qid"] in seen:
            raise ValueError(f"duplicate qid {r['qid']}")
        seen.add(r["qid"])
    return rows


def check_controlled(row: dict, passages: list[str], names: tuple[str, ...],
                     expected_type: str | None = None) -> tuple[bool, str]:
    """배정 유형과 같고, 참 문장과 다르며, 새로 넣은 숫자·회사명이 문단 8개에 없는지 본다."""
    from app.services.evidence.claims import new_values_absent

    if expected_type is not None and row["variant_type"] != expected_type:
        return False, "variant type mismatch"
    if row["variant_text"].strip() == row["true_text"].strip():
        return False, "variant equals true text"
    if not new_values_absent(row["variant_text"], row["true_text"], passages, names):
        return False, "new value present in passages"
    return True, "ok"


def _corp_names(P: Paths) -> dict[str, str]:
    return {c["corp_code"]: c["corp_name"] for c in load_split(P)["companies"]}


def cmd_question_packets(P: Paths, args) -> None:
    """회사마다 지시문 + 지정 절 문단 전체를 담은 질문 작성 꾸러미를 비공개 폴더에 쓴다."""
    guide = (P.ev / "prompts/question_writer.md").read_text()
    pbc = passages_by_corp(P)
    for c in companies(P, args.split):
        body = "\n".join(f"[{p['id']}] {p['text']}" for p in pbc[c["corp_code"]])
        out = P.priv / "packets/questions" / f"{c['corp_code']}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(f"{guide}\n\n# 회사: {c['corp_name']} ({c['corp_code']})\n"
                       f"# 출력 파일: lab/evidence/data/questions/{c['corp_code']}.jsonl\n\n{body}\n")
    print("ok")


def cmd_questions_merge(P: Paths, args) -> None:
    """회사별 질문 파일을 검증·병합해 questions.jsonl을 쓴다(다른 분할의 기존 행은 보존)."""
    keep = [r for r in read_jsonl(P.jsonl("questions.jsonl")) if r["split"] not in
            {c["split"] for c in companies(P, args.split)}]
    rows = []
    for c in companies(P, args.split):
        rows += merge_questions(read_jsonl(P.data / "questions" / f"{c['corp_code']}.jsonl"), c["corp_code"], c["split"])
    write_jsonl(P.jsonl("questions.jsonl"), sorted(keep + rows, key=lambda r: r["qid"]))
    print(f"{len(rows)} questions")


def _ollama():
    import os

    from app.lib.ollama import OllamaClient
    return OllamaClient(os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434"), 600.0)


def cmd_embed(P: Paths, args) -> None:
    """분할의 문단을 임베딩해 비공개 파일에 캐시한다(본문 해시가 같은 ID는 건너뜀). 컨테이너에서 실행."""
    import asyncio

    from app.services.evidence.retrieve import DOC_PREFIX

    out = P.priv / "passage_vecs.jsonl"
    done = set(load_vectors(P))
    llm = _ollama()

    async def run():
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("a") as f:
            pbc = passages_by_corp(P)
            for c in companies(P, args.split):
                for p in pbc.get(c["corp_code"], []):
                    if p["id"] not in done:
                        vec = await llm.embed(EMBED_MODEL, DOC_PREFIX + p["text"])
                        f.write(json.dumps({"id": p["id"], "sha256": _sha(p["text"]), "vec": vec}) + "\n")

    asyncio.run(run())
    print("ok")


def cmd_retrieve(P: Paths, args) -> None:
    """분할의 질문마다 같은 회사 문단 중 상위 K개를 고른다. 컨테이너에서 실행."""
    import asyncio

    from app.services.evidence.retrieve import QUERY_PREFIX, top_k

    vecs = load_vectors(P)
    pbc = passages_by_corp(P)
    missing = [p["id"] for c in companies(P, args.split) for p in pbc[c["corp_code"]] if p["id"] not in vecs]
    if missing:
        raise SystemExit(f"{len(missing)} passages lack current vectors; run embed")
    codes = {c["corp_code"] for c in companies(P, args.split)}
    answered = {a["qid"] for a in read_jsonl(P.jsonl("answers.jsonl"))}
    qs = retrieval_targets([q for q in read_jsonl(P.jsonl("questions.jsonl")) if q["corp_code"] in codes], answered)
    llm = _ollama()
    keep = [r for r in read_jsonl(P.jsonl("retrieval.jsonl")) if r["qid"] not in {q["qid"] for q in qs}]

    async def run():
        rows = []
        for q in qs:
            ids = [p["id"] for p in pbc[q["corp_code"]]]
            qv = await llm.embed(EMBED_MODEL, QUERY_PREFIX + q["question"])
            rows.append({"qid": q["qid"], "passage_ids": [ids[i] for i in top_k(qv, [vecs[i] for i in ids], K)]})
        return rows

    rows = asyncio.run(run())
    write_jsonl(P.jsonl("retrieval.jsonl"), sorted(keep + rows, key=lambda r: r["qid"]))
    print(f"{len(rows)} retrievals")


def _passage_text(P: Paths) -> dict[str, str]:
    return {r["id"]: r["text"] for r in read_jsonl(P.priv / "passages.jsonl")}


def cmd_generate(P: Paths, args) -> None:
    """질문마다 검색 문단 8개로 답변을 한 번 생성해 동결한다(이미 있는 qid는 건너뜀). 컨테이너에서 실행."""
    import asyncio

    import httpx

    from app.services.evidence.generate import PROMPT_SHA, generate_answer

    llm = _ollama()
    tags = httpx.get(f"{llm._base}/api/tags", timeout=30).json()["models"]
    digest = model_digest(tags, GEN_MODEL, GEN_DIGEST)
    text = _passage_text(P)
    names = _corp_names(P)
    ret = {r["qid"]: r["passage_ids"] for r in read_jsonl(P.jsonl("retrieval.jsonl"))}
    codes = {c["corp_code"] for c in companies(P, args.split)}
    existing = read_jsonl(P.jsonl("answers.jsonl"))
    done = {a["qid"] for a in existing}
    todo = [q for q in read_jsonl(P.jsonl("questions.jsonl")) if q["corp_code"] in codes and q["qid"] not in done]

    async def run():
        out = []
        for q in todo:
            ans = await generate_answer(llm, GEN_MODEL, names[q["corp_code"]], q["question"],
                                        [text[i] for i in ret[q["qid"]]])
            out.append({"qid": q["qid"], "model": GEN_MODEL, "digest": digest, "prompt_sha": PROMPT_SHA,
                        "answer": ans})
            write_jsonl(P.jsonl("answers.jsonl"), sorted(existing + out, key=lambda r: r["qid"]))
        return out

    print(f"{len(asyncio.run(run()))} answers")


def cmd_claims(P: Paths, args) -> None:
    """분할의 답변에서 자연 주장을 만든다(통제 주장 행은 보존)."""
    codes = {c["corp_code"] for c in companies(P, args.split)}
    answers = [a for a in read_jsonl(P.jsonl("answers.jsonl")) if a["qid"].split("-q")[0] in codes]
    keep = [c for c in read_jsonl(P.jsonl("claims.jsonl"))
            if c["source"] == "controlled" or c["qid"].split("-q")[0] not in codes]
    rows = natural_claims(answers)
    write_jsonl(P.jsonl("claims.jsonl"), sorted(keep + rows, key=lambda r: r["cid"]))
    print(f"{len(rows)} natural claims")


def cmd_controlled_packets(P: Paths, args) -> None:
    """회사마다 질문·검색 문단 8개·배정 변형 유형을 담은 통제 주장 작성 꾸러미를 쓴다."""
    guide = (P.ev / "prompts/controlled_writer.md").read_text()
    text = _passage_text(P)
    ret = {r["qid"]: r["passage_ids"] for r in read_jsonl(P.jsonl("retrieval.jsonl"))}
    codes = [c["corp_code"] for c in companies(P, args.split)]
    all_q = read_jsonl(P.jsonl("questions.jsonl"))
    all_qids = [q["qid"] for q in all_q]
    qs = [q for q in all_q if q["corp_code"] in codes]
    for corp in codes:
        parts = []
        for q in qs:
            if q["corp_code"] != corp:
                continue
            ps = "\n".join(f"[문단 {j}] {text[pid]}" for j, pid in enumerate(ret[q["qid"]], 1))
            parts.append(f"## {q['qid']} — 변형 유형: {assigned_variant(q['qid'], all_qids)}\n질문: {q['question']}\n{ps}\n")
        out = P.priv / "packets/controlled" / f"{corp}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(f"{guide}\n\n# 출력 파일: lab/evidence/data/controlled/{corp}.jsonl\n\n" + "\n".join(parts))
    print("ok")


def cmd_controlled_check(P: Paths, args) -> None:
    """통제 주장 작성 결과를 검사해 claims.jsonl에 c1(의역, 기대 지지됨)·c2(변형, 기대 지지 안 됨)로 넣는다."""
    text = _passage_text(P)
    ret = {r["qid"]: r["passage_ids"] for r in read_jsonl(P.jsonl("retrieval.jsonl"))}
    names = tuple(_corp_names(P).values())
    codes = {c["corp_code"] for c in companies(P, args.split)}
    all_qids = [q["qid"] for q in read_jsonl(P.jsonl("questions.jsonl"))]
    rows, rejected = [], []
    for corp in sorted(codes):
        for r in unique_by_qid(read_jsonl(P.data / "controlled" / f"{corp}.jsonl")):
            ok, why = check_controlled(r, [text[i] for i in ret[r["qid"]]], names,
                                       expected_type=assigned_variant(r["qid"], all_qids))
            if not ok:
                rejected.append({"qid": r["qid"], "reason": why})
                continue
            rows.append({"cid": f"{r['qid']}-c1", "qid": r["qid"], "source": "controlled", "text": r["true_text"],
                         "variant": "의역", "expected": "supported"})
            rows.append({"cid": f"{r['qid']}-c2", "qid": r["qid"], "source": "controlled", "text": r["variant_text"],
                         "variant": r["variant_type"], "expected": "not_supported"})
    keep = [c for c in read_jsonl(P.jsonl("claims.jsonl"))
            if not (c["source"] == "controlled" and c["qid"].split("-q")[0] in codes)]
    write_jsonl(P.jsonl("claims.jsonl"), sorted(keep + rows, key=lambda r: r["cid"]))
    write_jsonl(P.jsonl("controlled_rejected.jsonl"), rejected)
    log_attempt(P, "controlled-check", accepted=len(rows) // 2, rejected=len(rejected))
    print(f"{len(rows) // 2} accepted, {len(rejected)} rejected")


COMMANDS = {"split": cmd_split, "passages": cmd_passages, "question-packets": cmd_question_packets,
            "questions-merge": cmd_questions_merge, "embed": cmd_embed, "retrieve": cmd_retrieve,
            "generate": cmd_generate, "claims": cmd_claims, "controlled-packets": cmd_controlled_packets,
            "controlled-check": cmd_controlled_check}


LABELS = ("supported", "contradicted", "no_evidence", "non_claim")


def lid(cid: str) -> str:
    """라벨러에게 보이는 불투명 ID(출처·변형 유형을 숨긴다)."""
    return hashlib.sha256(cid.encode()).hexdigest()[:10]


def disagreements(r1: dict) -> list[str]:
    """1차 라벨이 다른 cid(두 라벨러 모두 낸 것만)."""
    a, b = r1["opus"], r1["codex"]
    return sorted(c for c in a.keys() & b.keys() if a[c]["label"] != b[c]["label"])


def missing_labels(r1: dict, cids: list[str]) -> dict[str, list[str]]:
    """라벨러별로 라벨이 없는 cid."""
    return {lb: sorted(set(cids) - set(r1.get(lb, {}))) for lb in ("opus", "codex")}


def kappa_ci(rows: list[dict]) -> tuple:
    """rows(cluster, qid, a, b)의 이진 κ와 2단 군집 부트스트랩 95% 구간. 한 종류뿐이면 None."""
    from lab.evidence.metrics import cluster_bootstrap, kappa

    def stat(rs):
        a, b = [r["a"] for r in rs], [r["b"] for r in rs]
        return None if len(set(a + b)) < 2 else kappa(a, b)

    return cluster_bootstrap(rows, stat)


def merge_labels(r1: dict, r2: dict) -> list[dict]:
    """1차 일치는 그대로, 불일치는 2차(조정 라운드) 일치로, 그래도 다르면 disputed."""
    out = []
    for cid in sorted(r1["opus"].keys() & r1["codex"].keys()):
        a, b = r1["opus"][cid], r1["codex"][cid]
        rec = {"cid": cid, "r1": {"opus": a, "codex": b}, "r2": None}
        if a["label"] == b["label"]:
            rec["label"] = a["label"]
        else:
            a2, b2 = r2.get("opus", {}).get(cid), r2.get("codex", {}).get(cid)
            rec["r2"] = {"opus": a2, "codex": b2}
            rec["label"] = a2["label"] if a2 and b2 and a2["label"] == b2["label"] else "disputed"
        out.append(rec)
    return out


def _read_labels(P: Paths, rnd: int, labeler: str, codes: set[str], cids: dict[str, str]) -> dict:
    """라벨러 출력(lid 기준)을 cid 기준으로 바꾼다. 허용되지 않은 라벨은 ValueError."""
    out = {}
    for corp in sorted(codes):
        for r in read_jsonl(P.data / "labels" / f"r{rnd}_{labeler}" / f"{corp}.jsonl"):
            if r["label"] not in LABELS:
                raise ValueError(f"bad label {r['label']} ({labeler} r{rnd} {corp})")
            if r["lid"] not in cids:
                raise ValueError(f"unknown lid {r['lid']} ({labeler} r{rnd} {corp})")
            if cids[r["lid"]] in out:
                raise ValueError(f"duplicate lid {r['lid']} ({labeler} r{rnd} {corp})")
            out[cids[r["lid"]]] = {"label": r["label"], "passage": r.get("passage"), "reason": r.get("reason", "")}
    return out


def cmd_label_packets(P: Paths, args) -> None:
    """라벨 꾸러미: 질문별 문단 8개와 주장(불투명 ID, 섞은 순서). 2차는 불일치만, 상대 라벨·이유를 함께 보인다."""
    import random

    guide = (P.ev / "prompts/labeler.md").read_text()
    text = _passage_text(P)
    ret = {r["qid"]: r["passage_ids"] for r in read_jsonl(P.jsonl("retrieval.jsonl"))}
    qtext = {q["qid"]: q["question"] for q in read_jsonl(P.jsonl("questions.jsonl"))}
    codes = {c["corp_code"] for c in companies(P, args.split)}
    claims = [c for c in read_jsonl(P.jsonl("claims.jsonl")) if c["qid"].split("-q")[0] in codes]
    only, other = None, {}
    if args.round == 2:
        cids = {lid(c["cid"]): c["cid"] for c in claims}
        r1 = {lb: _read_labels(P, 1, lb, codes, cids) for lb in ("opus", "codex")}
        only = set(disagreements(r1))
        other = r1["codex" if args.labeler == "opus" else "opus"]
    for corp in sorted(codes):
        parts = []
        for qid in sorted({c["qid"] for c in claims if c["qid"].startswith(corp)}):
            cl = [c for c in claims if c["qid"] == qid and (only is None or c["cid"] in only)]
            if not cl:
                continue
            random.Random(f"{args.round}-{qid}").shuffle(cl)
            ps = "\n".join(f"[문단 {j}] {text[pid]}" for j, pid in enumerate(ret[qid], 1))
            lines = []
            for c in cl:
                line = f"- lid={lid(c['cid'])}: {c['text']}"
                if only is not None:
                    o = other[c["cid"]]
                    line += f"\n  (다른 라벨러: {o['label']}, 문단 {o['passage']}, 이유: {o['reason']})"
                lines.append(line)
            parts.append(f"## 질문: {qtext[qid]}\n{ps}\n\n주장:\n" + "\n".join(lines) + "\n")
        if not parts:
            continue
        out = P.priv / "packets/labels" / f"r{args.round}_{args.labeler}" / f"{corp}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(f"{guide}\n\n# 출력 파일: lab/evidence/data/labels/r{args.round}_{args.labeler}/{corp}.jsonl\n\n"
                       + "\n".join(parts))
    print("ok")


def cmd_labels_merge(P: Paths, args) -> None:
    """1·2차 라벨을 병합해 labels.jsonl을 쓰고 1차 κ(이진, 비주장 제외)를 출력한다."""
    from lab.evidence.metrics import kappa

    codes = {c["corp_code"] for c in companies(P, args.split)}
    claims = [c for c in read_jsonl(P.jsonl("claims.jsonl")) if c["qid"].split("-q")[0] in codes]
    cids = {lid(c["cid"]): c["cid"] for c in claims}
    r1 = {lb: _read_labels(P, 1, lb, codes, cids) for lb in ("opus", "codex")}
    miss = missing_labels(r1, list(cids.values()))
    if any(miss.values()):
        raise SystemExit(f"round-1 labels missing: { {k: len(v) for k, v in miss.items()} }")
    r2 = {lb: _read_labels(P, 2, lb, codes, cids) for lb in ("opus", "codex")}
    rows = merge_labels(r1, r2)
    keep = [r for r in read_jsonl(P.jsonl("labels.jsonl")) if r["cid"].split("-q")[0] not in codes]
    write_jsonl(P.jsonl("labels.jsonl"), sorted(keep + rows, key=lambda r: r["cid"]))
    both = [c for c in r1["opus"].keys() & r1["codex"].keys()
            if "non_claim" not in (r1["opus"][c]["label"], r1["codex"][c]["label"])]
    k = kappa([r1["opus"][c]["label"] == "supported" for c in both], [r1["codex"][c]["label"] == "supported" for c in both])
    log_attempt(P, "labels-merge", split=args.split, labeled=len(rows),
                disputed=sum(r["label"] == "disputed" for r in rows), kappa_binary_r1=round(k, 4))
    print(f"{len(rows)} labels, kappa r1 {k:.3f}")


COMMANDS.update({"label-packets": cmd_label_packets, "labels-merge": cmd_labels_merge})


TOKEN_CAP = 20_000_000


def subset(cids: list[str], n: int) -> list[str]:
    """sha256 순으로 정렬한 앞 n개(결정적 표본)."""
    return sorted(cids, key=lambda c: hashlib.sha256(c.encode()).hexdigest())[:n]


def sessions(times: list[str], gap_hours: float = 1.0) -> list[list[str]]:
    """호출 시각을 gap_hours 이상 간격에서 세션으로 나눈다."""
    ts = sorted(datetime.fromisoformat(t) for t in times)
    out: list[list[str]] = []
    for t in ts:
        if not out or (t - datetime.fromisoformat(out[-1][-1])).total_seconds() >= gap_hours * 3600:
            out.append([])
        out[-1].append(t.isoformat())
    return out


def _judgeable(P: Paths, split_name: str) -> list[dict]:
    """분할의 주장 중 최종 라벨이 disputed·non_claim이 아닌 것(라벨 포함)."""
    codes = {c["corp_code"] for c in companies(P, split_name)}
    lab = {r["cid"]: r["label"] for r in read_jsonl(P.jsonl("labels.jsonl"))}
    return [dict(c, label=lab[c["cid"]]) for c in read_jsonl(P.jsonl("claims.jsonl"))
            if c["qid"].split("-q")[0] in codes and lab.get(c["cid"]) not in (None, "disputed", "non_claim")]


def cmd_judge(P: Paths, args) -> None:
    """JEV 판정을 실행해 비공개 점수 파일에 쓴다. 사전등록 파일이 없으면 거부."""
    from app.lib.jev import JevClient
    from app.services.evidence.judge import judge_claim

    if not P.prereg.exists():
        raise SystemExit("prereg.json must be committed before JEV calls")
    if not args.tag:
        raise SystemExit("--tag is required")
    claims = _judgeable(P, args.split)
    if args.limit:
        keep = set(subset([c["cid"] for c in claims if c["source"] == "natural"], args.limit))
        claims = [c for c in claims if c["cid"] in keep]
    text = _passage_text(P)
    ret = {r["qid"]: r["passage_ids"] for r in read_jsonl(P.jsonl("retrieval.jsonl"))}
    names = _corp_names(P)
    client = JevClient(P.calls, TOKEN_CAP)
    out = []
    for c in claims:
        j = judge_claim(client, names[c["qid"].split("-q")[0]], c["text"], [text[i] for i in ret[c["qid"]]],
                        use_cache=not args.no_cache, tag=args.tag, single=args.single)
        out.append({"cid": c["cid"], "s": j.s, "c": j.c, "ok": j.ok, "requests": j.requests})
    write_jsonl(P.priv / "scores" / f"{args.tag}.jsonl", out)
    log_attempt(P, "judge", tag=args.tag, split=args.split, claims=len(out), failed=sum(not r["ok"] for r in out),
                used_tokens=client.used_tokens)
    print(f"{len(out)} judged, used tokens {client.used_tokens}")


def stage0_gates(*, corpus, kappa, controlled_agree, auc, batch_agree, p95_ms, fail, n_sessions, repeat_agree) -> dict:
    """spec 7절 Stage 0 관문 판정."""
    from lab.evidence.metrics import cp_upper

    upper = cp_upper(*fail)
    g = {
        "corpus": {"value": corpus, "pass": corpus[0] >= 38},
        "kappa": {"value": kappa, "pass": kappa >= 0.6},
        "controlled": {"value": controlled_agree, "pass": controlled_agree >= 0.85},
        "h_ko": {"value": auc, "pass": auc[0] is not None and auc[0] >= 0.70 and auc[1] is not None and auc[1] > 0.5},
        "batching": {"value": batch_agree, "pass": batch_agree >= 0.90},
        "latency_fail": {"value": {"p95_ms": p95_ms, "first_requests": fail[1], "fail_upper": upper,
                                   "sessions": n_sessions},
                         "pass": fail[1] >= 300 and n_sessions >= 2 and p95_ms <= 1500 and upper <= 0.02},
        "repeat": {"value": repeat_agree, "pass": repeat_agree >= 0.90},
    }
    g["go"] = all(v["pass"] for v in g.values())
    return g


def _scores(P: Paths, tag: str) -> dict[str, dict]:
    return {r["cid"]: r for r in read_jsonl(P.priv / "scores" / f"{tag}.jsonl")}


def cmd_stage0_report(P: Paths, args) -> None:
    """Stage 0 관문을 계산해 집계 JSON과 리포트를 쓴다(달러 금액은 쓰지 않는다)."""
    from app.services.evidence.judge import Judgement, passage_labels
    from lab.evidence.metrics import auc, cluster_bootstrap, kappa, percentile

    split = load_split(P)
    cluster = {c["corp_code"]: c["cluster"] for c in split["companies"]}
    n_ok = sum(1 for c in split["companies"] if (P.priv / "docs" / f"{c['rcept_no']}.xml").exists())
    labels = {r["cid"]: r for r in read_jsonl(P.jsonl("labels.jsonl"))}
    claims = {c["cid"]: c for c in read_jsonl(P.jsonl("claims.jsonl"))}
    dev_codes = {c["corp_code"] for c in companies(P, "dev")}
    nat = [l for cid, l in labels.items() if claims[cid]["source"] == "natural"
           and cid.split("-q")[0] in dev_codes]
    both = [l for l in nat if "non_claim" not in (l["r1"]["opus"]["label"], l["r1"]["codex"]["label"])]
    k_rows = [{"cluster": cluster[l["cid"].split("-q")[0]], "qid": l["cid"].rsplit("-", 1)[0],
               "a": l["r1"]["opus"]["label"] == "supported", "b": l["r1"]["codex"]["label"] == "supported"} for l in both]
    k, k_lo, k_hi = kappa_ci(k_rows)
    k = k if k is not None else 0.0
    ctrl = [(l, claims[cid]) for cid, l in labels.items() if claims[cid]["source"] == "controlled"
            and cid.split("-q")[0] in dev_codes]
    agree = [(l["r1"][lb]["label"] == "supported") == (c["expected"] == "supported") for l, c in ctrl
             for lb in ("opus", "codex")]
    controlled_agree = sum(agree) / len(agree) if agree else 0.0
    chk = _scores(P, "check")
    rows = []
    for c in _judgeable(P, "check"):
        if c["source"] == "natural" and c["cid"] in chk:
            r = chk[c["cid"]]
            rows.append({"cluster": cluster[c["qid"].split("-q")[0]], "qid": c["qid"],
                         "y": int(c["label"] == "supported"), "score": max(r["s"]) if r["ok"] else 0.0})
    stat = lambda rs: auc([r["y"] for r in rs], [r["score"] for r in rs])
    auc_ci = cluster_bootstrap(rows, stat)
    single, batched = _scores(P, "single"), chk
    pairs = [(a, b) for cid in single for a, b in zip(passage_labels(Judgement(single[cid]["s"], single[cid]["c"], True, 0)),
                                                       passage_labels(Judgement(batched[cid]["s"], batched[cid]["c"], True, 0)))
             if single[cid]["ok"] and batched[cid]["ok"]]
    batch_agree = sum(a == b for a, b in pairs) / len(pairs) if pairs else 0.0
    reps = [_scores(P, f"repeat{i}") for i in (1, 2, 3)]

    def sig(r):
        j = Judgement(r["s"], r["c"], r["ok"], 0)
        top = max(range(len(j.s)), key=lambda i: j.s[i])
        return (top, passage_labels(j)[top])

    rep_cids = [cid for cid in reps[0] if all(cid in r and r[cid]["ok"] for r in reps)]
    repeat_agree = (sum(len({sig(r[cid]) for r in reps}) == 1 for cid in rep_cids) / len(rep_cids)) if rep_cids else 0.0
    first, single_first = latency_rows(read_jsonl(P.calls))
    lat = [r["latency_ms"] for r in first]
    fails = sum(not r["ok"] for r in first)
    sess = sessions([r["called_at"] for r in first])
    gates = stage0_gates(corpus=(n_ok, len(split["companies"])), kappa=k, controlled_agree=controlled_agree,
                         auc=auc_ci, batch_agree=batch_agree, p95_ms=percentile(lat, 95) if lat else 1e9,
                         fail=(fails, len(first)), n_sessions=len(sess), repeat_agree=repeat_agree)
    summary = {"gates": gates, "check_rows": len(rows), "check_supported": sum(r["y"] for r in rows),
               "latency_ms": {q: percentile(lat, q) for q in (50, 95, 99)} if lat else {},
               "first_requests": len(first), "input_tokens": sum(r["input_tokens"] for r in read_jsonl(P.calls)),
               "session_sizes": [len(s) for s in sess], "disputed": sum(l["label"] == "disputed" for l in nat),
               "sessions": session_stats(first) if first else [],
               "single_requests": {"n": len(single_first), "failures": sum(not r["ok"] for r in single_first)},
               "kappa_ci": [k_lo, k_hi], "kappa_pairs": len(both), "kappa_non_claim_excluded": len(nat) - len(both),
               "batch_pairs": len(pairs), "repeat_claims": len(rep_cids),
               "controlled_pairs": len(agree)}
    out = P.ev / "results/stage0.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str) + "\n")
    report = P.root / "docs/lab/evidence-stage0-report.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# 근거 판정 엔진 Stage 0 리포트", "",
             "> 정답 라벨은 사람이 아니라 AI(Claude Opus·Codex)가 만든 **AI 참조 라벨**이다. 결과는 AI 참조 라벨과의 일치 성능이다.", "",
             f"**판정: {'GO' if gates['go'] else 'NO-GO'}**", "", "| 기준 | 값 | 통과 |", "|---|---|---|"]
    for name, g in gates.items():
        if name != "go":
            lines.append(f"| {name} | {json.dumps(g['value'], ensure_ascii=False, default=str)} | {'예' if g['pass'] else '아니오'} |")
    lines += ["", f"- 확인 세트 자연 주장 {summary['check_rows']}건(지지됨 {summary['check_supported']}건)",
              f"- JEV 첫 요청 {summary['first_requests']}회, 입력 토큰 {summary['input_tokens']:,}개, 세션 크기 {summary['session_sizes']}",
              f"- 라벨 조정 후에도 갈린 개발 자연 주장 {summary['disputed']}건(주결과에서 제외)"]
    report.write_text("\n".join(lines) + "\n")
    log_attempt(P, "stage0-report", go=gates["go"])
    print("GO" if gates["go"] else "NO-GO")


COMMANDS.update({"judge": cmd_judge, "stage0-report": cmd_stage0_report})


def main(argv: list[str] | None = None) -> None:
    """명령 분기."""
    ap = argparse.ArgumentParser(prog="lab.evidence")
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("cmd", choices=sorted(COMMANDS))
    ap.add_argument("--split", default="dev", choices=SPLITS)
    ap.add_argument("--labeler", choices=["opus", "codex"])
    ap.add_argument("--round", type=int, default=1)
    ap.add_argument("--tag", default="")
    ap.add_argument("--single", action="store_true")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args(argv)
    COMMANDS[args.cmd](Paths(args.root), args)


if __name__ == "__main__":
    main()
