#!/usr/bin/env bash
# 로컬(적재한 컴퓨터): Qdrant factcheck_passages 컬렉션 스냅샷을 만들어 파일로 내려받는다.
#   QDRANT_URL=http://127.0.0.1:6333 deploy/factcheck/snapshot_create.sh [출력 파일]
# 기본 출력은 git이 무시하는 lab/data/factcheck/snapshots/ 아래다(*.snapshot도 무시). DART 파생 파일이라 public 저장소에
# 커밋하지 않는다. 서버로 올린 뒤 snapshot_restore.sh로 복원한다(docs/deploy/factcheck.md).
set -euo pipefail
QDRANT_URL="${QDRANT_URL:-http://127.0.0.1:6333}"
COLLECTION="factcheck_passages"
OUT="${1:-lab/data/factcheck/snapshots/factcheck_passages-$(date +%Y%m%d).snapshot}"
mkdir -p "$(dirname "$OUT")"

name=$(curl -fsS -X POST "$QDRANT_URL/collections/$COLLECTION/snapshots?wait=true" |
  python3 -c 'import json, sys; print(json.load(sys.stdin)["result"]["name"])')
curl -fsS -o "$OUT" "$QDRANT_URL/collections/$COLLECTION/snapshots/$name"
count=$(curl -fsS -X POST -H 'Content-Type: application/json' -d '{"exact": true}' \
  "$QDRANT_URL/collections/$COLLECTION/points/count" |
  python3 -c 'import json, sys; print(json.load(sys.stdin)["result"]["count"])')
echo "snapshot=$OUT points=$count"
sha256sum "$OUT"
