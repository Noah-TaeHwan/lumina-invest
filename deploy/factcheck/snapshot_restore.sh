#!/usr/bin/env bash
# 서버: 올려 둔 스냅샷 파일을 compose 안 Qdrant의 factcheck_passages로 복원한다(같은 이름 컬렉션은 덮어쓴다).
# Qdrant 포트는 밖에 열지 않으므로 같은 네트워크의 app 이미지(httpx 있음)로 보낸다. 저장소 루트에서 실행한다.
#   deploy/factcheck/snapshot_restore.sh ./factcheck_passages-20261006.snapshot
set -euo pipefail
SNAP="${1:?usage: snapshot_restore.sh <snapshot file>}"
DIR=$(cd "$(dirname "$SNAP")" && pwd)
FILE=$(basename "$SNAP")
COMPOSE=(docker compose -f compose.factcheck.yml --env-file .env.factcheck)

"${COMPOSE[@]}" up -d qdrant
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
echo "복원 끝. 앱을 다시 시작하면(또는 30초 안에 재확인) /api/health에서 passages가 보인다:"
echo "  ${COMPOSE[*]} restart app"
