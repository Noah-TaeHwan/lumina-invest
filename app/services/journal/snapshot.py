"""판단 일지 스냅샷 생성기(모듈 C spec 결정 5-3, 형식 c1).

- 끝난 판정 실행 하나(done/partial/failed/limited/skipped)에서만 만든다. 진행 중 실행은 라우터가 먼저 409로 거른다.
- 서버가 DB의 원 실행·문장·메시지에서 복사한다. 클라이언트가 보낸 스냅샷은 받지 않는다(호출자가 run_id만 넘긴다).
- 문단별 확률 s·c, 어휘 점수 lex, jev_request_key, 지연·토큰은 담지 않는다. 화면은 상태·근거 문단·확신도 라벨만 쓰고,
  일지는 내보내기를 하므로 JEV 출력 원값을 사용자 파일로 흘리지 않는다. number_ok는 로컬 숫자 확인이라 담는다.
- 확신도 라벨은 정책 표에 의존하므로 기록 시점 값(records.serialize_claim)으로 고정한다.
"""
from __future__ import annotations

from app.models import Chat, EvidenceClaim, EvidenceRun
from app.services.evidence import records

SNAPSHOT_VERSION = "c1"
RUN_FIELDS = ("status", "error_code", "trigger", "policy_version", "jev_model", "generator_model")
CLAIM_FIELDS = ("idx", "text", "start", "end", "status", "route", "reason", "source_idx", "number_ok", "confidence")


def build_snapshot(run: EvidenceRun, claims: list[EvidenceClaim], chat: Chat) -> dict:
    return {
        "run": {"id": str(run.id), **{k: getattr(run, k) for k in RUN_FIELDS},
                "created_at": records._iso(run.created_at), "finished_at": records._iso(run.finished_at)},
        "company": run.company, "corp_code": run.corp_code, "rcept_no": run.rcept_no,
        "question": chat.question, "answer": chat.answer,
        "claims": [{k: v for k, v in records.serialize_claim(c, run.policy_version).items() if k in CLAIM_FIELDS}
                   for c in sorted(claims, key=lambda c: c.idx)],
        "passages": [{k: p.get(k) for k in records.PASSAGE_FIELDS} for p in run.passages],
    }


def selectable_claims(snapshot: dict) -> set[int]:
    """기댄 문장으로 고를 수 있는 문장 idx(비주장 문장 제외)."""
    return {c["idx"] for c in snapshot.get("claims", []) if c.get("status") != "not_claim"}
