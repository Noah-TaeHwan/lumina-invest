#!/usr/bin/env bash
# 배포 단계에서 1회 실행하는 DB 마이그레이션 (앱은 RUN_MIGRATIONS_ON_STARTUP=false 로 기동)
#   ./scripts/migrate.sh            # 로컬 python 환경
#   docker compose run --rm app alembic upgrade head   # 컨테이너
set -euo pipefail
cd "$(dirname "$0")/.."
alembic upgrade head
