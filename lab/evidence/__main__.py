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


def companies(P: Paths, split_name: str) -> list[dict]:
    """분할 이름에 속한 회사 목록. 홀드아웃은 동결 파일이 생기기 전까지 거부한다."""
    if split_name == "holdout" and not P.prereg_holdout.exists():
        raise SystemExit("holdout is frozen until prereg_holdout.json")
    names = {"dev": {"tune", "check"}, "all": {"tune", "check", "holdout"}}.get(split_name, {split_name})
    return [c for c in load_split(P)["companies"] if c["split"] in names]


def passages_by_corp(P: Paths) -> dict[str, list[dict]]:
    """비공개 문단 파일을 회사별로 묶는다."""
    out: dict[str, list[dict]] = {}
    for r in read_jsonl(P.priv / "passages.jsonl"):
        out.setdefault(r["corp_code"], []).append(r)
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
    P.split_json.parent.mkdir(parents=True, exist_ok=True)
    P.split_json.write_text(json.dumps({"seed": sp.SEED, "corpcode_sha256": hashlib.sha256(raw).hexdigest(),
                                        "companies": sorted(chosen, key=lambda c: c["corp_code"]),
                                        "clusters": cl}, ensure_ascii=False, indent=1) + "\n")
    write_jsonl(P.jsonl("draw_ledger.jsonl"), ledger)
    counts = {s: sum(c["split"] == s for c in chosen) for s in ("tune", "check", "holdout")}
    log_attempt(P, "split", companies=len(chosen), clusters=len(cl), **counts)
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


def check_controlled(row: dict, passages: list[str], names: tuple[str, ...]) -> tuple[bool, str]:
    """통제 변형이 참 문장과 다르고, 새로 넣은 숫자·회사명이 문단 8개에 없는지 본다."""
    from app.services.evidence.claims import new_values_absent

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
    """모든 문단을 임베딩해 비공개 파일에 캐시한다(이미 있는 ID는 건너뜀). 컨테이너에서 실행."""
    import asyncio

    from app.services.evidence.retrieve import DOC_PREFIX

    out = P.priv / "passage_vecs.jsonl"
    done = {r["id"] for r in read_jsonl(out)}
    llm = _ollama()

    async def run():
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("a") as f:
            for rows in passages_by_corp(P).values():
                for p in rows:
                    if p["id"] not in done:
                        vec = await llm.embed(EMBED_MODEL, DOC_PREFIX + p["text"])
                        f.write(json.dumps({"id": p["id"], "vec": vec}) + "\n")

    asyncio.run(run())
    print("ok")


def cmd_retrieve(P: Paths, args) -> None:
    """분할의 질문마다 같은 회사 문단 중 상위 K개를 고른다. 컨테이너에서 실행."""
    import asyncio

    from app.services.evidence.retrieve import QUERY_PREFIX, top_k

    vecs = {r["id"]: r["vec"] for r in read_jsonl(P.priv / "passage_vecs.jsonl")}
    pbc = passages_by_corp(P)
    codes = {c["corp_code"] for c in companies(P, args.split)}
    qs = [q for q in read_jsonl(P.jsonl("questions.jsonl")) if q["corp_code"] in codes]
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
    digest = next(m["digest"][:12] for m in tags if m["name"] == GEN_MODEL)
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
    qs = [q for q in read_jsonl(P.jsonl("questions.jsonl")) if q["corp_code"] in codes]
    for corp in codes:
        parts = []
        for i, q in enumerate(qs):
            if q["corp_code"] != corp:
                continue
            ps = "\n".join(f"[문단 {j}] {text[pid]}" for j, pid in enumerate(ret[q["qid"]], 1))
            parts.append(f"## {q['qid']} — 변형 유형: {variant_for(i)}\n질문: {q['question']}\n{ps}\n")
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
    rows, rejected = [], []
    for corp in sorted(codes):
        for r in read_jsonl(P.data / "controlled" / f"{corp}.jsonl"):
            ok, why = check_controlled(r, [text[i] for i in ret[r["qid"]]], names)
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


def main(argv: list[str] | None = None) -> None:
    """명령 분기."""
    ap = argparse.ArgumentParser(prog="lab.evidence")
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("cmd", choices=sorted(COMMANDS))
    ap.add_argument("--split", default="dev")
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
