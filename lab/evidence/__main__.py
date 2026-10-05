# lab/evidence/__main__.py
"""근거 판정 엔진 평가 명령줄. `python -m lab.evidence <명령>`.

호스트(uv)에서 돌리는 명령: split, passages, question-packets, questions-merge, claims, controlled-packets,
controlled-check, label-packets, labels-merge, judge, stage0-report.
컨테이너(Ollama 접근)에서 돌리는 명령: embed, retrieve, generate.

`--study a2`(A-2 spec 6절)는 같은 명령을 A-2 경로(lab/evidence/study_a2, lab/data/evidence_a2, split_a2.json,
prereg_a2.json)에서 돌린다. A-2의 봉인 분할은 check(확인 세트)이고, 조정·리포트는 a2-tune·a2-report다.
원장은 같은 attempts.jsonl에 "study": "a2"로 덧붙인다.

`--study a3`(A-3 spec 5절)는 A-3 경로(lab/evidence/study_a3, lab/data/evidence_a3, split_a3.json, prereg_a3.json)에서
돌린다. 조정 세트가 없고 전부 확인 세트(check)다. 사전등록 파일이 "status": "registered"가 되기 전에는 추첨·데이터를
만들지 않는다. 통제 주장은 c1 의역·c2 주체 교체·c3 표기 변형이고, 결과는 a3-report다.

`--study a4`(A-4 spec 4·5·8절)는 A-4 경로(lab/evidence/study_a4, lab/data/evidence_a4, split_a4.json, prereg_a4.json)에서
돌린다. 확정 전에는 a4-measure(1a 실측, 호출 0)와 a4-explore(A-2 조정 세트·A-3 확인 세트에 후속 주체 질문만, 별도 원장
jev_calls_explore.jsonl)만 돈다. 확정 뒤 추첨·데이터·동결을 거쳐 judge(주 판정 + 후속)와 a4-report를 1회 돌린다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


STUDIES = ("a1", "a2", "a3", "a4")


class Paths:
    """저장소 루트 기준 경로. 테스트·검증은 임시 루트를 쓴다. study="a2"·"a3"면 그 연구 경로(앞 연구 파일을 건드리지 않는다)."""

    def __init__(self, root: Path, study: str = "a1"):
        self.root = Path(root)
        self.study = study
        self.ev = self.root / "lab/evidence"
        self.attempts = self.ev / "attempts.jsonl"
        if study == "a2":
            self.data = self.ev / "study_a2"  # lab/evidence/a2.py 모듈과 이름이 겹치지 않게
            self.priv = self.root / "lab/data/evidence_a2"
            self.split_json = self.ev / "split_a2.json"
            self.prereg = self.ev / "prereg_a2.json"
            self.prereg_holdout = self.ev / "prereg_a2_check.json"
            self.tune_json = self.ev / "results/a2-tune.json"
            self.sealed = "check"
            self.groups = {"dev": {"tune"}, "all": {"tune", "check"}}
        elif study in ("a3", "a4"):
            self.data = self.ev / f"study_{study}"  # lab/evidence/a3.py·a4.py 모듈과 이름이 겹치지 않게
            self.priv = self.root / f"lab/data/evidence_{study}"
            self.split_json = self.ev / f"split_{study}.json"
            self.prereg = self.ev / f"prereg_{study}.json"
            self.prereg_holdout = self.ev / f"prereg_{study}_check.json"
            self.sealed = "check"
            self.groups = {"dev": set(), "all": {"check"}}
        else:
            self.data = self.ev / "data"
            self.priv = self.root / "lab/data/evidence"
            self.split_json = self.ev / "split.json"
            self.prereg = self.ev / "prereg.json"
            self.prereg_holdout = self.ev / "prereg_holdout.json"
            self.sealed = "holdout"
            self.groups = {"dev": {"tune", "check"}, "all": {"tune", "check", "holdout"}}
        self.calls = self.priv / "jev_calls.jsonl"
        self.explore_calls = self.priv / "jev_calls_explore.jsonl"  # A-4 탐색 후속 호출(확인 원장과 분리)

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
    rec = {"at": datetime.now(timezone.utc).isoformat(), "event": event}
    if P.study != "a1":
        rec["study"] = P.study
    with P.attempts.open("a") as f:
        f.write(json.dumps({**rec, **detail}, ensure_ascii=False) + "\n")


def load_split(P: Paths) -> dict:
    """split.json을 읽는다."""
    return json.loads(P.split_json.read_text())


SPLITS = ("tune", "check", "dev", "holdout", "all")


def holdout_frozen(P: Paths) -> bool:
    """동결 파일이 있고 분할·주장 해시를 담고 있으면 True(내용 재검증은 verify_freeze)."""
    if not P.prereg_holdout.exists():
        return False
    data = json.loads(P.prereg_holdout.read_text() or "{}")
    return bool(data.get("split_sha256")) and bool(data.get("claims_sha256"))


def holdout_data_open(P: Paths) -> bool:
    """사전등록에 stage1 블록(임계값·B*·MDE·프롬프트)이 있어야 홀드아웃 데이터를 만들 수 있다.

    A-2: 조정 결과(results/a2-tune.json, τ_s·θ)가 있어야 확인 세트 데이터를 만든다.
    A-3: 고르는 값이 없으므로 사전등록 파일이 확정("status": "registered")되어 있어야 한다.
    """
    if P.study == "a2":
        return P.tune_json.exists()
    if P.study in ("a3", "a4"):
        return prereg_registered(P)
    return P.prereg.exists() and bool(json.loads(P.prereg.read_text()).get("stage1"))


_SEALED = {"a1": "holdout data is sealed until prereg.json has a stage1 block",
           "a2": "check data is sealed until results/a2-tune.json exists (run a2-tune)",
           "a3": "check data is sealed until prereg_a3.json is registered (status: registered)",
           "a4": "check data is sealed until prereg_a4.json is registered (status: registered)"}


def prereg_registered(P: Paths) -> bool:
    """사전등록 파일이 초안이 아니라 확정본인가(A-3). 확정 커밋 뒤에는 그 파일을 고치지 않는다."""
    return P.prereg.exists() and json.loads(P.prereg.read_text()).get("status") == "registered"


def companies(P: Paths, split_name: str, stage: str = "judge") -> list[dict]:
    """분할의 회사 목록. 홀드아웃은 두 단계로 연다.

    데이터(stage="data"): stage1 사전등록 뒤부터 동결 전까지만. 판정(그 밖): 동결 뒤, 동결 해시가 그대로일 때만.
    """
    names = P.groups.get(split_name, {split_name})
    if P.sealed in names:
        if stage == "data":
            if not holdout_data_open(P):
                raise SystemExit(_SEALED[P.study])
            if P.prereg_holdout.exists():
                raise SystemExit(f"{P.sealed} data is frozen ({P.prereg_holdout.name} exists)")
        else:
            if not holdout_frozen(P):
                raise SystemExit(f"{P.sealed} is frozen until {P.prereg_holdout.name}")
            verify_freeze(P)
    return [c for c in load_split(P)["companies"] if c["split"] in names]


def once(P: Paths, path: Path, split_name: str, event: str, tag: str) -> None:
    """봉인 분할(A-1 홀드아웃, A-2 확인) 판정·기준선은 한 번만. 비공개 출력 파일뿐 아니라 커밋되는 원장으로도
    확인한다. 원장은 연구가 같은 줄만 센다(A-1 줄에는 study가 없다)."""
    if split_name == "all":
        raise SystemExit(f"use --split {P.sealed} or a dev split (not all) for judge/baselines")
    if split_name != P.sealed:
        return
    done = any(r.get("event") == event and r.get("split") == P.sealed and r.get("tag") == tag
               and r.get("study", "a1") == P.study for r in read_jsonl(P.attempts))
    if Path(path).exists() or done:
        raise SystemExit(f"{P.sealed} {event} {tag} already ran")


def _code_sha(P: Paths) -> str:
    """판정·평가 코드 파일 내용의 해시(동결 뒤 코드가 바뀌었는지 확인)."""
    files = sorted([P.root / "app/lib/jev.py", *(P.root / "app/services/evidence").glob("*.py"),
                    *(P.root / "lab/evidence").glob("*.py")])
    h = hashlib.sha256()
    for f in files:
        h.update(f.name.encode() + b"\0" + f.read_bytes())
    return h.hexdigest()


def _git_head(P: Paths) -> str:
    """현재 커밋 해시. git이 없는 환경(앱 이미지)에서는 빈 문자열."""
    import subprocess

    try:
        return subprocess.run(["git", "-C", str(P.root), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    except FileNotFoundError:
        return ""


def _tree_dirty(P: Paths) -> bool:
    """코드·평가 데이터에 커밋되지 않은 변경이 있으면 True."""
    import subprocess

    out = subprocess.run(["git", "-C", str(P.root), "status", "--porcelain", "--", "app", "lab/evidence"],
                         capture_output=True, text=True).stdout
    return bool(out.strip())


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


def freeze_bundle(P: Paths) -> dict:
    """spec 7절 5단계 동결 묶음: 원문·문단·검색·답변·주장·라벨·프롬프트·사전등록·코드 해시."""
    codes = {c["corp_code"] for c in load_split(P)["companies"] if c["split"] == P.sealed}
    pick = lambda name, key: [r for r in read_jsonl(P.jsonl(name)) if r[key].split("-q")[0] in codes]
    extra = {"tune_sha256": _file_sha(P.tune_json)} if P.study == "a2" else {}
    if P.study == "a4":  # 교체어 태그(판정 전 AI 작성)도 동결한다
        extra = {"swap_tags_sha256": _file_sha(P.jsonl("swap_tags.jsonl"))}
    prompts = ("labeler.md", "question_writer.md",
               "controlled_writer_a3.md" if P.study in ("a3", "a4") else "controlled_writer.md")
    return {**extra, "split_sha256": _file_sha(P.split_json),
            "passages_manifest_sha256": _file_sha(P.jsonl("passages_manifest.jsonl")),
            "dart_ledger_sha256": _file_sha(P.jsonl("dart_ledger.jsonl")),
            "questions_sha256": _sha_rows(pick("questions.jsonl", "qid")),
            "retrieval_sha256": _sha_rows(pick("retrieval.jsonl", "qid")),
            "answers_sha256": _sha_rows(pick("answers.jsonl", "qid")),
            "claims_sha256": _sha_rows(pick("claims.jsonl", "cid")),
            "labels_sha256": _sha_rows(pick("labels.jsonl", "cid")),
            "prompts_sha256": {n: _file_sha(P.ev / "prompts" / n) for n in prompts},
            "prereg_sha256": _file_sha(P.prereg),
            "code_sha256": _code_sha(P)}


def verify_freeze(P: Paths) -> None:
    """동결 묶음을 다시 계산해 다르면 멈춘다(라벨·주장·코드를 동결 뒤 바꾸지 못하게)."""
    frozen = json.loads(P.prereg_holdout.read_text())
    now = freeze_bundle(P)
    bad = [k for k, v in now.items() if frozen.get(k) != v]
    if bad:
        raise SystemExit(f"holdout freeze mismatch: {bad}")


def split_by_counts(values: list, counts: list[int]) -> list[list]:
    """평평한 점수 목록을 주장별 문단 수로 자른다."""
    out, i = [], 0
    for n in counts:
        out.append(values[i:i + n])
        i += n
    return out


def missing_baselines(scores: dict, n: int) -> list[str]:
    """조정에 필요한 기준선 4종 중 빠지거나 점수가 비거나 개수가 다른 것."""
    return [t for t in ("lex", "emb", "nli", "llm")
            if t not in scores or len(scores[t]) != n or any(v is None for v in scores[t])]


def reported_verdict(lo, hi, descriptive_only: bool, exploratory: bool) -> dict:
    """spec 6절: 표본 하한 미달이면 기술 통계, 탐색적이면 탐색적. 그 밖에는 통계 판정."""
    from lab.evidence.metrics import verdict

    v = verdict(lo, hi)
    if descriptive_only:
        v["statistical"] = "descriptive"
    elif exploratory:
        v["statistical"] = "exploratory"
    return v


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


STAGE0_TAGS = {"tune", "check", "repeat1", "repeat2", "repeat3"}


def latency_rows(calls: list[dict]) -> tuple[list[dict], list[dict]]:
    """첫 시도 기록을 관문용(Stage 0 묶음·반복)과 단건(single)으로 나눈다. holdout(Stage 1) 등은 뺀다."""
    first = [r for r in calls if r["attempt"] == 1]
    return [r for r in first if r["tag"] in STAGE0_TAGS], [r for r in first if r["tag"] == "single"]


def stage0_input_tokens(calls: list[dict]) -> int:
    """Stage 0 호출(묶음·반복·단건, 모든 시도)의 입력 토큰 합. holdout(Stage 1) 등은 뺀다."""
    return sum(r["input_tokens"] for r in calls if r["tag"] in STAGE0_TAGS or r["tag"] == "single")


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


def _corp_list(P: Paths, client, key) -> tuple[bytes, list]:
    """OpenDART 회사 고유번호 파일(비공개 폴더에 한 번 받아 둔다)과 파싱한 회사 목록."""
    from app.services.evidence import dart

    xml_path = P.priv / "corpCode.xml"
    if not xml_path.exists():
        xml_path.parent.mkdir(parents=True, exist_ok=True)
        xml_path.write_bytes(dart.fetch_corp_codes(client, key))
    raw = xml_path.read_bytes()
    return raw, dart.parse_corp_codes(raw)


def dart_self_aliases(info: dict, corp_name: str) -> list[str]:
    """DART 기업개황(company.json)의 종목명·영문명 중 회사명과 다른 것(A-3 자기 회사 별칭, 근거 있는 소스만)."""
    out = []
    for k in ("stock_name", "corp_name_eng"):
        v = (info.get(k) or "").strip()
        if v and v != corp_name and v not in out:
            out.append(v)
    return out


def company_names(P: Paths):
    """주체 확인 (d) 상장사 이름 사전: 연구 추첨 때 받은 DART corpCode.xml(비공개 폴더). 없으면 멈춘다."""
    from app.services.evidence import subject

    path = P.priv / "corpCode.xml"
    if not path.exists():
        raise SystemExit(f"{_rel(P, path)} is needed for the subject check (listed-company names); run split")
    return subject.load_company_names(path)


def self_names(P: Paths) -> dict[str, tuple[str, ...]]:
    """회사별 자기 회사 표기(회사명 + 추첨 때 남긴 DART 별칭). 별칭이 없는 분할(A-2)은 회사명만."""
    return {c["corp_code"]: (c["corp_name"], *c.get("self_aliases", ())) for c in load_split(P)["companies"]}


def _take(P: Paths, client, key, corp, source: str, check=None, aliases: bool = False) -> tuple[str, dict | None, str]:
    """사업보고서를 받아 문단으로 나누고 추첨 조건을 본다. (결과, 회사 행 또는 None, 지정 절 본문).

    check(corp, text)가 사유 문자열을 돌려주면 그 사유로 뺀다(A-2 교차 언급 제외).
    aliases=True(A-3)면 기업개황의 종목명·영문명을 회사 행 self_aliases에 남긴다.
    """
    from app.services.evidence import dart, passages
    from lab.evidence import split as sp

    rep = dart.pick_annual_report(dart.list_annual_reports(client, key, corp.corp_code))
    if rep is None:
        return "no_annual_report", None, ""
    info = dart.company_info(client, key, corp.corp_code) if source == "random" or aliases else {}
    if source == "random" and sp.is_finance(info.get("induty_code", "")):
        return "finance", None, ""
    try:  # 공시 원문이 없으면(DART status 014 등) 사업보고서가 없는 것과 같이 뺀다
        path = dart.download_document(client, key, rep["rcept_no"], P.priv / "docs", P.jsonl("dart_ledger.jsonl"))
    except ValueError as exc:
        if "<status>014</status>" not in str(exc):  # 요청 제한·키 오류 등은 표본을 바꾸지 않게 멈춘다
            raise
        return f"document: {exc}"[:200], None, ""
    try:
        ps = passages.build_passages(corp.corp_code, rep["rcept_no"], path.read_text(encoding="utf-8", errors="ignore"))
    except ValueError as exc:
        return f"parse: {exc}", None, ""
    chars = sum(len(p.text) for p in ps)
    if source == "random" and chars < sp.MIN_CHARS:
        return "short", None, ""
    text = " ".join(p.text for p in ps)
    why = check(corp, text) if check else None
    if why:
        return why, None, text
    row = {**asdict(corp), "rcept_no": rep["rcept_no"], "report_nm": rep["report_nm"], "source": source, "chars": chars}
    if aliases:
        row["self_aliases"] = dart_self_aliases(info, corp.corp_name)
    return "ok", row, text


def cmd_split(P: Paths, args) -> None:
    """상장사 목록에서 시드 15 + 무작위 25개사를 고르고 군집 단위로 분할한다. JEV 호출 뒤에는 거부."""
    import httpx

    from app.services.evidence import dart
    from lab.evidence import split as sp

    if P.calls.exists():
        raise SystemExit("split is frozen once JEV calls exist")
    if P.study in ("a3", "a4"):
        if not prereg_registered(P):
            raise SystemExit(f"split needs {P.prereg.name} registered (status: registered)")
        verify_prereg_code(P)
        if P.study == "a4":
            from lab.evidence import a4

            a4.check_registrable(json.loads(P.prereg.read_text()), _a4_selection(P), require_selection=True)
            return _split_a4(P)
        return _split_a3(P)
    if P.study == "a2":
        verify_prereg_code(P)
        return _split_a2(P)
    key = dart.load_dart_key()
    client = httpx.Client(timeout=60)
    raw, corps = _corp_list(P, client, key)
    by_stock = {c.stock_code: c for c in corps}
    chosen: list[dict] = []
    texts: dict[str, str] = {}
    ledger: list[dict] = []

    def take(corp, source: str) -> str:
        result, row, text = _take(P, client, key, corp, source)
        if row is not None:
            chosen.append(row)
            texts[corp.corp_code] = text
        return result

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


def prior_texts(P: Paths, studies: tuple[str, ...]) -> tuple[dict[str, str], dict[str, str]]:
    """앞 연구 회사 이름과 지정 절 본문(각 연구의 비공개 문단 파일). 하나라도 빠지면 멈춘다(교차 언급 제외를 못 하므로)."""
    names: dict[str, str] = {}
    texts: dict[str, list[str]] = {}
    for st in studies:
        S = Paths(P.root, st)
        names.update({c["corp_code"]: c["corp_name"] for c in load_split(S)["companies"]})
        for r in read_jsonl(S.priv / "passages.jsonl"):
            texts.setdefault(r["corp_code"], []).append(r["text"])
    missing = sorted(set(names) - set(texts))
    if missing:
        where = ", ".join(_rel(P, Paths(P.root, st).priv / "passages.jsonl") for st in studies)
        raise SystemExit(f"prior passages missing for {len(missing)} companies; draw needs {where}")
    return names, {code: " ".join(texts[code]) for code in names}


def a1_texts(P: Paths) -> tuple[dict[str, str], dict[str, str]]:
    """A-1 40개사 이름과 지정 절 본문(A-1 비공개 문단 파일)."""
    return prior_texts(P, ("a1",))


def _draw_random(P: Paths, prior_names: dict[str, str], prior_body: dict[str, str], *, seed: int, n: int,
                 check_cap: int, tag: str, meta: dict, aliases: bool = False) -> None:
    """무작위 n개사(앞 연구 회사·교차 언급 10회 이상 제외), 군집 단위 확인 ≤ check_cap / 나머지 조정. A-2·A-3 공용.

    원장 제외 사유는 {tag}_company·{tag}_mention:<코드>(A-2는 tag a1)."""
    import httpx

    from app.services.evidence import dart
    from lab.evidence import split as sp

    key = dart.load_dart_key()
    client = httpx.Client(timeout=60)
    raw, corps = _corp_list(P, client, key)
    chosen: list[dict] = []
    texts: dict[str, str] = {}
    ledger: list[dict] = []

    def linked(corp, text: str) -> str | None:
        code = sp.a1_link(corp.corp_name, text, prior_names, prior_body)
        return f"{tag}_mention:{code}" if code else None

    for corp in sp.shuffled(corps, seed=seed):
        if len(chosen) >= n:
            break
        skip = sp.a2_skip(corp, set(prior_names))
        if skip == "prefix":
            continue  # A-1처럼 원장에 남기지 않는다
        if skip:
            result = skip.replace("a1_", f"{tag}_")
        else:
            result, row, text = _take(P, client, key, corp, "random", linked, aliases=aliases)
            if row is not None:
                chosen.append(row)
                texts[corp.corp_code] = text
        ledger.append({"corp_code": corp.corp_code, "corp_name": corp.corp_name, "source": "random", "result": result})
    names = {c["corp_code"]: c["corp_name"] for c in chosen}
    cl = sp.clusters(names, texts, [])
    check, tune = sp.assign(cl, check_cap, seed=seed)
    label = {code: name for name, part in (("check", check), ("tune", tune)) for members in part for code in members}
    index = {code: i for i, members in enumerate(cl) for code in members}
    for c in chosen:
        c.update(cluster=index[c["corp_code"]], split=label[c["corp_code"]])
    write_jsonl(P.jsonl("draw_ledger.jsonl"), ledger)
    write_jsonl(P.jsonl("mention_edges.jsonl"), [{"from": a, "to": b, "name": n} for a, b, n in sp.mention_edges(names, texts)])
    counts = {s: sum(c["split"] == s for c in chosen) for s in ("tune", "check")}
    excluded: dict[str, int] = {}
    for r in ledger:
        if r["result"] != "ok":
            k = r["result"].split(":")[0]
            excluded[k] = excluded.get(k, 0) + 1
    log_attempt(P, "split", companies=len(chosen), clusters=len(cl), excluded=excluded, **counts)
    if len(chosen) < n:
        raise SystemExit(f"only {len(chosen)} companies drawn (need {n})")
    if P.study == "a2":
        sp.check_split_sizes(counts, sealed="check", others=("tune",))
    P.split_json.parent.mkdir(parents=True, exist_ok=True)
    P.split_json.write_text(json.dumps({"seed": seed, "corpcode_sha256": hashlib.sha256(raw).hexdigest(), **meta,
                                        "companies": sorted(chosen, key=lambda c: c["corp_code"]),
                                        "clusters": cl}, ensure_ascii=False, indent=1) + "\n")
    print(json.dumps(counts))


def _split_a2(P: Paths) -> None:
    """A-2 spec 6.2: 무작위 40개사(A-1 40개사·A-1과 교차 언급 10회 이상 제외), 군집 단위 확인 ≤ 20 / 나머지 조정."""
    from lab.evidence import split as sp

    a1_names, a1_body = a1_texts(P)
    _draw_random(P, a1_names, a1_body, seed=sp.A2_SEED, n=sp.A2_RANDOM_N, check_cap=sp.A2_CHECK_CAP, tag="a1",
                 meta={"a1_split_sha256": _file_sha(Paths(P.root).split_json)})


def _split_a3(P: Paths) -> None:
    """A-3 spec 5.2: 무작위 40개사(A-1·A-2 80개사와 그들과 교차 언급 10회 이상 제외), 전부 확인 세트(조정 세트 없음)."""
    from lab.evidence import a3

    names, body = prior_texts(P, ("a1", "a2"))
    _draw_random(P, names, body, seed=a3.SEED, n=a3.RANDOM_N, check_cap=a3.RANDOM_N, tag="prior",
                 meta={"a1_split_sha256": _file_sha(Paths(P.root).split_json),
                       "a2_split_sha256": _file_sha(Paths(P.root, "a2").split_json)}, aliases=True)


def _split_a4(P: Paths) -> None:
    """A-4 spec 4.3: 무작위 45개사(A-1·A-2·A-3 120개사와 그들과 교차 언급 10회 이상 제외), 전부 확인 세트."""
    from lab.evidence import a4

    names, body = prior_texts(P, a4.PRIOR_STUDIES)
    _draw_random(P, names, body, seed=a4.SEED, n=a4.RANDOM_N, check_cap=a4.RANDOM_N, tag="prior",
                 meta={"a1_split_sha256": _file_sha(Paths(P.root).split_json),
                       "a2_split_sha256": _file_sha(Paths(P.root, "a2").split_json),
                       "a3_split_sha256": _file_sha(Paths(P.root, "a3").split_json)}, aliases=True)


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


def _rel(P: Paths, path: Path) -> str:
    """꾸러미에 적는 저장소 기준 경로."""
    return path.relative_to(P.root).as_posix()


def _generator(P: Paths) -> tuple[str, str]:
    """연구의 생성기 태그와 digest 앞 12자리. A-2는 사전등록 파일에 적힌 값만 쓴다."""
    if P.study == "a1":
        return GEN_MODEL, GEN_DIGEST
    g = json.loads(P.prereg.read_text())["generator"]
    return g["model"], g["digest"]


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
    for c in companies(P, args.split, stage="data"):
        body = "\n".join(f"[{p['id']}] {p['text']}" for p in pbc[c["corp_code"]])
        out = P.priv / "packets/questions" / f"{c['corp_code']}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(f"{guide}\n\n# 회사: {c['corp_name']} ({c['corp_code']})\n"
                       f"# 출력 파일: {_rel(P, P.data)}/questions/{c['corp_code']}.jsonl\n\n{body}\n")
    print("ok")


def cmd_questions_merge(P: Paths, args) -> None:
    """회사별 질문 파일을 검증·병합해 questions.jsonl을 쓴다(다른 분할의 기존 행은 보존)."""
    keep = [r for r in read_jsonl(P.jsonl("questions.jsonl")) if r["split"] not in
            {c["split"] for c in companies(P, args.split, stage="data")}]
    rows = []
    for c in companies(P, args.split, stage="data"):
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
            for c in companies(P, args.split, stage="data"):
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
    missing = [p["id"] for c in companies(P, args.split, stage="data") for p in pbc[c["corp_code"]] if p["id"] not in vecs]
    if missing:
        raise SystemExit(f"{len(missing)} passages lack current vectors; run embed")
    codes = {c["corp_code"] for c in companies(P, args.split, stage="data")}
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
    """질문마다 검색 문단 8개로 답변을 한 번 생성해 동결한다(이미 있는 qid는 건너뜀). 컨테이너에서 실행.

    답변마다 생성 지연(ms)을 남긴다. 이 프로세스의 첫 답변은 모델 적재가 끼므로 cold로 표시한다(A-2 spec 7.1).
    """
    import asyncio
    import time

    import httpx

    from app.services.evidence.generate import PROMPT_SHA, generate_answer

    if P.study in ("a2", "a3", "a4"):
        verify_prereg_code(P)
        registered = json.loads(P.prereg.read_text())["generator"].get("prompt_sha")
        if registered != PROMPT_SHA:
            raise SystemExit(f"generator prompt_sha {PROMPT_SHA} != registered {registered}")
    llm = _ollama()
    tags = httpx.get(f"{llm._base}/api/tags", timeout=30).json()["models"]
    gen_model, gen_digest = _generator(P)
    digest = model_digest(tags, gen_model, gen_digest)
    text = _passage_text(P)
    names = _corp_names(P)
    ret = {r["qid"]: r["passage_ids"] for r in read_jsonl(P.jsonl("retrieval.jsonl"))}
    codes = {c["corp_code"] for c in companies(P, args.split, stage="data")}
    existing = read_jsonl(P.jsonl("answers.jsonl"))
    done = {a["qid"] for a in existing}
    todo = [q for q in read_jsonl(P.jsonl("questions.jsonl")) if q["corp_code"] in codes and q["qid"] not in done]

    async def run():
        out = []
        for n, q in enumerate(todo):
            t0 = time.perf_counter()
            ans = await generate_answer(llm, gen_model, names[q["corp_code"]], q["question"],
                                        [text[i] for i in ret[q["qid"]]])
            out.append({"qid": q["qid"], "model": gen_model, "digest": digest, "prompt_sha": PROMPT_SHA,
                        "answer": ans, "latency_ms": round((time.perf_counter() - t0) * 1000, 1), "cold": n == 0})
            write_jsonl(P.jsonl("answers.jsonl"), sorted(existing + out, key=lambda r: r["qid"]))
        return out

    print(f"{len(asyncio.run(run()))} answers")


def cmd_claims(P: Paths, args) -> None:
    """분할의 답변에서 자연 주장을 만든다(통제 주장 행은 보존).

    A-2: 제품과 같은 claim_spans·비주장 규칙·상한 8(lab.evidence.a2.natural_claims). 규칙에 걸린 문장도 표시해 남긴다.
    """
    codes = {c["corp_code"] for c in companies(P, args.split, stage="data")}
    answers = [a for a in read_jsonl(P.jsonl("answers.jsonl")) if a["qid"].split("-q")[0] in codes]
    keep = [c for c in read_jsonl(P.jsonl("claims.jsonl"))
            if c["source"] == "controlled" or c["qid"].split("-q")[0] not in codes]
    if P.study in ("a2", "a3", "a4"):
        from app.services.evidence.claims import claim_spans
        from lab.evidence import a2

        verify_prereg_code(P)
        rows = a2.natural_claims(answers)
        spans = sum(len(claim_spans(a["answer"])) for a in answers)
        log_attempt(P, "claims", split=args.split, answers=len(answers), sentences=spans, rows=len(rows),
                    not_claim_rule=sum(r["not_claim_rule"] for r in rows), capped=spans - len(rows))
    else:
        rows = natural_claims(answers)
    write_jsonl(P.jsonl("claims.jsonl"), sorted(keep + rows, key=lambda r: r["cid"]))
    print(f"{len(rows)} natural claims")


def _assignment_line(P: Paths, qid: str, all_qids: list[str]) -> str:
    """꾸러미 질문 머리줄의 배정. A-3은 주체 교체 하위 유형과 표기 변형 유형, 그 밖은 변형 유형."""
    if P.study in ("a3", "a4"):
        from lab.evidence import a3, a4

        sub, nt = (a3 if P.study == "a3" else a4).assigned(qid, all_qids)
        return f"주체 교체 하위 유형: {sub}, 표기 변형 유형: {nt}"
    return f"변형 유형: {assigned_variant(qid, all_qids)}"


def cmd_controlled_packets(P: Paths, args) -> None:
    """회사마다 질문·검색 문단 8개·배정 변형 유형을 담은 통제 주장 작성 꾸러미를 쓴다(A-3은 전용 지시문)."""
    guide = (P.ev / "prompts" / ("controlled_writer_a3.md" if P.study in ("a3", "a4") else "controlled_writer.md")).read_text()
    text = _passage_text(P)
    ret = {r["qid"]: r["passage_ids"] for r in read_jsonl(P.jsonl("retrieval.jsonl"))}
    codes = [c["corp_code"] for c in companies(P, args.split, stage="data")]
    all_q = read_jsonl(P.jsonl("questions.jsonl"))
    all_qids = [q["qid"] for q in all_q]
    qs = [q for q in all_q if q["corp_code"] in codes]
    for corp in codes:
        parts = []
        for q in qs:
            if q["corp_code"] != corp:
                continue
            ps = "\n".join(f"[문단 {j}] {text[pid]}" for j, pid in enumerate(ret[q["qid"]], 1))
            parts.append(f"## {q['qid']} — {_assignment_line(P, q['qid'], all_qids)}\n질문: {q['question']}\n{ps}\n")
        out = P.priv / "packets/controlled" / f"{corp}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(f"{guide}\n\n# 출력 파일: {_rel(P, P.data)}/controlled/{corp}.jsonl\n\n" + "\n".join(parts))
    print("ok")


def cmd_controlled_check(P: Paths, args) -> None:
    """통제 주장 작성 결과를 검사해 claims.jsonl에 c1(의역, 기대 지지됨)·c2(변형, 기대 지지 안 됨)로 넣는다.
    A-3은 c3(표기 변형 참, 기대 지지됨)까지 세 문장이다."""
    text = _passage_text(P)
    ret = {r["qid"]: r["passage_ids"] for r in read_jsonl(P.jsonl("retrieval.jsonl"))}
    names = tuple(_corp_names(P).values())
    codes = {c["corp_code"] for c in companies(P, args.split, stage="data")}
    all_qids = [q["qid"] for q in read_jsonl(P.jsonl("questions.jsonl"))]
    if P.study in ("a3", "a4"):
        rows, rejected = _controlled_a3(P, codes, text, ret, names, all_qids)
    else:
        rows, rejected = _controlled_a2(P, codes, text, ret, names, all_qids)
    keep = [c for c in read_jsonl(P.jsonl("claims.jsonl"))
            if not (c["source"] == "controlled" and c["qid"].split("-q")[0] in codes)]
    write_jsonl(P.jsonl("claims.jsonl"), sorted(keep + rows, key=lambda r: r["cid"]))
    others = [r for r in read_jsonl(P.jsonl("controlled_rejected.jsonl")) if r["qid"].split("-q")[0] not in codes]
    write_jsonl(P.jsonl("controlled_rejected.jsonl"), sorted(others + rejected, key=lambda r: r["qid"]))
    accepted = len({r["qid"] for r in rows})
    log_attempt(P, "controlled-check", accepted=accepted, rejected=len(rejected))
    print(f"{accepted} accepted, {len(rejected)} rejected")


def _controlled_a2(P: Paths, codes: set[str], text: dict, ret: dict, names: tuple[str, ...],
                   all_qids: list[str]) -> tuple[list[dict], list[dict]]:
    """A-1·A-2 통제 주장: c1 의역(지지됨), c2 배정 변형(지지 안 됨)."""
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
    return rows, rejected


def _controlled_a3(P: Paths, codes: set[str], text: dict, ret: dict, names: tuple[str, ...],
                   all_qids: list[str]) -> tuple[list[dict], list[dict]]:
    """A-3 통제 주장: c1 의역 참(지지됨), c2 주체 교체(지지 안 됨), c3 표기 변형 참(지지됨). 하나라도 검사에 걸리면
    그 질문의 문장을 모두 뺀다(짝 비교를 질문 단위로 맞춘다). 영문·약칭 배정인데 문단에 약칭이 없으면 c3 없이 c1·c2만.
    A-4는 검사(a3.check_swap·check_notation)와 지시문이 같고 c2 하위 유형 배정만 9칸 순환(a4.assigned)이다."""
    from lab.evidence import a3, a4

    assign = a4.assigned if P.study == "a4" else a3.assigned

    rows, rejected = [], []
    for corp in sorted(codes):
        for r in unique_by_qid(read_jsonl(P.data / "controlled" / f"{corp}.jsonl")):
            ps = [text[i] for i in ret[r["qid"]]]
            sub, nt = assign(r["qid"], all_qids)
            # 영문·약칭인데 문단에 쓸 약칭이 없으면 notation_text가 null이다(c1·c2만 넣는다)
            skip_c3 = r.get("notation_text") is None and nt == "영문·약칭"
            checks = [("swap", (r.get("swap_subtype") == sub, "swap subtype mismatch")),
                      ("notation", (r.get("notation_type") == nt, "notation type mismatch")),
                      ("swap", a3.check_swap(r["true_text"], r["swap_text"], ps, names, subtype=sub)),
                      ("notation", (True, "ok") if skip_c3 else
                       a3.check_notation(r["true_text"], r.get("notation_text") or "", nt, ps))]
            bad = next(((k, why) for k, (ok, why) in checks if not ok), None)
            if bad:
                rejected.append({"qid": r["qid"], "reason": f"{bad[0]}: {bad[1]}"})
                continue
            rows += [{"cid": f"{r['qid']}-c1", "qid": r["qid"], "source": "controlled", "text": r["true_text"],
                      "variant": "의역", "expected": "supported"},
                     {"cid": f"{r['qid']}-c2", "qid": r["qid"], "source": "controlled", "text": r["swap_text"],
                      "variant": f"주체 교체:{sub}", "expected": "not_supported"},
                     {"cid": f"{r['qid']}-c3", "qid": r["qid"], "source": "controlled", "text": r["notation_text"],
                      "variant": f"표기 변형:{nt}", "expected": "supported"}][:2 if skip_c3 else 3]
    return rows, rejected


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
    codes = {c["corp_code"] for c in companies(P, args.split, stage="data")}
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
        out.write_text(f"{guide}\n\n# 출력 파일: {_rel(P, P.data)}/labels/r{args.round}_{args.labeler}/{corp}.jsonl\n\n"
                       + "\n".join(parts))
    print("ok")


def r1_binary_kappa(pairs: list[tuple[str, str]]) -> float | None:
    """1차 라벨 (opus, codex) 쌍의 이진 κ(지지됨 대 나머지, 어느 한쪽이 비주장이면 뺀다). 쌍이 없으면 None."""
    from lab.evidence.metrics import kappa

    both = [(a, b) for a, b in pairs if "non_claim" not in (a, b)]
    return kappa([a == "supported" for a, _ in both], [b == "supported" for _, b in both]) if both else None


def cmd_labels_merge(P: Paths, args) -> None:
    """1·2차 라벨을 병합해 labels.jsonl을 쓰고 1차 κ(이진, 비주장 제외)를 출력한다."""
    from lab.evidence.metrics import kappa

    codes = {c["corp_code"] for c in companies(P, args.split, stage="data")}
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
    k = r1_binary_kappa([(r1["opus"][c]["label"], r1["codex"][c]["label"]) for c in r1["opus"].keys() & r1["codex"].keys()])
    log_attempt(P, "labels-merge", split=args.split, labeled=len(rows),
                disputed=sum(r["label"] == "disputed" for r in rows), kappa_binary_r1=None if k is None else round(k, 4))
    print(f"{len(rows)} labels, kappa r1 {'n/a' if k is None else f'{k:.3f}'}")


COMMANDS.update({"label-packets": cmd_label_packets, "labels-merge": cmd_labels_merge})


TOKEN_CAP = 20_000_000


def _token_cap(P: Paths) -> int:
    """연구별 입력 토큰 상한(A-1 20,000,000, A-2는 사전등록 값 6,000,000). 호출 기록 파일도 연구별이다."""
    return TOKEN_CAP if P.study == "a1" else int(json.loads(P.prereg.read_text())["token_cap"])


def a2_budget(P: Paths, split_name: str, claims: list[dict], text: dict, ret: dict, names: dict) -> list[dict]:
    """A-2 예산 규칙(prereg_a2.json budget): 캐시에 없는 주장 × 5,070 토큰으로 추정해 호출 전에 확인한다.
    A-3(prereg_a3.json budget)은 같은 추정으로 상한(7,000,000)을 넘으면 멈추기만 한다.

    조정 세트: 자연 주장만 판정하고, 확인 세트 자연 주장 몫(조정 세트와 같은 크기로 추정)을 예약한다.
    확인 세트: 자연 + 통제가 상한을 넘으면 통제 주장을 빼고(원장 기록), 자연만으로도 넘으면 멈춘다.
    """
    from app.lib.jev import request_key
    from app.services.evidence.judge import build_questions, build_state
    from lab.evidence import a2

    calls = read_jsonl(P.calls)
    used = sum(r["input_tokens"] for r in calls)
    cached = {r["key"] for r in calls if r["ok"]}

    def estimate(rows: list[dict]) -> int:
        n = 0
        for c in rows:
            ps = [text[i] for i in ret[c["qid"]]]
            n += request_key(build_state(names[c["qid"].split("-q")[0]], c["text"], ps), build_questions(len(ps))) not in cached
        return n * a2.TOKENS_PER_CALL

    cap = _token_cap(P)
    if P.study == "a3":
        # A-3은 통제 주장이 주결과라 빼지 않는다. 상한을 넘으면 호출 전에 멈춘다(새 사전등록으로 상한을 다시 정한다)
        a2.check_budget(used, estimate(claims), cap)
        return claims
    if split_name != "check":
        claims = [c for c in claims if c["source"] == "natural"]
        a2.check_budget(used, estimate(claims), cap, reserve=len(claims) * a2.TOKENS_PER_CALL)
        return claims
    natural = [c for c in claims if c["source"] == "natural"]
    est_all, est_nat = estimate(claims), estimate(natural)
    if used + est_all > cap:
        log_attempt(P, "budget-drop-controlled", split=split_name, used=used, estimate_all=est_all, cap=cap,
                    dropped=len(claims) - len(natural))
        claims, est_all = natural, est_nat
    a2.check_budget(used, est_all, cap)
    return claims


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


def _judgeable(P: Paths, split_name: str, include_disputed: bool = False) -> list[dict]:
    """분할의 주장 중 판정 대상(라벨 포함). 기본은 disputed·non_claim 제외, 민감도용으로 disputed 포함 가능.
    A-2 비주장 규칙에 걸린 문장(not_claim_rule)은 제품이 판정하지 않으므로 뺀다."""
    codes = {c["corp_code"] for c in companies(P, split_name)}
    lab = {r["cid"]: r["label"] for r in read_jsonl(P.jsonl("labels.jsonl"))}
    skip = {None, "non_claim"} | (set() if include_disputed else {"disputed"})
    return [dict(c, label=lab[c["cid"]]) for c in read_jsonl(P.jsonl("claims.jsonl"))
            if c["qid"].split("-q")[0] in codes and lab.get(c["cid"]) not in skip and not c.get("not_claim_rule")]


def cmd_judge(P: Paths, args) -> None:
    """JEV 판정을 실행해 비공개 점수 파일에 쓴다. 사전등록 파일이 없으면 거부."""
    from app.lib.jev import JevClient
    from app.services.evidence.judge import judge_claim

    if not P.prereg.exists():
        raise SystemExit(f"{P.prereg.name} must be committed before JEV calls")
    if not args.tag:
        raise SystemExit("--tag is required")
    if args.split == P.sealed and (args.tag != P.sealed or args.limit or args.single or args.no_cache):
        raise SystemExit(f"{P.sealed} judge runs once with --tag {P.sealed} and no --limit/--single/--no-cache")
    if P.study == "a4":
        return _judge_a4(P, args)
    if P.study in ("a2", "a3"):
        verify_prereg_code(P)
    once(P, P.priv / "scores" / f"{args.tag}.jsonl", args.split, "judge", args.tag)
    claims = _judgeable(P, args.split, include_disputed=(args.split == P.sealed))
    if args.limit:
        keep = set(subset([c["cid"] for c in claims if c["source"] == "natural"], args.limit))
        claims = [c for c in claims if c["cid"] in keep]
    text = _passage_text(P)
    ret = {r["qid"]: r["passage_ids"] for r in read_jsonl(P.jsonl("retrieval.jsonl"))}
    names = _corp_names(P)
    if P.study in ("a2", "a3"):
        claims = a2_budget(P, args.split, claims, text, ret, names)
    client = JevClient(P.calls, _token_cap(P))
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
    calls = read_jsonl(P.calls)
    first, single_first = latency_rows(calls)
    lat = [r["latency_ms"] for r in first]
    fails = sum(not r["ok"] for r in first)
    sess = sessions([r["called_at"] for r in first])
    gates = stage0_gates(corpus=(n_ok, len(split["companies"])), kappa=k, controlled_agree=controlled_agree,
                         auc=auc_ci, batch_agree=batch_agree, p95_ms=percentile(lat, 95) if lat else 1e9,
                         fail=(fails, len(first)), n_sessions=len(sess), repeat_agree=repeat_agree)
    summary = {"gates": gates, "check_rows": len(rows), "check_supported": sum(r["y"] for r in rows),
               "latency_ms": {q: percentile(lat, q) for q in (50, 95, 99)} if lat else {},
               "first_requests": len(first), "input_tokens": stage0_input_tokens(calls),
               "session_sizes": [len(s) for s in sess], "disputed": sum(l["label"] == "disputed" for l in nat),
               "sessions": session_stats(first) if first else [],
               "single_requests": {"n": len(single_first), "failures": sum(not r["ok"] for r in single_first)},
               "kappa_ci": [k_lo, k_hi], "kappa_pairs": len(both), "kappa_non_claim_excluded": len(nat) - len(both),
               "batch_pairs": len(pairs), "repeat_claims": len(rep_cids),
               "controlled_pairs": len(agree)}
    out = P.ev / "results/stage0.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str) + "\n")
    report = P.ev / "results/stage0-gates.md"
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


def _claims_for(P: Paths, split_name: str, stage: str = "judge") -> list[dict]:
    codes = {c["corp_code"] for c in companies(P, split_name, stage=stage)}
    return [c for c in read_jsonl(P.jsonl("claims.jsonl")) if c["qid"].split("-q")[0] in codes]


def cmd_claim_embed(P: Paths, args) -> None:
    """주장을 QUERY 접두어로 임베딩한다(cid+본문 해시가 같으면 건너뜀). 호스트 Ollama."""
    import asyncio

    from app.services.evidence.retrieve import QUERY_PREFIX

    out = P.priv / "claim_vecs.jsonl"
    done = {(r["cid"], r["sha256"]) for r in read_jsonl(out)}
    todo = [c for c in _claims_for(P, args.split) if (c["cid"], _sha(c["text"])) not in done]
    llm = _ollama()

    async def run():
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("a") as f:
            for c in todo:
                f.write(json.dumps({"cid": c["cid"], "sha256": _sha(c["text"]),
                                    "vec": await llm.embed(EMBED_MODEL, QUERY_PREFIX + c["text"])}) + "\n")

    asyncio.run(run())
    print(f"{len(todo)} claim vectors")


def nli_revision() -> str:
    from huggingface_hub import HfApi

    from lab.evidence.baselines import NLI_MODEL
    return HfApi().model_info(NLI_MODEL).sha


def cmd_baselines(P: Paths, args) -> None:
    """기준선 하나를 분할 주장에 실행한다. 홀드아웃은 한 번만, NLI는 사전등록 리비전으로."""
    import asyncio
    import os

    import httpx

    from lab.evidence import baselines as bl

    out = P.priv / "baselines" / f"{args.tag}_{args.split}.jsonl"
    once(P, out, args.split, "baselines", args.tag)
    claims = _judgeable(P, args.split, include_disputed=(args.split == "holdout"))
    text = _passage_text(P)
    ret = {r["qid"]: r["passage_ids"] for r in read_jsonl(P.jsonl("retrieval.jsonl"))}
    rows: list[dict] = []
    if args.tag == "lex":
        rows = [{"cid": c["cid"], "score": bl.lex_score(c["text"], [text[i] for i in ret[c["qid"]]]), "ok": True} for c in claims]
    elif args.tag == "emb":
        pv = load_vectors(P)
        cv = {r["cid"]: r["vec"] for r in read_jsonl(P.priv / "claim_vecs.jsonl")}
        rows = [{"cid": c["cid"], "score": bl.emb_score(cv[c["cid"]], [pv[i] for i in ret[c["qid"]]]), "ok": True} for c in claims]
    elif args.tag == "nli":
        pre = json.loads(P.prereg.read_text()).get("stage1") or {}
        rev = pre.get("nli_revision") if args.split == "holdout" else None
        rev = rev or nli_revision()
        pairs = [(text[i], c["text"]) for c in claims for i in ret[c["qid"]]]
        per = split_by_counts(bl.nli_entailment(pairs, rev), [len(ret[c["qid"]]) for c in claims])
        rows = [{"cid": c["cid"], "score": max(v), "ok": True, "revision": rev} for c, v in zip(claims, per)]
    elif args.tag == "llm":
        base = os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434")

        async def run():
            res = []
            async with httpx.AsyncClient(timeout=600) as cl:
                for c in claims:
                    r = await cl.post(f"{base}/api/chat", json=bl.llm_payload(c["text"], [text[i] for i in ret[c["qid"]]], GEN_MODEL))
                    sc = bl.parse_llm_json(r.json().get("message", {}).get("content", ""))
                    res.append({"cid": c["cid"], "score": sc if sc is not None else 0.0, "ok": sc is not None})
            return res

        rows = asyncio.run(run())
    else:
        raise SystemExit("--tag must be lex|emb|nli|llm")
    write_jsonl(out, rows)
    log_attempt(P, "baselines", tag=args.tag, split=args.split, claims=len(rows), failed=sum(not r["ok"] for r in rows))
    print(f"{len(rows)} {args.tag} scores")


def cmd_stage1_freeze_config(P: Paths, args) -> None:
    """stage1-tune 결과를 사전등록 stage1 블록으로 동결한다(홀드아웃 데이터 생성이 열린다). 덮어쓰기 금지.

    MDE가 0.10을 넘으면 홀드아웃 질문 수를 10개로 늘렸거나 탐색적 평가로 낮춘 경우에만 동결한다(spec 6절).
    """
    pre = json.loads(P.prereg.read_text())
    if pre.get("stage1"):
        raise SystemExit("stage1 block already frozen")
    tune = json.loads((P.ev / "results/stage1-tune.json").read_text())
    m = tune.get("mde")
    if m is None or (m > 0.10 and tune.get("holdout_questions_per_company") == 6 and not tune.get("exploratory")):
        raise SystemExit(f"MDE {m} > 0.10 needs more holdout questions or an explicit exploratory flag")
    pre["stage1"] = tune
    pre["version"] = max(2, pre.get("version", 1))
    P.prereg.write_text(json.dumps(pre, ensure_ascii=False, indent=2) + "\n")
    log_attempt(P, "stage1-freeze-config")
    print("stage1 frozen")


COMMANDS.update({"claim-embed": cmd_claim_embed, "baselines": cmd_baselines,
                 "stage1-freeze-config": cmd_stage1_freeze_config})


BASELINES = ("lex", "emb", "nli", "llm")


def delta_stat(b_star: str):
    """같은 표본에서 AUC(SYS) − AUC(B*). 한 종류뿐이면 None."""
    from lab.evidence.metrics import auc

    def stat(rows):
        a = auc([r["y"] for r in rows], [r["sys"] for r in rows])
        b = auc([r["y"] for r in rows], [r[b_star] for r in rows])
        return None if a is None or b is None else a - b
    return stat


def score_rows(P: Paths, split_name: str, tau_s: float, tau_c: float, natural_only: bool = True,
               claims: list[dict] | None = None) -> list[dict]:
    """판정 가능한 주장마다 SYS·JEV·기준선 점수와 라벨을 모은다."""
    from app.services.evidence.judge import Judgement, sys_decision
    from app.services.evidence.numbers import number_check, parse

    cluster = {c["corp_code"]: c["cluster"] for c in load_split(P)["companies"]}
    text = _passage_text(P)
    ret = {r["qid"]: r["passage_ids"] for r in read_jsonl(P.jsonl("retrieval.jsonl"))}
    jev = _scores(P, split_name)
    base = {t: {r["cid"]: r["score"] for r in read_jsonl(P.priv / "baselines" / f"{t}_{split_name}.jsonl")} for t in BASELINES}
    rows = []
    claims = _judgeable(P, split_name) if claims is None else claims
    if split_name == "holdout":
        missing = [c["cid"] for c in claims if c["cid"] not in jev]
        if missing:
            raise SystemExit(f"{len(missing)} holdout claims lack JEV scores")
    for c in claims:
        if (natural_only and c["source"] != "natural") or c["cid"] not in jev:
            continue
        r = jev[c["cid"]]
        ps = [text[i] for i in ret[c["qid"]]]
        valid = [number_check(c["text"], p) for p in ps]
        dec, idx, sys_score = sys_decision(Judgement(r["s"], r["c"], r["ok"], 0), valid, tau_s, tau_c)
        rows.append({"cid": c["cid"], "qid": c["qid"], "cluster": cluster[c["qid"].split("-q")[0]],
                     "label": c["label"], "y": int(c["label"] == "supported"), "sys": sys_score,
                     "jev": max(r["s"]) if r["ok"] else 0.0, "has_number": bool(parse(c["text"])),
                     "decision": dec, "source": c["source"], "variant": c.get("variant"),
                     "expected": c.get("expected"), **{t: base[t].get(c["cid"]) for t in BASELINES}})
    return rows


def _judge_rows_for_tuning(P: Paths, split_name: str) -> list[dict]:
    from app.services.evidence.numbers import number_check

    text = _passage_text(P)
    ret = {r["qid"]: r["passage_ids"] for r in read_jsonl(P.jsonl("retrieval.jsonl"))}
    jev = _scores(P, split_name)
    return [{"s": jev[c["cid"]]["s"], "c": jev[c["cid"]]["c"], "label": c["label"],
             "valid": [number_check(c["text"], text[i]) for i in ret[c["qid"]]]}
            for c in _judgeable(P, split_name) if c["source"] == "natural" and c["cid"] in jev and jev[c["cid"]]["ok"]]


def _op_point(rows: list[dict]) -> dict:
    """지지됨 정밀도·재현율·커버리지와 반박 오탐률(정답이 반박 아님인데 반박 판정)."""
    sup = [r for r in rows if r["decision"] == "supported"]
    tp = sum(r["y"] for r in sup)
    pos = sum(r["y"] for r in rows)
    neg_c = [r for r in rows if r["label"] != "contradicted"]
    return {"supported_precision": tp / len(sup) if sup else None, "supported_recall": tp / pos if pos else None,
            "coverage": len(sup) / len(rows) if rows else None,
            "contradiction_false_positive_rate": (sum(r["decision"] == "contradicted" for r in neg_c) / len(neg_c)) if neg_c else None}


def cmd_stage1_tune(P: Paths, args) -> None:
    """τ_c→τ_s(조정 자연), B*(조정 자연 AUC), MDE(개발 자연, 홀드아웃 군집 수), 확인 세트 운영점."""
    from lab.evidence import thresholds as th
    from lab.evidence.metrics import auc, mde

    tune = _judge_rows_for_tuning(P, "tune")
    tau_c = th.choose_tau_c(tune)
    tau_s = th.choose_tau_s(tune, tau_c)
    t_rows = score_rows(P, "tune", tau_s, tau_c)
    miss = missing_baselines({t: [r[t] for r in t_rows] for t in BASELINES}, len(t_rows))
    if miss:
        raise SystemExit(f"baselines missing on tune: {miss}")
    base_auc = {t: auc([r["y"] for r in t_rows], [r[t] for r in t_rows]) for t in BASELINES}
    b_star = max(base_auc, key=base_auc.get)
    dev = t_rows + score_rows(P, "check", tau_s, tau_c)
    n_hold = len({c["cluster"] for c in load_split(P)["companies"] if c["split"] == "holdout"})
    m = mde(dev, n_hold, delta_stat(b_star))
    c_rows = score_rows(P, "check", tau_s, tau_c)
    result = {"tau_c": tau_c, "tau_s": tau_s, "baseline_auc_tune": base_auc, "b_star": b_star,
              "sys_auc_tune": auc([r["y"] for r in t_rows], [r["sys"] for r in t_rows]),
              "mde": m, "holdout_clusters": n_hold,
              "holdout_questions_per_company": 6 if (m is not None and m <= 0.10) else 10,
              "exploratory": False, "check_operating_point": _op_point(c_rows),
              "check_sys_auc": auc([r["y"] for r in c_rows], [r["sys"] for r in c_rows]),
              "nli_revision": next((r.get("revision") for r in read_jsonl(P.priv / "baselines/nli_tune.jsonl")), None),
              "llm_prompt": __import__("lab.evidence.baselines", fromlist=["LLM_SYSTEM"]).LLM_SYSTEM}
    out = P.ev / "results/stage1-tune.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n")
    log_attempt(P, "stage1-tune", tau_c=tau_c, tau_s=tau_s, b_star=b_star, mde=m)
    print(json.dumps({k: result[k] for k in ("tau_c", "tau_s", "b_star", "mde", "holdout_questions_per_company")}))


def _sha_rows(rows: list[dict]) -> str:
    return hashlib.sha256("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in rows).encode()).hexdigest()


def cmd_freeze_holdout(P: Paths, args) -> None:
    """봉인 분할 동결 묶음(A-1 spec 7절 5단계, A-2 spec 6.4절 5단계)을 쓴다(판정 봉인 해제). 덮어쓰기·미커밋 변경 금지."""
    if P.prereg_holdout.exists():
        raise SystemExit(f"{P.prereg_holdout.name} already exists")
    if _tree_dirty(P):
        raise SystemExit("commit app/ and lab/evidence/ changes before freezing")
    codes = {c["corp_code"] for c in companies(P, P.sealed, stage="data")}
    if not [r for r in read_jsonl(P.jsonl("labels.jsonl")) if r["cid"].split("-q")[0] in codes]:
        raise SystemExit(f"no {P.sealed} labels")
    pre = json.loads(P.prereg.read_text())
    if P.study in ("a3", "a4"):
        from lab.evidence import a3

        k = r1_binary_kappa([(r["r1"]["opus"]["label"], r["r1"]["codex"]["label"])
                             for r in read_jsonl(P.jsonl("labels.jsonl")) if r["cid"].split("-q")[0] in codes])
        if k is None or not k >= a3.KAPPA_MIN:  # 한 종류 라벨뿐이면 κ가 nan이다
            log_attempt(P, "freeze-stop", reason="kappa", kappa=None if k is None or k != k else k)
            raise SystemExit(f"stop: label kappa {k} < {a3.KAPPA_MIN}")
    if P.study == "a4":
        _check_swap_tags(P, codes)
    labelers = (pre.get("stage1", {}) if P.study == "a1" else pre.get("labels", {})).get("labelers")
    gen_model, gen_digest = _generator(P)
    data = {**freeze_bundle(P),
            "models": {"jev": "jev-1.13.0", "generator": f"{gen_model}@{gen_digest}", "embedder": "nomic-embed-text@0a109f422b47",
                       "labelers": labelers},
            "code_commit": _git_head(P), "frozen_at": datetime.now(timezone.utc).isoformat()}
    P.prereg_holdout.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n")
    log_attempt(P, "freeze-holdout", claims=len([r for r in read_jsonl(P.jsonl("claims.jsonl")) if r["cid"].split("-q")[0] in codes]))
    print(f"{P.sealed} frozen")


def _sensitivity(P: Paths, cfg: dict, b: str, rows: list[dict]) -> dict:
    """spec 4·6절: 갈린 라벨을 각 라벨러 쪽으로 넣었을 때의 Δ, 제외율, 제외 사례 분포."""
    from lab.evidence.metrics import cluster_bootstrap

    lab = {r["cid"]: r for r in read_jsonl(P.jsonl("labels.jsonl"))}
    disputed = [c for c in _judgeable(P, "holdout", include_disputed=True)
                if c["label"] == "disputed" and c["source"] == "natural"]
    out = {"disputed_natural": len(disputed),
           "exclusion_rate": len(disputed) / (len(disputed) + len(rows)) if (disputed or rows) else None,
           "disputed_clusters": sorted({c["qid"].split("-q")[0] for c in disputed})}
    for lb in ("opus", "codex"):
        def final(cid):
            r = lab[cid]
            return ((r.get("r2") or {}).get(lb) or r["r1"][lb])["label"]
        extra = [dict(c, label=final(c["cid"])) for c in disputed if final(c["cid"]) != "non_claim"]
        both = rows + score_rows(P, "holdout", cfg["tau_s"], cfg["tau_c"], claims=extra)
        out[f"with_{lb}_labels"] = {"classes": {k: sum(e["label"] == k for e in extra) for k in ("supported", "contradicted", "no_evidence")},
                                    "delta": cluster_bootstrap(both, delta_stat(b))}
    return out


def cmd_stage1_report(P: Paths, args) -> None:
    """홀드아웃 주결과와 보조 지표를 계산한다(동결 뒤에만)."""
    from lab.evidence.metrics import auc, cluster_bootstrap, macro_f1, verdict

    cfg = json.loads(P.prereg.read_text())["stage1"]
    companies(P, "holdout")  # 동결 확인
    rows = score_rows(P, "holdout", cfg["tau_s"], cfg["tau_c"])
    b = cfg["b_star"]
    pos, neg = sum(r["y"] for r in rows), sum(1 - r["y"] for r in rows)
    point, lo, hi = cluster_bootstrap(rows, delta_stat(b))
    a = lambda key, rs=rows: auc([r["y"] for r in rs], [r[key] for r in rs])
    by_cluster = {}
    for r in rows:
        by_cluster.setdefault(r["cluster"], []).append(r)
    signs = [delta_stat(b)(rs) for rs in by_cluster.values()]
    nonum = [r for r in rows if not r["has_number"]]
    pred3 = [{"supported": "supported", "contradicted": "contradicted"}.get(r["decision"], "no_evidence") for r in rows]
    ctrl = score_rows(P, "holdout", cfg["tau_s"], cfg["tau_c"], natural_only=False)
    ctrl = [r for r in ctrl if r["source"] == "controlled"]
    by_var = {}
    for r in ctrl:
        by_var.setdefault(r["variant"], []).append((r["decision"] == "supported") == (r["expected"] == "supported"))
    res = {"n": len(rows), "supported": pos, "not_supported": neg, "descriptive_only": pos < 60 or neg < 60,
           "b_star": b, "auc": {k: a(k) for k in ("sys", "jev", *BASELINES) if all(r[k] is not None for r in rows)},
           "delta": {"point": point, "lo": lo, "hi": hi,
                     **reported_verdict(lo, hi, pos < 60 or neg < 60, cfg.get("exploratory", False))}, "mde": cfg.get("mde"),
           "exploratory": cfg.get("exploratory", False),
           "jev_minus_bstar": cluster_bootstrap(rows, lambda rs: (lambda x, y: None if x is None or y is None else x - y)(
               auc([r["y"] for r in rs], [r["jev"] for r in rs]), auc([r["y"] for r in rs], [r[b] for r in rs]))),
           "operating_point": _op_point(rows),
           "numberless_auc": {"sys": a("sys", nonum), b: a(b, nonum), "n": len(nonum)},
           "macro_f1_3class": macro_f1([r["label"] if r["label"] in ("supported", "contradicted") else "no_evidence" for r in rows],
                                       pred3, ("supported", "contradicted", "no_evidence")),
           "cluster_delta_signs": {"positive": sum(s is not None and s > 0 for s in signs),
                                   "negative": sum(s is not None and s < 0 for s in signs), "undefined": sum(s is None for s in signs)},
           "controlled_accuracy_by_variant": {k: sum(v) / len(v) for k, v in by_var.items()}}
    res["sensitivity"] = _sensitivity(P, cfg, b, rows)
    out = P.ev / "results/stage1.json"
    out.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str) + "\n")
    (P.ev / "results/stage1-gates.md").write_text(
        f"# Stage 1 주결과\n\nΔ = AUC(SYS) − AUC({b}) = {point} (95% {lo} ~ {hi}) → {res['delta']}\n\n"
        f"n={len(rows)} (지지됨 {pos}), descriptive_only={res['descriptive_only']}\n")
    log_attempt(P, "stage1-report", delta=point, lo=lo, hi=hi)
    print(json.dumps(res["delta"]))


COMMANDS.update({"stage1-tune": cmd_stage1_tune, "freeze-holdout": cmd_freeze_holdout,
                 "stage1-report": cmd_stage1_report})


def verify_prereg_code(P: Paths) -> None:
    """A-2 사전등록에 적힌 코드 파일 해시(1차 필터·비주장 규칙·숫자 확인)가 지금과 같은지 본다. 다르면 멈춘다."""
    pre = json.loads(P.prereg.read_text()).get("code_sha256")
    if not pre:
        raise SystemExit(f"{P.prereg.name} has no code_sha256")
    bad = sorted(rel for rel, sha in pre.items() if _file_sha(P.root / rel) != sha)
    if bad:
        raise SystemExit(f"code changed after {P.prereg.name}: {bad}")


def guard_tune_open(P: Paths) -> None:
    """조정(τ_s·θ 선택)은 확인 세트 데이터를 만들기 전에만 한다. 확인 세트를 본 뒤 다시 고르지 못하게."""
    codes = {c["corp_code"] for c in load_split(P)["companies"] if c["split"] == P.sealed}
    if P.prereg_holdout.exists() or any(q["corp_code"] in codes for q in read_jsonl(P.jsonl("questions.jsonl"))):
        raise SystemExit("tune is closed: check data exists")


def _a2_inputs(P: Paths):
    from app.services.evidence.lexical import lex_features
    from app.services.evidence.numbers import number_check

    text = _passage_text(P)
    ret = {r["qid"]: r["passage_ids"] for r in read_jsonl(P.jsonl("retrieval.jsonl"))}
    names = _corp_names(P)
    cluster = {c["corp_code"]: c["cluster"] for c in load_split(P)["companies"]}
    selves = self_names(P)
    dic = company_names(P) if P.study == "a3" else None

    def feats(c: dict) -> dict:
        corp = c["qid"].split("-q")[0]
        ps = [text[i] for i in ret[c["qid"]]]
        f = lex_features(c["text"], ps, names[corp])
        row = {"cid": c["cid"], "qid": c["qid"], "cluster": cluster[corp], "lex": f.lex, "high_ok": f.high_ok,
               "valid": [number_check(c["text"], p) for p in ps]}
        if P.study == "a3":  # 주체 확인(제품과 같은 subject_valid)과 감사용 못 찾은 이름. 자기 회사는 DART 별칭까지
            from app.services.evidence import subject

            me, alias = selves[corp], subject.aliases(ps)
            row.update(best=f.best, text=c["text"], subj=subject.subject_valid(c["text"], ps, me, names=dic),
                       missing=[subject.missing_subjects(c["text"], p, me, alias, names=dic) for p in ps])
        return row
    return feats


def a2_rows(P: Paths, split_name: str, source: str = "natural") -> tuple[list[dict], int]:
    """판정 가능한 주장(라벨·JEV 확률·제품 1차 필터 입력)과 JEV 실패로 뺀 수.

    자연 주장은 하나라도 점수가 없으면 멈춘다(--limit 스모크·잘못된 --tag의 부분 점수로 조용히 폴백하지 않게).
    통제 주장은 예산 규칙으로 뺐을 수 있으므로 점수가 있는 것만 쓴다.
    """
    feats = _a2_inputs(P)
    jev = _scores(P, split_name)
    claims = [c for c in _judgeable(P, split_name) if c["source"] == source]
    missing = [c["cid"] for c in claims if c["cid"] not in jev]
    if missing and source == "natural":
        raise SystemExit(f"{len(missing)} {split_name} claims lack JEV scores (scores/{split_name}.jsonl)")
    rows, failed = [], 0
    for c in claims:
        if c["cid"] not in jev:
            continue
        if not jev[c["cid"]]["ok"]:
            failed += 1
            continue
        rows.append({**feats(c), "s": jev[c["cid"]]["s"], "c": jev[c["cid"]]["c"], "label": c["label"],
                     "y": int(c["label"] == "supported"), "variant": c.get("variant"), "expected": c.get("expected")})
    return rows, failed


def a2_pool(P: Paths, split_name: str) -> list[dict]:
    """JEV 호출 감소율의 분모: 규칙을 통과한 자연 주장 전체(라벨과 무관)의 1차 필터 입력."""
    feats = _a2_inputs(P)
    codes = {c["corp_code"] for c in companies(P, split_name)}
    return [feats(c) for c in read_jsonl(P.jsonl("claims.jsonl"))
            if c["source"] == "natural" and not c.get("not_claim_rule") and c["qid"].split("-q")[0] in codes]


def _r1_kappa(P: Paths, split_name: str) -> float | None:
    """분할의 1차 라벨 이진 κ(labels-merge와 같은 r1_binary_kappa)."""
    codes = {c["corp_code"] for c in companies(P, split_name)}
    return r1_binary_kappa([(r["r1"]["opus"]["label"], r["r1"]["codex"]["label"])
                            for r in read_jsonl(P.jsonl("labels.jsonl")) if r["cid"].split("-q")[0] in codes])


def _labeled_natural(P: Paths, split_name: str) -> list[dict]:
    codes = {c["corp_code"] for c in companies(P, split_name)}
    lab = {r["cid"]: r["label"] for r in read_jsonl(P.jsonl("labels.jsonl"))}
    return [dict(c, label=lab.get(c["cid"])) for c in read_jsonl(P.jsonl("claims.jsonl"))
            if c["source"] == "natural" and c["qid"].split("-q")[0] in codes]


def _latency_by_split(P: Paths) -> dict:
    """생성 지연을 분할별로 따로 요약한다(조정·확인을 섞지 않는다)."""
    from lab.evidence import a2

    split_of = {c["corp_code"]: c["split"] for c in load_split(P)["companies"]}
    answers = read_jsonl(P.jsonl("answers.jsonl"))
    return {s: a2.latency_summary([a for a in answers if split_of.get(a["qid"].split("-q")[0]) == s])
            for s in ("tune", P.sealed)}


def cmd_a2_tune(P: Paths, args) -> None:
    """A-2 spec 6.3: 조정 세트에서 τ_s(τ_c 0.35 고정)·θ_low·θ_high를 고른다. 확인 세트 데이터가 생기면 거부."""
    from lab.evidence import a2

    guard_tune_open(P)
    verify_prereg_code(P)
    k = _r1_kappa(P, "tune")
    if k is None or k < a2.KAPPA_MIN:
        log_attempt(P, "a2-tune-stop", reason="kappa", kappa=k)
        raise SystemExit(f"stop: tune label kappa {k} < {a2.KAPPA_MIN}")
    rows, failed = a2_rows(P, "tune")
    tau = a2.choose_tau_s(rows)
    low, high = a2.choose_theta_low(rows), a2.choose_theta_high(rows)
    cfg = {"tau_s": tau["tau_s"], "tau_c": a2.TAU_C, "theta_low": low["theta_low"], "theta_high": high["theta_high"]}
    d = a2.decide(rows, cfg)
    result = {**cfg, "tau_s_fallback": tau["fallback"], "n": len(rows), "jev_failed_excluded": failed,
              "supported_labels": sum(r["y"] for r in rows), "kappa_r1": k,
              "boundary_share": a2.boundary_share(rows, cfg["tau_s"]),
              "operating_point": _op_point([dict(r, decision=r["sys_decision"]) for r in d]),
              "tau_s_candidates": tau["candidates"], "theta_low_candidates": low["candidates"],
              "theta_high_candidates": high["candidates"],
              "not_claim_audit": a2.not_claim_audit(_labeled_natural(P, "tune")),
              "generation_latency": _latency_by_split(P)["tune"],
              "note": "θ는 AI 참조 라벨과 어휘 점수로만 골랐다(MCA 2.3(b)). 확인 세트 결과를 보고 다시 고르지 않는다."}
    P.tune_json.parent.mkdir(parents=True, exist_ok=True)
    P.tune_json.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n")
    log_attempt(P, "a2-tune", **cfg, tau_s_fallback=tau["fallback"], n=len(rows))
    print(json.dumps(cfg))


def _num(v) -> str:
    return "—" if v is None else (f"{v:.3f}" if isinstance(v, float) else str(v))


def _passfail(b: bool) -> str:
    return "통과" if b else "실패"


def _gate_lines(g: dict) -> list[str]:
    hp, hl, hh, ht = g["h_prec"], g["h_low"], g["h_high"], g["h_tier"]
    prec = f"✅ 예측 {hp['predicted']}건, 정밀도 {_num(hp['precision'])} (95% {_num(hp['ci'][0])}~{_num(hp['ci'][1])})"
    if hp["descriptive_only"]:
        prec += ", 기술 통계만(✅ 예측 150건 미만)"
    low = "미시험(조정 세트에서 θ_low 없음)" if hl["status"] == "not_tested" else (
        f"하단 {hl['band']}건, 지지됨 비율 {_num(hl['supported_rate'])}, 전체 대비 {_num(hl['share'])}")
    high = "미시험(조정 세트에서 θ_high 없음)" if hh["status"] == "not_tested" else (
        f"판정 불가(확인 세트 상단 구간 {hh['band']}건 < 30) → 미채택" if hh["status"] == "insufficient" else
        f"상단 {hh['band']}건, 정밀도 {_num(hh['precision'])}, 상단 적용 ✅ 정밀도 {_num(hh['tier_precision'])} "
        f"대 SYS {_num(hh['sys_precision'])}")
    if ht["status"] == "not_applicable":
        tier = "해당 없음(채택된 구간 없음)"
    else:
        a = ht["auc_diff"]
        tier = (f"구간 {'+'.join(ht['adopted'])}, AUC 차이 {_num(a['point'])} (95% {_num(a['lo'])}~{_num(a['hi'])}), "
                f"JEV 호출 감소 {_num(ht['call_reduction'])}")
    return ["| 가설 | 값 | 결과 |", "|---|---|---|", f"| H-prec | {prec} | {_passfail(hp['pass'])} |",
            f"| H-low | {low} | {_passfail(hl['pass'])} |", f"| H-high | {high} | {_passfail(hh['pass'])} |",
            f"| H-tier | {tier} | {_passfail(ht['pass'])} |"]


def report_md(res: dict) -> str:
    """A-2 리포트 본문(AI 생성 표시, 금액 없음)."""
    cfg, pol, lat, nc = res["tuned"], res["policy_a2_v1"], res["generation_latency"], res["not_claim_audit"]
    sl = res["sys_minus_lex"]
    lines = ["# 근거 판정 A-2 평가 리포트(확인 세트 1회 실행)", "",
             "> 이 리포트는 `python -m lab.evidence --study a2 a2-report`가 자동 생성했다. 정답 라벨은 사람이 아니라 "
             "AI(Claude Opus·Codex)가 만든 **AI 참조 라벨**이며, 수치는 그 라벨과의 일치 성능이다. "
             "판정에는 TypeSafe의 JEV 모델을 썼고, 이 프로젝트는 TypeSafe와 제휴 관계가 아니다.", "",
             f"- 사전등록: `lab/evidence/prereg_a2.json`, 동결: `lab/evidence/prereg_a2_check.json`, 원자료: "
             "`lab/evidence/results/a2-check.json`",
             f"- 조정 세트에서 고른 값: τ_s {_num(cfg['tau_s'])}{' (폴백)' if cfg.get('tau_s_fallback') else ''}, "
             f"τ_c {_num(cfg['tau_c'])}, θ_low {_num(cfg['theta_low'])}, θ_high {_num(cfg['theta_high'])}",
             f"- 확인 세트 판정 가능 자연 주장 {res['n']}건(AI 라벨 지지됨 {res['supported_labels']}건), "
             f"JEV 실패로 제외 {res['jev_failed_excluded']}건, τ_s ± 0.05 경계 비율 {_num(res['boundary_share'])}", "",
             "## 관문", "", *_gate_lines(res["gates"]), "",
             "## 제품 정책 a2-v1(사전등록 반영 규칙 표에 따름)", "",
             f"- τ_s {_num(pol['tau_s'])}, τ_c {_num(pol['tau_c'])}, θ_low {_num(pol['theta_low'])}, "
             f"θ_high {_num(pol['theta_high'])}",
             "- 정밀도 목표 확인됨" if pol["precision_target_confirmed"] else
             "- **정밀도 목표 미확인** — τ_s 0.85 고정, 배지 툴팁에 \"확신도 기준을 보수적으로 둔 시험 운영\"을 덧붙인다", "",
             "## 보조 결과", "",
             f"- SYS − 어휘 겹침 AUC: {_num(sl['point'])} (95% {_num(sl['lo'])}~{_num(sl['hi'])}), "
             f"판정 {sl['verdict']['statistical']}, 실용적 동등 {sl['verdict']['practically_equivalent']}",
             f"- SYS 운영점: {json.dumps(res['operating_point'], ensure_ascii=False)}",
             f"- 통제 주장 변형 유형별 정확도: {json.dumps(res['controlled_accuracy_by_variant'], ensure_ascii=False)}",
             f"- 비주장 규칙: 걸린 문장 {nc['flagged']}건 중 AI 라벨이 주장 {nc['claim']}건(비율 {_num(nc['claim_share'])}), "
             f"규칙에서 뺄 표현 {nc['drop_phrases'] or '없음'}",
             *[f"- 생성 지연 {name}(서비스 생성기, 첫 호출 제외 {v['n']}건): p50 {_num(v['p50_ms'])}ms, "
               f"p95 {_num(v['p95_ms'])}ms → 타임아웃 재설정값(p95 × 2) {_num(v['timeout_s'])}초"
               for name, v in lat.items()], ""]
    return "\n".join(lines)


def cmd_a2_report(P: Paths, args) -> None:
    """확인 세트(동결 뒤 1회 판정 결과)로 관문·정책 a2-v1·보조 지표를 계산해 결과 JSON과 리포트를 쓴다. 추가 호출 없음."""
    from lab.evidence import a2
    from lab.evidence.metrics import auc, cluster_bootstrap

    companies(P, P.sealed)  # 동결 확인
    verify_prereg_code(P)
    cfg = json.loads(P.tune_json.read_text())
    rows, failed = a2_rows(P, P.sealed)
    g = a2.check_gates(rows, a2_pool(P, P.sealed), cfg)
    d = a2.decide(rows, cfg)
    sl = cluster_bootstrap(d, lambda rs: (lambda x, y: None if x is None or y is None else x - y)(
        auc([r["y"] for r in rs], [r["sys_score"] for r in rs]), auc([r["y"] for r in rs], [r["lex"] for r in rs])),
        n=a2.BOOTSTRAP_N, seed=a2.SEED)
    ctrl, _ = a2_rows(P, P.sealed, source="controlled")
    by_var: dict[str, list[bool]] = {}
    for r in a2.decide(ctrl, cfg):
        by_var.setdefault(r["variant"], []).append((r["sys_decision"] == "supported") == (r["expected"] == "supported"))
    codes = {c["corp_code"] for c in companies(P, P.sealed)}
    res = {"tuned": {k: cfg.get(k) for k in ("tau_s", "tau_c", "theta_low", "theta_high", "tau_s_fallback")},
           "n": len(rows), "supported_labels": sum(r["y"] for r in rows), "jev_failed_excluded": failed,
           "gates": g, "policy_a2_v1": a2.policy_a2_v1(g, cfg),
           "boundary_share": a2.boundary_share(rows, cfg["tau_s"]),
           "operating_point": _op_point([dict(r, decision=r["sys_decision"]) for r in d]),
           "sys_minus_lex": {"point": sl[0], "lo": sl[1], "hi": sl[2], "verdict": reported_verdict(sl[1], sl[2], False, False)},
           "controlled_accuracy_by_variant": {k: sum(v) / len(v) for k, v in sorted(by_var.items())},
           "not_claim_audit": a2.not_claim_audit(_labeled_natural(P, P.sealed)),
           "generation_latency": _latency_by_split(P),
           "check_answers": sum(a["qid"].split("-q")[0] in codes for a in read_jsonl(P.jsonl("answers.jsonl")))}
    out = P.ev / "results/a2-check.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str) + "\n")
    doc = P.root / "docs/lab/evidence-a2-report.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(report_md(res))
    log_attempt(P, "a2-report", policy=res["policy_a2_v1"], h_prec=g["h_prec"]["pass"], h_low=g["h_low"]["pass"],
                h_high=g["h_high"]["pass"], h_tier=g["h_tier"]["pass"])
    print(json.dumps(res["policy_a2_v1"]))


COMMANDS.update({"a2-tune": cmd_a2_tune, "a2-report": cmd_a2_report})


def _pct(v) -> str:
    return "—" if v is None else f"{v:.3f}"


def _ci(ci) -> str:
    return f"95% {_pct(ci[0])}~{_pct(ci[1])}"


def a3_report_md(res: dict) -> str:
    """A-3 리포트 본문(AI 생성 표시, 금액 없음)."""
    g, ctl, rec = res["gates"], res["controlled"], res["recommendation"]
    sw, rc, pr = g["h_swap"], g["h_recall"], g["h_prec"]
    nf = ctl["notation_false_negative"]
    lines = ["# 근거 판정 A-3 평가 리포트(주체 확인, 확인 세트 1회 실행)", "",
             "> 이 리포트는 `python -m lab.evidence --study a3 a3-report`가 자동 생성했다. 정답 라벨은 사람이 아니라 "
             "AI(Claude Opus·Codex)가 만든 **AI 참조 라벨**이며, 통제 주장 기대값은 AI 작성자가 만든 변형에서 왔다. "
             "판정에는 TypeSafe의 JEV 모델을 썼고, 이 프로젝트는 TypeSafe와 제휴 관계가 아니다.", "",
             "- 사전등록: `lab/evidence/prereg_a3.json`, 동결: `lab/evidence/prereg_a3_check.json`, 원자료: "
             "`lab/evidence/results/a3-check.json`",
             f"- 비교: 같은 JEV 확률 위에서 a2-v1 대 a3-subject-exp(주체 확인만 다름). 자연 주장 {res['n_natural']}건"
             f"(AI 라벨 지지됨 {rc['positive']}건), 통제 주체 교체 {sw['n']}건, JEV 실패 제외 {res['jev_failed_excluded']}건",
             "", "## 관문", "", "| 가설 | 값 | 결과 |", "|---|---|---|",
             f"| H-swap | 주체 교체 정확도 a3 {_pct(sw['exp_accuracy'])} ({_ci(sw['ci'])}) 대 a2-v1 "
             f"{_pct(sw['base_accuracy'])}, 차이 {_pct(sw['diff']['point'])} "
             f"(95% {_pct(sw['diff']['lo'])}~{_pct(sw['diff']['hi'])})"
             f"{', 기술 통계만(150건 미만)' if sw['descriptive_only'] else ''} | {'통과' if sw['pass'] else '실패'} |",
             f"| H-recall | ✅ 재현율 a2-v1 {_pct(rc['base_recall'])} → a3 {_pct(rc['exp_recall'])}, 손실 {_pct(rc['loss'])} "
             f"({_ci(rc['ci'])}) | {'통과' if rc['pass'] else '실패'} |",
             f"| H-prec | ✅ 예측 {pr['predicted']}건, 정밀도 {_pct(pr['precision'])} ({_ci(pr['ci'])}), a2-v1 "
             f"{pr['base_predicted']}건 {_pct(pr['base_precision'])}, 차이 {_pct(pr['diff']['point'])} "
             f"(95% {_pct(pr['diff']['lo'])}~{_pct(pr['diff']['hi'])})"
             f"{', 기술 통계만(150건 미만)' if pr['descriptive_only'] else ''} | {'통과' if pr['pass'] else '실패'} |",
             "", "## 권고(사전등록 규칙)", "", f"- {rec['note']}", "", "## 보조 결과", "",
             *[f"- H-swap 하위 유형 {k}: {v['n']}건, 정확도 a2-v1 {_pct(v['base_accuracy'])} → a3 {_pct(v['exp_accuracy'])}"
               for k, v in sw["by_subtype"].items()],
             f"- 문단 안 교체(관문 밖, 주체 확인이 원리상 못 잡음) {g['in_passage_swap']['n']}건: 정확도 a2-v1 "
             f"{_pct(g['in_passage_swap']['base_accuracy'])}, a3 {_pct(g['in_passage_swap']['exp_accuracy'])}",
             f"- 표기 변형 오탐(a2-v1 ✅인 c3 중 a3에서 ✅ 아님): {nf['lost']}/{nf['n_base_supported']}"
             f" (비율 {_pct(nf['rate'])}), 유형별 {json.dumps(nf['by_type'], ensure_ascii=False)}",
             f"- 통제 주장 변형별 정확도 a2-v1: {json.dumps(ctl['accuracy']['base'], ensure_ascii=False)}",
             f"- 통제 주장 변형별 정확도 a3: {json.dumps(ctl['accuracy']['exp'], ensure_ascii=False)}",
             f"- 자연 주장 AUC a2-v1 {_pct(res['auc']['base'])}, a3 {_pct(res['auc']['exp'])}",
             f"- a2-v1 ✅ → a3 ❔로 바뀐 자연 주장 {len(res['removed'])}건(목록과 못 찾은 이름은 원자료 removed)", ""]
    return "\n".join(lines)


def cmd_a3_report(P: Paths, args) -> None:
    """확인 세트(동결 뒤 1회 판정 결과)로 a2-v1 대 a3-subject-exp 관문·보조 지표를 계산한다. 추가 호출 없음."""
    from lab.evidence import a3
    from lab.evidence.metrics import auc

    companies(P, P.sealed)  # 동결 확인
    verify_prereg_code(P)
    pre = json.loads(P.prereg.read_text())
    if pre.get("status") != "registered":
        raise SystemExit("a3-report needs prereg_a3.json registered")
    want = {"base": a3.policy_fields(a3.BASE), "exp": a3.policy_fields(a3.EXP)}
    if pre.get("policies") != want:
        raise SystemExit(f"policy constants differ from prereg_a3.json: {want}")
    natural, failed = a2_rows(P, P.sealed)
    ctrl, _ = a2_rows(P, P.sealed, source="controlled")
    swaps = [r for r in ctrl if (r["variant"] or "").startswith("주체 교체")]
    g = a3.check_gates(natural, swaps)
    d = a3.annotate(natural)
    y = [r["y"] for r in d]
    res = {"policies": want, "n_natural": len(natural), "jev_failed_excluded": failed, "gates": g,
           "recommendation": a3.recommendation(g), "controlled": a3.controlled_summary(ctrl),
           "auc": {k: auc(y, [r[f"{k}_score"] for r in d]) for k in ("base", "exp")},
           "removed": a3.removed_audit(natural)}
    out = P.ev / "results/a3-check.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str) + "\n")
    doc = P.root / "docs/lab/evidence-a3-report.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(a3_report_md(res))
    log_attempt(P, "a3-report", h_swap=g["h_swap"]["pass"], h_recall=g["h_recall"]["pass"], h_prec=g["h_prec"]["pass"],
                switch_default=res["recommendation"]["switch_default"])
    print(json.dumps({k: g[k]["pass"] for k in ("h_swap", "h_recall", "h_prec")}))


def cmd_a3_explore(P: Paths, args) -> None:
    """A-3 사전등록 확정 전 탐색(prereg_a3.json order 0): A-2 조정 세트(탐색 허용)의 저장된 JEV 확률로 a2-v1 대
    a3-subject-exp를 비교하고, 재현율 손실률(AI 라벨 지지됨이고 a2-v1 ✅인 것 중 a3가 빼는 비율)·진행 여부(≤ 1.5%)와
    빠지는 자연 주장·못 찾은 이름을 비공개 파일에 쓴다. 추가 호출 없음.
    A-2 확인 세트는 읽지 않는다(조정 세트만 연다). 확정(registered) 뒤에는 거부한다."""
    from app.services.evidence import subject
    from app.services.evidence.lexical import lex_best
    from lab.evidence import a3

    if prereg_registered(P):
        raise SystemExit("a3-explore is for the draft prereg only (prereg_a3.json is registered)")
    A2 = Paths(P.root, "a2")
    text = _passage_text(A2)
    ret = {r["qid"]: r["passage_ids"] for r in read_jsonl(A2.jsonl("retrieval.jsonl"))}
    names = self_names(A2)  # A-2 추첨 행에는 DART 별칭이 없어 회사명만(A-3보다 보수적)
    dic = company_names(A2)  # A-2 추첨 때 받은 corpCode.xml(lab/data/evidence_a2)
    rows, failed = a2_rows(A2, "tune")
    claim_text = {c["cid"]: c["text"] for c in _judgeable(A2, "tune")}
    for r in rows:
        ps, co, claim = [text[i] for i in ret[r["qid"]]], names[r["qid"].split("-q")[0]], claim_text[r["cid"]]
        alias = subject.aliases(ps)
        r.update(best=lex_best(claim, ps)[1], text=claim, subj=subject.subject_valid(claim, ps, co, names=dic),
                 missing=[subject.missing_subjects(claim, p, co, alias, names=dic) for p in ps])
    summary = a3.explore_summary(rows)
    removed = a3.removed_audit(rows)
    out = {"split": "tune (A-2)", "n": len(rows), "jev_failed_excluded": failed, **summary,
           "removed": removed, "generic_extra": list(subject.SUBJECT_GENERIC)}
    path = P.priv / "a3_explore_tune.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n")
    log_attempt(P, "a3-explore", source="a2-tune", n=len(rows), **summary, removed=len(removed))
    print(json.dumps({k: out[k] for k in ("n", "positive", "base_supported_true", "lost", "loss_rate", "proceed")}))


COMMANDS.update({"a3-report": cmd_a3_report, "a3-explore": cmd_a3_explore})


# --- A-4(--study a4) --------------------------------------------------------------------------------------------
def _jev_client(path: Path, cap: int):
    """JEV 클라이언트(호출 기록 = 캐시·토큰 원장). 테스트는 가짜 전송으로 바꾼다."""
    from app.lib.jev import JevClient

    return JevClient(path, cap)


def _a4_feats(S: Paths):
    """연구 S(A-2·A-3·A-4)의 주장 → 제품 1차 필터 입력·숫자 확인·①c 코드 마스크(팔 c·a·a3x0·a3). 자기 회사는 S 추첨 행의
    별칭(A-2는 회사명만), 상장사 이름 사전은 S 추첨 때 받은 corpCode.xml."""
    from app.services.evidence import subject, subject_a4
    from app.services.evidence.lexical import lex_features
    from app.services.evidence.numbers import number_check

    text = _passage_text(S)
    ret = {r["qid"]: r["passage_ids"] for r in read_jsonl(S.jsonl("retrieval.jsonl"))}
    names = _corp_names(S)
    cluster = {c["corp_code"]: c["cluster"] for c in load_split(S)["companies"]}
    selves = self_names(S)
    dic = company_names(S)

    def feats(c: dict) -> dict:
        corp = c["qid"].split("-q")[0]
        ps = [text[i] for i in ret[c["qid"]]]
        f = lex_features(c["text"], ps, names[corp])
        me = selves[corp]
        code = {arm: subject_a4.code_valid(c["text"], ps, me, names=dic, arm=arm) for arm in ("c", "a", "a3x0")}
        code["a3"] = subject.subject_valid(c["text"], ps, me, names=dic)
        return {"cid": c["cid"], "qid": c["qid"], "cluster": f"{S.study}:{cluster[corp]}", "lex": f.lex,
                "high_ok": f.high_ok, "best": f.best, "valid": [number_check(c["text"], p) for p in ps],
                "code": code, "text": c["text"], "company": names[corp], "passages": ps}
    return feats


_JUDGE_SKIP = {None, "non_claim", "disputed"}


def _a4_halves(P: Paths) -> tuple[list[list[str]], list[list[str]]]:
    """A-3 확인 세트 반분(split_a3.json 군집 목록). 원장에 첫 반분이 있으면 같은지 대조한다."""
    from lab.evidence import a4

    A3 = Paths(P.root, "a3")
    split = load_split(A3)
    check = {c["corp_code"] for c in split["companies"] if c["split"] == "check"}
    clusters = [cl for cl in split["clusters"] if set(cl) <= check]
    design, rest = a4.split_halves(clusters)
    prior = [r for r in read_jsonl(P.attempts) if r.get("event") == "a4-half-split" and r.get("study") == "a4"]
    if prior and (prior[0]["design"] != design or prior[0]["check"] != rest):
        raise SystemExit("A-3 half split differs from the one in attempts.jsonl")
    return design, rest


def _a4_explore_rows(P: Paths) -> tuple[list[dict], dict, dict]:
    """탐색 데이터(읽기만): A-2 조정 세트 자연 주장 + A-3 확인 세트 판정 대상 전부(자연·통제), 저장된 주 판정 확률.
    A-3 동결 검증(companies)을 거치지 않는다 — HEAD에서는 코드 해시가 바뀌어 동결 대조가 깨지는 것이 정상이고, 여기서는 결론 난
    A-3 데이터를 읽기만 한다. A-3 결과(results/a3-check.json)가 있어야 연다. A-2 확인 세트는 읽지 않는다."""
    A2, A3 = Paths(P.root, "a2"), Paths(P.root, "a3")
    if not (A3.ev / "results/a3-check.json").exists():
        raise SystemExit("A-3 must be concluded (lab/evidence/results/a3-check.json) before A-4 exploration")
    design, _ = _a4_halves(P)
    design_codes = {c for cl in design for c in cl}
    rows, failed, n = [], {}, {}
    for S, split_name, sources in ((A2, "tune", ("natural",)), (A3, "check", ("natural", "controlled"))):
        codes = {c["corp_code"] for c in load_split(S)["companies"] if c["split"] == split_name}
        lab = {r["cid"]: r["label"] for r in read_jsonl(S.jsonl("labels.jsonl"))}
        sc = _scores(S, split_name)
        feats = _a4_feats(S)
        for c in read_jsonl(S.jsonl("claims.jsonl")):
            corp = c["qid"].split("-q")[0]
            if corp not in codes or c["source"] not in sources or lab.get(c["cid"]) in _JUDGE_SKIP \
                    or c.get("not_claim_rule"):
                continue
            tag = "a2-tune" if S.study == "a2" else ("a3-design" if corp in design_codes else "a3-check")
            if c["cid"] not in sc:
                if c["source"] == "natural":
                    raise SystemExit(f"{c['cid']} lacks a stored JEV score ({S.study} scores/{split_name}.jsonl)")
                continue
            if not sc[c["cid"]]["ok"]:
                failed[tag] = failed.get(tag, 0) + 1
                continue
            label = lab[c["cid"]]
            rows.append({**feats(c), "s": sc[c["cid"]]["s"], "c": sc[c["cid"]]["c"], "label": label,
                         "y": int(label == "supported"), "variant": c.get("variant"), "expected": c.get("expected"),
                         "source": c["source"], "set": tag})
            n[tag] = n.get(tag, 0) + 1
    return rows, n, failed


def _halves_sha(design: list[list[str]], check: list[list[str]]) -> str:
    """반분 목록의 SHA-256(사전등록 exploration.half_split.sha256과 대조)."""
    return hashlib.sha256(json.dumps({"design": design, "check": check}, ensure_ascii=False, sort_keys=True)
                          .encode()).hexdigest()


def _require_draft(P: Paths, cmd: str) -> dict:
    pre = json.loads(P.prereg.read_text())
    if pre.get("status") == "registered":
        raise SystemExit(f"{cmd} is for the draft prereg only ({P.prereg.name} is registered)")
    return pre


def cmd_a4_measure(P: Paths, args) -> None:
    """1a 실측(spec 8절 1a, 호출 0): A-3 확인 세트 반분 목록을 원장에 먼저 남기고, 두 탐색 세트의 저장된 주 판정 확률로 후속
    비율·후보 문단 수 m(평균·p95)을 세어 탐색 상한·확인 미리 멈춤 식 값을 다시 계산한다. 값은 prereg_a4.json 초안에 옮긴다."""
    from lab.evidence import a4

    pre = _require_draft(P, "a4-measure")
    rows, n, failed = _a4_explore_rows(P)  # 데이터를 먼저 다 읽는다(빠진 것이 있으면 원장에 아무것도 남기지 않는다)
    design, rest = _a4_halves(P)
    half_sha = _halves_sha(design, rest)
    want = ((pre.get("exploration") or {}).get("half_split") or {}).get("sha256")
    if want and want != half_sha:
        raise SystemExit(f"half split sha256 {half_sha} differs from prereg_a4.json exploration.half_split")
    if not any(r.get("event") == "a4-half-split" and r.get("study") == "a4" for r in read_jsonl(P.attempts)):
        log_attempt(P, "a4-half-split", seed=a4.HALF_SEED, design=design, check=rest, sha256=half_sha,
                    design_companies=sum(map(len, design)), check_companies=sum(map(len, rest)))
    sets = {k: a4.measure([r for r in rows if r["set"] == k]) for k in ("a2-tune", "a3-design", "a3-check")}
    allm = a4.measure(rows)
    confirm_n = round(len([r for r in rows if r["set"] != "a2-tune"]) * a4.RANDOM_N / 40)  # A-3 판정 대상 × 45/40
    est = {"explore_cap_v1": allm["explore_estimate_per_version"],
           "explore_cap_v3": a4.explore_estimate(allm["followup"], allm["m_mean"], versions=3),
           "confirm_judge_n": confirm_n,
           "confirm_estimate": a4.confirm_estimate(confirm_n, confirm_n, allm["followup_ratio"] or 0.0,
                                                   allm["followup_tokens_p95_estimate"])}
    out = {"sets": sets, "all": allm, "jev_failed_excluded": failed, "estimates": est, "half_split_sha256": half_sha}
    path = P.priv / "a4_measure.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n")
    log_attempt(P, "a4-measure", **out)
    print(json.dumps(out, ensure_ascii=False))


def _explore_cap(pre: dict) -> int:
    from lab.evidence import a4

    cap = (pre.get("exploration") or {}).get("token_cap")
    if cap is None:
        raise SystemExit("prereg_a4.json exploration.token_cap is empty: run a4-measure and fill it in the draft")
    if int(cap) > a4.EXPLORE_CAP_MAX:
        raise SystemExit(f"exploration token cap {cap} exceeds max 3,000,000 (decision 6)")
    return int(cap)


def _a4_selection(P: Paths) -> dict | None:
    """원장의 a4-proceed 선택(판·문맥·신호·τ_d)과 진행 기준 통과 여부. 없으면 None."""
    lines = [r for r in read_jsonl(P.attempts) if r.get("event") == "a4-proceed" and r.get("study") == "a4"]
    if not lines or not lines[-1].get("selection"):
        return None
    return {**lines[-1]["selection"], "proceed": lines[-1]["proceed"]}


def _a4_proceeded(P: Paths) -> bool:
    return any(r.get("event") == "a4-proceed" and r.get("study") == "a4" for r in read_jsonl(P.attempts)) \
        or (P.priv / "a4_proceed.json").exists()


A4_CODE = ("app/services/evidence/subject_a4.py", "lab/evidence/a4.py")


def _context_all_ruled(P: Paths) -> bool:
    """결정 2-2: 원장에 설계용 절반 C 손실 중 원인 (마) 건수 ruling 줄이 있고 (마)가 절반 이상이어야 '문단 8개 전부'로 바꾼다."""
    rul = [r for r in read_jsonl(P.attempts) if r.get("event") == "ruling" and r.get("study") == "a4"
           and r.get("topic") == "context-all"]
    if not rul:
        return False
    loss, ma = int(rul[-1].get("design_c_loss") or 0), int(rul[-1].get("cause_ma") or 0)
    return loss > 0 and 2 * ma >= loss


def _compact(grid: list[dict]) -> list[dict]:
    return [{"signal": g["signal"], "tau_d": g["tau_d"],
             **{f"{arm}_{h}": {k: v[k] for k in ("loss", "denominator", "swap_accuracy", "swap_n")}
                for arm, halves in g["arms"].items() for h, v in halves.items()}} for g in grid]


def cmd_a4_explore(P: Paths, args) -> None:
    """확정 전 탐색(spec 4.2): 두 탐색 세트의 ✅ 후보 주장에 후속 주체 질문만 새로 부른다(별도 원장 jev_calls_explore.jsonl,
    A-3 원장·결과는 읽기만). 상한에 닿으면 어떤 지표도 계산하지 않는다. 다 부르면 **설계용 절반만**: (신호 × τ_d) 모든 조합의
    설계용 격자, 설계용 선택(참고), 설계용 감사(AI 분류 칸 비움)를 비공개 파일과 원장에 남긴다. 점검용 절반은 계산하지 않는다
    (a4-proceed가 한 번만 본다). a4-proceed 뒤에는 거부한다."""
    from app.lib.jev import TokenCapExceeded, request_key
    from app.services.evidence import subject_a4 as sa
    from lab.evidence import a2, a4

    pre = _require_draft(P, "a4-explore")
    if _a4_proceeded(P):
        raise SystemExit("exploration is closed: a4-proceed already ran")
    if not any(r.get("event") == "a4-measure" and r.get("study") == "a4" for r in read_jsonl(P.attempts)):
        raise SystemExit("run a4-measure first (1a: half split and measured caps)")
    cap = _explore_cap(pre)
    version, context = args.prompt_version, args.context
    if version not in sa.PROMPTS:
        raise SystemExit(f"unknown prompt version {version} (subject_a4.PROMPTS)")
    used = [(r["prompt_version"], r.get("context", "candidates")) for r in read_jsonl(P.attempts)
            if r.get("event") == "a4-explore" and r.get("study") == "a4"]
    a4.check_prompt_versions(used + [(version, context)])
    if context == "all" and not _context_all_ruled(P):
        raise SystemExit("--context all needs a ruling line (topic context-all) with cause (마) ≥ half of design C loss")
    p95 = (pre.get("budget") or {}).get("followup_tokens_p95_estimate")
    if p95 is None:
        raise SystemExit("prereg_a4.json budget.followup_tokens_p95_estimate is empty: run a4-measure and fill it")
    if context == "all":
        p95 = a4.FOLLOWUP_HEAD + a4.K * a4.FOLLOWUP_PER_PASSAGE
    rows, n, failed = _a4_explore_rows(P)
    reqs = [(r, *sa.build_followup(r["company"], r["text"], r["passages"], cand, context, version))
            for r in rows for cand in a4.followup_requests(r)]
    calls = read_jsonl(P.explore_calls)
    spent = sum(c["input_tokens"] for c in calls)
    cached = {c["key"] for c in calls if c["ok"]}
    a2.check_budget(spent, sum(request_key(st, qs) not in cached for _, st, qs, _ in reqs) * int(p95), cap)
    client = _jev_client(P.explore_calls, cap)
    for r in rows:
        r["q"], r["q_failed"] = {}, False
    try:
        for r, st, qs, qmap in reqs:
            res = client.ask(st, qs, tag=f"explore-{version}-{context}")
            if res.ok:
                r["q"].update(sa.followup_probs(res.answers, qmap))
            else:
                r["q_failed"] = True
    except TokenCapExceeded:
        log_attempt(P, "a4-explore-incomplete", prompt_version=version, context=context, todo=len(reqs),
                    used_tokens=client.used_tokens, cap=cap)
        raise SystemExit("explore incomplete: token cap reached; no metrics computed (AI lead ruling needed)")
    qpath = P.priv / "explore_q" / f"{version}-{context}.jsonl"
    write_jsonl(qpath, [{"cid": r["cid"], "q": None if r["q_failed"] else r["q"], "q_failed": r["q_failed"]}
                        for r in rows])
    nat, sw = _a4_split_rows(rows)
    grid = a4.explore_grid(nat, sw, halves=("design",))
    sel = a4.choose(grid)
    # 감사 기준점: 설계용 선택, 없으면 가장 덜 막는 설정(P(diff), 최대 τ_d) — 판 고침 근거라 선택이 없을 때도 낸다
    at = sel or {"signal": "p_diff", "tau_d": max(a4.TAU_D_GRID)}
    aud = {**a4.audit(nat, sw, at["signal"], at["tau_d"]), "at": at}
    code = {rel: _file_sha(P.root / rel) for rel in A4_CODE}
    res = {"prompt_version": version, "context": context, "subject_question_sha": sa.question_sha(version),
           "code_sha256": code, "n": n, "jev_failed_excluded": failed, "followup_calls": len(reqs),
           "followup_failed": sum(r["q_failed"] for r in rows), "used_tokens": client.used_tokens, "cap": cap,
           "grid": grid, "selected": sel, "audit": aud, "x0_audit": a4.x0_audit([r for r in sw if r["set"] != "a3-check"]),
           "note": "설계용 절반(A-2 조정 + A-3 설계용 반)만 계산했다. 점검용 절반은 a4-proceed가 한 번만 본다. "
                   "손실 분모는 후속 실패 행을 뺀다. 감사 ai_cause는 AI가 채우고 AI 작성임을 밝힌다."}
    path = P.priv / f"a4_explore_{version}-{context}.json"
    path.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str) + "\n")
    log_attempt(P, "a4-explore", prompt_version=version, context=context, subject_question_sha=res["subject_question_sha"],
                code_sha256=code, q_sha256=_file_sha(qpath), n=n, followup_calls=len(reqs),
                followup_failed=res["followup_failed"], used_tokens=client.used_tokens, grid=_compact(grid),
                selected_design=sel, x0_more_supported=res["x0_audit"]["more_supported"])
    print(json.dumps({"selected_design": sel}, ensure_ascii=False))


def _a4_split_rows(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """후속 실패 행을 뺀 자연 주장·통제 주체 교체 주장(탐색 손실 분모는 후속 실패 행을 뺀다)."""
    ok = [r for r in rows if not r.get("q_failed")]
    return ([r for r in ok if r["source"] == "natural"],
            [r for r in ok if (r.get("variant") or "").startswith("주체 교체")])


def cmd_a4_proceed(P: Paths, args) -> None:
    """진행 기준(spec 4.2-4, 6.2) — 한 번만. 지금까지 탐색한 모든 (판, 문맥)의 설계용 격자를 합쳐 6.2 규칙으로 판·문맥·신호·
    τ_d를 고르고(a4.choose_all), 그 선택에만 점검용 절반과 진행 기준을 계산한다. 모든 조합의 격자(점검용 포함)를 공개한다.
    호출 없음(탐색 후속 응답 파일을 원장의 해시와 대조해 읽는다). 이 뒤에는 a4-explore가 거부되고, 사전등록 확정은 이 선택과
    같아야 한다(a4.check_registrable)."""
    from lab.evidence import a4

    _require_draft(P, "a4-proceed")
    if _a4_proceeded(P):
        raise SystemExit("a4-proceed already ran (the check half is looked at once)")
    lines = [r for r in read_jsonl(P.attempts) if r.get("event") == "a4-explore" and r.get("study") == "a4"
             and r.get("q_sha256")]
    if not lines:
        raise SystemExit("run a4-explore first")
    latest: dict[tuple[str, str], str] = {}
    for r in lines:  # 처음 탐색한 순서를 지키고, 같은 판을 다시 돌렸으면 마지막 응답 해시
        latest[(r["prompt_version"], r.get("context", "candidates"))] = r["q_sha256"]
    base, n, failed = _a4_explore_rows(P)
    per = {}
    for (v, ctx), sha in latest.items():
        qpath = P.priv / "explore_q" / f"{v}-{ctx}.jsonl"
        if _file_sha(qpath) != sha:
            raise SystemExit(f"explore answers changed after a4-explore: {qpath.name}")
        q = {r["cid"]: r for r in read_jsonl(qpath)}
        rows = [{**r, "q": None if q[r["cid"]]["q"] is None else {int(k): p for k, p in q[r["cid"]]["q"].items()},
                 "q_failed": q[r["cid"]]["q_failed"]} for r in base]
        per[(v, ctx)] = _a4_split_rows(rows)
    design = {k: a4.explore_grid(*nat_sw, halves=("design",)) for k, nat_sw in per.items()}
    sel = a4.choose_all(design)
    full = {f"{v}-{ctx}": a4.explore_grid(*nat_sw) for (v, ctx), nat_sw in per.items()}
    crit = aud = None
    if sel:
        nat, sw = per[(sel["version"], sel["context"])]
        crit = a4.proceed(nat, sw, sel["signal"], sel["tau_d"])
        aud = a4.audit(nat, sw, sel["signal"], sel["tau_d"])
    ok = bool(crit and crit["proceed"])
    res = {"selection": sel, "proceed": ok, "criteria": crit, "grids": full, "audit": aud, "n": n,
           "jev_failed_excluded": failed,
           "note": "판·문맥·신호·τ_d는 설계용 격자 합본에서만 골랐다(6.2). 점검용 절반은 이 선택에만, 이 명령에서 한 번만 봤다."}
    (P.priv / "a4_proceed.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str) + "\n")
    log_attempt(P, "a4-proceed", selection=sel, proceed=ok,
                criteria=None if crit is None else {k: crit[k] for k in ("recall", "swap", "direction", "optimism")},
                grids={k: _compact(g) for k, g in full.items()})
    print(json.dumps({"selection": sel, "proceed": ok}, ensure_ascii=False))


def _check_swap_tags(P: Paths, codes: set[str]) -> None:
    """채택된 c2마다 교체어 태그(고유명/일반명사, 판정 전 AI 작성)가 있어야 동결한다."""
    from lab.evidence import a4

    tags = {r["cid"]: r["tag"] for r in read_jsonl(P.jsonl("swap_tags.jsonl"))}
    c2 = [c["cid"] for c in read_jsonl(P.jsonl("claims.jsonl")) if c["qid"].split("-q")[0] in codes
          and (c.get("variant") or "").startswith("주체 교체")]
    bad = [cid for cid in c2 if tags.get(cid) not in a4.SWAP_TAGS]
    if bad:
        raise SystemExit(f"missing swap tag (고유명/일반명사) for {len(bad)} c2 claims, e.g. {bad[:3]}")


def _judge_a4(P: Paths, args) -> None:
    """A-4 판정: 주 판정(a2-v1과 같은 요청, judge.build_state·build_questions) + ✅ 후보 문단 후속 주체 질문. 같은 원장(태그로
    구분). 호출 전 미리 멈춤(spec 5.6): 캐시에 없는 주장 × 5,070 + 판정 대상 × (실측 후속 비율 + 0.10) × 실측 후속 p95.
    상한에 닿으면 부분 결과만 .partial로 남기고 멈춘다(a4-report가 거부한다)."""
    from app.lib.jev import TokenCapExceeded, request_key
    from app.services.evidence import judge as jg
    from app.services.evidence import subject_a4 as sa
    from lab.evidence import a2, a4

    pre = json.loads(P.prereg.read_text())
    if args.split == P.sealed and pre.get("status") != "registered":
        raise SystemExit("a4 check judge needs prereg_a4.json registered")
    verify_prereg_code(P)
    once(P, P.priv / "scores" / f"{args.tag}.jsonl", args.split, "judge", args.tag)
    cap = a4.check_confirm_cap(int(pre["token_cap"]))
    b = pre.get("budget") or {}
    ratio, p95 = b.get("followup_ratio"), b.get("followup_tokens_p95_estimate")
    if ratio is None or p95 is None:
        raise SystemExit("prereg_a4.json budget needs measured followup_ratio and followup_tokens_p95_estimate")
    claims = _judgeable(P, args.split, include_disputed=(args.split == P.sealed))
    if args.limit:
        keep = set(subset([c["cid"] for c in claims if c["source"] == "natural"], args.limit))
        claims = [c for c in claims if c["cid"] in keep]
    feats = _a4_feats(P)
    fs = {c["cid"]: feats(c) for c in claims}
    main = {cid: (jg.build_state(f["company"], f["text"], f["passages"]), jg.build_questions(len(f["passages"])))
            for cid, f in fs.items()}
    calls = read_jsonl(P.calls)
    used = sum(r["input_tokens"] for r in calls)
    cached = {r["key"] for r in calls if r["ok"]}
    uncached = sum(request_key(*main[c["cid"]]) not in cached for c in claims)
    a2.check_budget(used, a4.confirm_estimate(uncached, len(claims), float(ratio), float(p95)), cap)
    sq = pre.get("subject_question") or {}
    version, context = sq.get("version", sa.PROMPT_VERSION), sq.get("context", "candidates")
    client = _jev_client(P.calls, cap)
    out = []
    try:
        for c in claims:
            f = fs[c["cid"]]
            n = len(f["passages"])
            r = client.ask(*main[c["cid"]], use_cache=not args.no_cache, tag=args.tag)
            row = {"cid": c["cid"], "ok": r.ok, "requests": 1, "main_tokens": r.input_tokens,
                   "latency_ms": r.latency_ms, "s": [0.0] * n, "c": [0.0] * n, "m": 0, "q": {}, "q_failed": False,
                   "followup_tokens": 0, "followup_latency_ms": 0.0}
            if r.ok:
                row["s"] = [r.answers[f"p{j}"]["supports"] for j in range(1, n + 1)]
                row["c"] = [r.answers[f"p{j}"]["contradicts"] for j in range(1, n + 1)]
                for cand in a4.followup_requests({**f, "s": row["s"], "c": row["c"]}):  # 제품과 같은 문맥(F8)
                    st, qs, qmap = sa.build_followup(f["company"], f["text"], f["passages"], cand, context, version)
                    fr = client.ask(st, qs, use_cache=not args.no_cache, tag=f"{args.tag}-followup")
                    row["m"] += len(cand)
                    row["followup_tokens"] += fr.input_tokens
                    row["followup_latency_ms"] += fr.latency_ms
                    if fr.ok and not row["q_failed"]:
                        row["q"].update(sa.followup_probs(fr.answers, qmap))
                    else:
                        row["q"], row["q_failed"] = None, True
            out.append(row)
    except TokenCapExceeded:
        write_jsonl(P.priv / "scores" / f"{args.tag}.partial.jsonl", out)
        log_attempt(P, "judge-incomplete", tag=args.tag, split=args.split, claims=len(out), todo=len(claims),
                    used_tokens=client.used_tokens, cap=cap)
        raise SystemExit("judge incomplete: token cap reached (raise only by AI-lead ruling within 9,600,000)")
    write_jsonl(P.priv / "scores" / f"{args.tag}.jsonl", out)
    log_attempt(P, "judge", tag=args.tag, split=args.split, claims=len(out), failed=sum(not r["ok"] for r in out),
                followup=sum(r["m"] > 0 for r in out), followup_failed=sum(bool(r["q_failed"]) for r in out),
                used_tokens=client.used_tokens)
    print(f"{len(out)} judged, used tokens {client.used_tokens}")


def a4_rows(P: Paths, split_name: str) -> tuple[list[dict], int]:
    """판정 가능한 주장(자연·통제)의 평가 행(①c 마스크·주 판정·후속 응답)과 주 판정 실패로 뺀 수. 판정이 끝나지 않았거나
    (.partial, 빠진 주장, 후속 응답 없는 후보 주장) 있으면 멈춘다 — 판정이 끝나기 전에는 어떤 지표도 계산하지 않는다."""
    from lab.evidence import a4

    if (P.priv / "scores" / f"{split_name}.partial.jsonl").exists():
        raise SystemExit("judge incomplete (token cap reached): no metrics are computed")
    feats = _a4_feats(P)
    sc = _scores(P, split_name)
    tags = {r["cid"]: r["tag"] for r in read_jsonl(P.jsonl("swap_tags.jsonl"))}
    claims = _judgeable(P, split_name)
    missing = [c["cid"] for c in claims if c["cid"] not in sc]
    if missing:
        raise SystemExit(f"judge incomplete: {len(missing)} {split_name} claims lack scores")
    rows, failed = [], 0
    for c in claims:
        s = sc[c["cid"]]
        if not s["ok"]:
            failed += 1
            continue
        r = {**feats(c), "s": s["s"], "c": s["c"], "label": c["label"], "y": int(c["label"] == "supported"),
             "variant": c.get("variant"), "expected": c.get("expected"), "source": c["source"],
             "swap_tag": tags.get(c["cid"]), "q_failed": bool(s.get("q_failed")),
             "q": None if s.get("q") is None else {int(k): v for k, v in s["q"].items()},
             "m": s.get("m", 0), "main_tokens": s.get("main_tokens", 0), "latency_ms": s.get("latency_ms", 0.0),
             "followup_tokens": s.get("followup_tokens", 0), "followup_latency_ms": s.get("followup_latency_ms", 0.0)}
        if not r["q_failed"] and any(i not in (r["q"] or {}) for i in a4.followup_candidates(r)):
            raise SystemExit(f"judge incomplete: {c['cid']} lacks follow-up answers")
        rows.append(r)
    return rows, failed


def a4_report_md(res: dict) -> str:
    """A-4 리포트 본문(AI 생성 표시, 금액 없음)."""
    g, rec, cost = res["gates"], res["recommendation"], res["cost"]
    sw, rc, pr = g["h_swap"], g["h_recall"], g["h_prec"]
    fp = lambda d: ", ".join(f"{k} {_pct(v)}" for k, v in d.items())  # noqa: E731
    lines = ["# 근거 판정 A-4 평가 리포트(주체 질문, 확인 세트 1회 실행)", "",
             "> 이 리포트는 `python -m lab.evidence --study a4 a4-report`가 자동 생성했다. 정답 라벨은 사람이 아니라 "
             "AI(Claude Opus·Codex)가 만든 **AI 참조 라벨**이며, 통제 주장 기대값과 교체어 태그는 AI 작성자가 만들었다. "
             "판정에는 TypeSafe의 JEV 모델을 썼고, 이 프로젝트는 TypeSafe와 제휴 관계가 아니다.", "",
             "- 사전등록: `lab/evidence/prereg_a4.json`, 동결: `lab/evidence/prereg_a4_check.json`, 원자료: "
             "`lab/evidence/results/a4-check.json`",
             f"- 비교: 같은 JEV 확률 위에서 a2-v1 대 a4-subject-exp(C, ①c 코드 + ② 주체 질문, 신호 {res['signal']}, "
             f"τ_d {res['tau_d']}). 자연 주장 {res['n_natural']}건(AI 라벨 지지됨 {rc['positive']}건), 관문 대상 주체 교체 "
             f"{sw['n']}건, 주 판정 실패 제외 {res['jev_failed_excluded']}건, 판정 실패 합계 제외 {g['followup_failed_excluded']}건"
             f"{' — 1% 초과로 세 관문 기술 통계만' if g['judge_failed_over'] else ''}",
             "", "## 관문(C에만 건다)", "", "| 가설 | 값 | 결과 |", "|---|---|---|",
             f"| H-swap | 주체 교체 정확도 C {_pct(sw['accuracy'])} ({_ci(sw['ci'])}) 대 a2-v1 {_pct(sw['base_accuracy'])}, "
             f"차이 {_pct(sw['diff']['point'])} (95% {_pct(sw['diff']['lo'])}~{_pct(sw['diff']['hi'])})"
             f"{', 기술 통계만' if sw['descriptive_only'] else ''} | {'통과' if sw['pass'] else '실패'} |",
             f"| H-recall | ✅ 재현율 a2-v1 {_pct(rc['base_recall'])} → C {_pct(rc['recall'])}, 손실 {_pct(rc['loss'])} "
             f"({_ci(rc['ci'])}){', 기술 통계만' if rc['descriptive_only'] else ''} | {'통과' if rc['pass'] else '실패'} |",
             f"| H-prec | ✅ 예측 {pr['predicted']}건, 정밀도 {_pct(pr['precision'])} ({_ci(pr['ci'])}), a2-v1 "
             f"{pr['base_predicted']}건 {_pct(pr['base_precision'])}, 차이 {_pct(pr['diff']['point'])} "
             f"(95% {_pct(pr['diff']['lo'])}~{_pct(pr['diff']['hi'])}){', 기술 통계만' if pr['descriptive_only'] else ''}"
             f" | {'통과' if pr['pass'] else '실패'} |",
             "", "## 권고(사전등록 규칙, 보조 팔은 쓰지 않는다)", "", f"- {rec['note']}",
             f"- 운영 비용: 자연 주장 주장당 입력 토큰 증가 {_pct(cost['token_increase'])}(조건 ≤ {cost['max_increase']}), "
             f"후속 비율 {_pct(cost['followup_ratio'])}, m 평균 {_pct(cost['m_mean'])}·p95 {_pct(cost['m_p95'])}, "
             f"후속 지연 p95 {_pct(cost['followup_latency_p95_ms'])}ms, 마감 8초 초과 비율 {_pct(cost['over_deadline_share'])}",
             "", "## 보조 결과(기술 통계)", "",
             *[f"- H-swap 하위 유형 {k}: {v['n']}건, 정확도 {fp(v['accuracy'])}; 짝 불일치 C만 막음 "
               f"{v['discordant']['c_only']} / a3만 막음 {v['discordant']['a3_only']}"
               for k, v in sw["by_subtype"].items()],
             *[f"- 교체어 태그 {k}: {v['n']}건, 정확도 {fp(v['accuracy'])}" for k, v in sw["by_tag"].items()],
             f"- 문단 안 교체(관문 밖) {g['in_passage_swap']['n']}건: 정확도 {fp(g['in_passage_swap']['accuracy'])}",
             f"- H-swap 민감도(라벨 supported·disputed c2 제외, 관문 아님) {g['h_swap_sensitivity']['n']}건: C "
             f"{_pct(g['h_swap_sensitivity']['accuracy'])} ({_ci(g['h_swap_sensitivity']['ci'])})",
             *[f"- 보조 팔 {arm}: H-swap {_pct(v['h_swap']['accuracy'])}, H-recall 손실 {_pct(v['h_recall']['loss'])} "
               f"(상한 {_pct(v['h_recall']['ci'][1])}), H-prec 차이 하한 {_pct(v['h_prec']['diff']['lo'])}"
               for arm, v in g["arms"].items()],
             f"- 표기 변형 오탐(a2-v1 ✅인 c3 중 C에서 ✅ 아님): {res['controlled']['notation_false_negative']['lost']}/"
             f"{res['controlled']['notation_false_negative']['n_base_supported']}",
             f"- 자연 주장 AUC a2-v1 {_pct(res['auc']['a2-v1'])}, C {_pct(res['auc']['C'])}",
             f"- a2-v1 ✅ → C ❔로 바뀐 자연 주장 {len(res['removed'])}건(원인 ①c·②·둘 다는 원자료 removed), "
             f"C가 놓친 교체 {len(res['missed_swaps'])}건의 P(diff)·P(unclear)는 원자료 missed_swaps", ""]
    return "\n".join(lines)


def _a4_controlled_summary(ctrl: list[dict], signal: str, tau_d: float) -> dict:
    """통제 주장 변형별 정확도(다섯 정책)와 표기 변형 오탐(AI 라벨 supported인 c3 중 a2-v1 ✅ → C ✅ 아님)."""
    from lab.evidence import a4

    d = a4.annotate([r for r in ctrl if not r.get("q_failed")], signal, tau_d)
    acc: dict[str, dict[str, list[bool]]] = {arm: {} for arm in a4.FIVE}
    for r in d:
        for arm in a4.FIVE:
            acc[arm].setdefault(r["variant"], []).append((r["dec"][arm] == "supported") == (r["expected"] == "supported"))
    c3 = [r for r in d if (r["variant"] or "").startswith("표기 변형") and r.get("label") == "supported"]
    kept = [r for r in c3 if r["dec"]["a2-v1"] == "supported"]
    lost = [r for r in kept if r["dec"]["C"] != "supported"]
    types: dict[str, dict[str, int]] = {}
    for r in kept:
        t = types.setdefault(r["variant"].split(":", 1)[1], {"n": 0, "lost": 0})
        t["n"] += 1
        t["lost"] += r["dec"]["C"] != "supported"
    return {"accuracy": {arm: {v: sum(x) / len(x) for v, x in sorted(m.items())} for arm, m in acc.items()},
            "notation_false_negative": {"n_labeled_supported": len(c3), "n_base_supported": len(kept),
                                        "lost": len(lost), "rate": len(lost) / len(kept) if kept else None,
                                        "by_type": types}}


def cmd_a4_report(P: Paths, args) -> None:
    """확인 세트(동결 뒤 1회 판정)로 C 대 a2-v1 관문·보조 팔·하위 유형 다섯 정책·민감도·운영 비용을 계산한다. 추가 호출 없음."""
    from app.services.evidence import subject_a4 as sa
    from lab.evidence import a4
    from lab.evidence.metrics import auc

    companies(P, P.sealed)  # 동결 확인
    verify_prereg_code(P)
    pre = json.loads(P.prereg.read_text())
    if pre.get("status") != "registered":
        raise SystemExit("a4-report needs prereg_a4.json registered")
    a4.check_registrable(pre, _a4_selection(P), require_selection=True)
    want = {"base": a4.policy_fields(a4.BASE), "exp": a4.policy_fields(a4.EXP), "a3": a4.policy_fields(a4.A3)}
    if pre.get("policies") != want or (pre["tau_d"], pre["signal"]) != (a4.EXP.tau_d, a4.EXP.subject_signal):
        raise SystemExit(f"policy constants differ from prereg_a4.json: {want}")
    sq = pre.get("subject_question") or {}
    if sq.get("sha") != sa.question_sha(sq.get("version", sa.PROMPT_VERSION)):
        raise SystemExit("subject question sha differs from prereg_a4.json")
    rows, failed = a4_rows(P, P.sealed)
    signal, tau_d = pre["signal"], pre["tau_d"]
    natural = [r for r in rows if r["source"] == "natural"]
    ctrl = [r for r in rows if r["source"] == "controlled"]
    swaps = [r for r in ctrl if (r["variant"] or "").startswith("주체 교체")]
    g = a4.check_gates(rows, signal, tau_d, main_failed=failed)  # 1% 규칙 분모 = 이견 없는 판정 주장 전부(F6)
    cost = a4.cost_summary(rows)
    d = a4.annotate([r for r in natural if not r["q_failed"]], signal, tau_d, arms=("a2-v1", "C"))
    y = [r["y"] for r in d]
    res = {"policies": want, "signal": signal, "tau_d": tau_d, "n_natural": len(natural), "jev_failed_excluded": failed,
           "gates": g, "recommendation": a4.recommendation(g, cost), "cost": cost,
           "controlled": _a4_controlled_summary(ctrl, signal, tau_d),
           "auc": {k: auc(y, [r["score"][k] for r in d]) for k in ("a2-v1", "C")},
           "removed": a4.removed_audit(natural, signal, tau_d),
           "missed_swaps": a4.missed_swap_distribution(swaps, signal, tau_d)}
    out = P.ev / "results/a4-check.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str) + "\n")
    doc = P.root / "docs/lab/evidence-a4-report.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(a4_report_md(res))
    log_attempt(P, "a4-report", h_swap=g["h_swap"]["pass"], h_recall=g["h_recall"]["pass"], h_prec=g["h_prec"]["pass"],
                cost_pass=cost["pass"], switch_default=res["recommendation"]["switch_default"])
    print(json.dumps({k: g[k]["pass"] for k in ("h_swap", "h_recall", "h_prec")}))


COMMANDS.update({"a4-measure": cmd_a4_measure, "a4-explore": cmd_a4_explore, "a4-proceed": cmd_a4_proceed,
                 "a4-report": cmd_a4_report})
A1_ONLY = {"stage0-report", "stage1-tune", "stage1-freeze-config", "stage1-report", "baselines", "claim-embed"}
A2_ONLY = {"a2-tune", "a2-report"}
A3_ONLY = {"a3-report", "a3-explore"}
A4_ONLY = {"a4-measure", "a4-explore", "a4-proceed", "a4-report"}


def main(argv: list[str] | None = None) -> None:
    """명령 분기."""
    ap = argparse.ArgumentParser(prog="lab.evidence")
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--study", default="a1", choices=STUDIES)
    ap.add_argument("cmd", choices=sorted(COMMANDS))
    ap.add_argument("--split", default="dev", choices=SPLITS)
    ap.add_argument("--labeler", choices=["opus", "codex"])
    ap.add_argument("--round", type=int, default=1)
    ap.add_argument("--tag", default="")
    ap.add_argument("--single", action="store_true")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--prompt-version", default="v1")  # A-4 탐색 주체 질문 문구 판(subject_a4.PROMPTS)
    ap.add_argument("--context", default="candidates", choices=("candidates", "all"))  # A-4 후속 요청 문맥(결정 2-2)
    args = ap.parse_args(argv)
    if args.study != "a1" and args.cmd in A1_ONLY:
        raise SystemExit(f"{args.cmd} is an a1 command")
    if args.study != "a2" and args.cmd in A2_ONLY:
        raise SystemExit(f"{args.cmd} needs --study a2")
    if args.study != "a3" and args.cmd in A3_ONLY:
        raise SystemExit(f"{args.cmd} needs --study a3")
    if args.study != "a4" and args.cmd in A4_ONLY:
        raise SystemExit(f"{args.cmd} needs --study a4")
    COMMANDS[args.cmd](Paths(args.root, args.study), args)


if __name__ == "__main__":
    main()
