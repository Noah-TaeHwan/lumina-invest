"""판단 일지 변화 비교(모듈 C spec 결정 6-1·6-2, 10절 P2).

스냅샷과 문단 저장소의 현재 상태를 비교한다. 외부 호출(OpenDART·JEV·LLM·임베딩) 없이 그 회사 문단의 Qdrant scroll
한 번(`PassageStore.existing(corp_code)`)만 쓴다. 결과는 저장하지 않는다(결정 6-2, 열 때마다 새로 계산).

- 확인 불가를 먼저 판정한다: 저장소가 None이거나, `exists()`가 거짓이거나, `exists()`·`existing()`이 예외를 내거나
  STORE_TIMEOUT_S 안에 끝나지 않으면 {"status": "unavailable"} 하나만 돌려준다. `existing()`은 컬렉션이 없을 때 빈
  dict를 주므로 이 확인 없이 비교하면 모든 문단이 gone으로 잘못 보인다.
- 문단: 스냅샷 문단의 sha256(본문만의 해시)이 지금 그 회사 문단들의 sha256 집합 안에 있으면 same, 없으면 gone.
  passage_id(위치 이름)로 짝짓지 않는다. 문단은 스냅샷 순서이고 passage_idx는 스냅샷 passages 위치(문장 source_idx와
  같은 번호)다. ✅·⚠️ 근거 문단을 맨 위에 두는 것은 스냅샷 문장을 가진 화면(P3)이 한다.
- 보고서: 지금 그 회사 문단들의 rcept_no 집합이 스냅샷 rcept_no 하나뿐이면 same, 아니면 replaced(집합을 그대로),
  그 회사 문단이 0개면 company_gone(이때 문단은 모두 gone). 접수번호 변화는 문단 상태에 섞지 않는다.
- 판정 정책: 스냅샷 run.policy_version과 현재 DEFAULT_POLICY.version을 둘 다 돌려준다(다르면 화면이 안내). 다시 판정하지 않는다.
"""
from __future__ import annotations

import asyncio
import json
import logging

from app.services.evidence.runner import DEFAULT_POLICY

log = logging.getLogger("app.journal.changes")

STORE_TIMEOUT_S = 5.0  # 저장소가 멈춰 있으면 기다리지 않고 unavailable(상세는 이 경로와 따로 뜬다)
UNAVAILABLE = {"status": "unavailable"}


def diff(snapshot: dict, current: list[dict], policy_version: str = DEFAULT_POLICY.version) -> dict:
    """스냅샷과 지금 그 회사 적재 문단(passage_id·rcept_no·sha256 dict 목록)의 비교."""
    hashes = {p.get("sha256") for p in current}
    rcepts = sorted({p.get("rcept_no") for p in current if p.get("rcept_no")})
    then = snapshot.get("rcept_no")
    report = "company_gone" if not current else "same" if rcepts == [then] else "replaced"
    return {
        "status": "ok",
        "report": {"status": report, "snapshot_rcept_no": then, "current_rcept_nos": rcepts},
        "passages": [{"passage_idx": i, "passage_id": p.get("passage_id"), "sha256": p.get("sha256"),
                      "status": "same" if p.get("sha256") in hashes else "gone"}
                     for i, p in enumerate(snapshot.get("passages") or [])],
        "policy": {"snapshot": (snapshot.get("run") or {}).get("policy_version"), "current": policy_version},
    }


async def _current(store, corp_code: str) -> list[dict] | None:
    if not await store.exists():
        return None
    return list((await store.existing(corp_code)).values())


async def compare(store, snapshot: dict) -> dict:
    """저장소(PassageStore 또는 None)와 스냅샷의 비교. 확인할 수 없으면 UNAVAILABLE."""
    if store is None:
        return dict(UNAVAILABLE)
    try:
        current = await asyncio.wait_for(_current(store, snapshot.get("corp_code") or ""), STORE_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 — 연결 실패·시간 초과 모두 확인 불가
        log.warning(json.dumps({"event": "journal_changes_unavailable", "error": type(exc).__name__}))
        return dict(UNAVAILABLE)
    if current is None:
        return dict(UNAVAILABLE)
    return diff(snapshot, current)
