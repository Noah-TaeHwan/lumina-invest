#!/usr/bin/env bash
# 서버: 올려 둔 스냅샷 파일을 compose 안 Qdrant의 factcheck_passages로 복원한다(같은 이름 컬렉션은 덮어쓴다).
# 복원하는 동안 앱을 멈춘다(덮어쓰는 중 검수가 빈·반쯤 찬 컬렉션을 보지 않게) → 복원 → 앱을 다시 켠다.
# 중간에 실패해도 종료할 때 항상 앱을 다시 켠다(trap) — 데모가 꺼진 채 남지 않게. 실패하면 이전 컬렉션이 남아 있을 수 있으니
# /api/health의 checks.data를 확인한다.
# Qdrant 포트는 밖에 열지 않으므로 같은 네트워크의 app 이미지(httpx 있음)로 보낸다. 저장소 루트에서 실행한다.
#   deploy/factcheck/snapshot_restore.sh ./factcheck_passages-20261006.snapshot
set -euo pipefail
SNAP="${1:?usage: snapshot_restore.sh <snapshot file>}"
DIR=$(cd "$(dirname "$SNAP")" && pwd)
FILE=$(basename "$SNAP")
COMPOSE=(docker compose -f compose.factcheck.yml --env-file .env.factcheck)

"${COMPOSE[@]}" up -d --wait qdrant
"${COMPOSE[@]}" stop app
# shellcheck disable=SC2329 # trap이 부른다
restart_app() { "${COMPOSE[@]}" start app || echo "[WARN] 앱을 다시 켜지 못했다: ${COMPOSE[*]} start app" >&2; }
trap restart_app EXIT
"${COMPOSE[@]}" run --rm -T --no-deps -v "$DIR:/snap:ro" --entrypoint python app - "$FILE" <<'PY'
import sys

import httpx

name = sys.argv[1]
base = "http://qdrant:6333/collections/factcheck_passages"
with open(f"/snap/{name}", "rb") as fh:
    r = httpx.post(f"{base}/snapshots/upload?priority=snapshot&wait=true", files={"snapshot": (name, fh)},
                   timeout=1800)
r.raise_for_status()
count = httpx.post(f"{base}/points/count", json={"exact": True}, timeout=60).json()["result"]["count"]
print(f"restored={name} points={count}")
PY
echo "복원 끝. 앱을 다시 켠다(trap). /api/health의 checks.data(회사별 passages)를 확인한다."
