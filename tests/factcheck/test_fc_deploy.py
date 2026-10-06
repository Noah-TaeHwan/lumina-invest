# tests/factcheck/test_fc_deploy.py
"""T4 배포 파일 회귀: compose.factcheck.yml·Caddyfile·.env.factcheck.example의 구조를 고정한다(기동하지 않는다).

- 외부에 여는 포트는 Caddy 80/443뿐(PostgreSQL·Qdrant·Ollama·앱 포트 비공개).
- 앱은 app.factcheck_main을 uvicorn 워커 1개로(job이 메모리라 2개 이상이면 폴링이 다른 워커로 가 404).
- 모든 서비스 restart: unless-stopped + healthcheck, 앱은 PG·Qdrant·Ollama healthy 뒤, Caddy는 앱 시작 뒤(healthy 대기 아님).
- Redis·Neo4j 없음. 판정 키는 파일로 읽기 전용 마운트(환경변수로 키 값을 넘기지 않음). 데이터 폴더도 읽기 전용.
- .env.factcheck.example에는 비밀 실값이 없고, 실제 .env.factcheck는 git이 무시한다.
"""
import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "compose.factcheck.yml"


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def test_only_caddy_publishes_ports_80_443():
    svcs = _compose()["services"]
    assert set(svcs) == {"app", "postgres", "qdrant", "ollama", "caddy"}  # Redis·Neo4j 없음
    published = {name: s.get("ports") for name, s in svcs.items() if s.get("ports")}
    assert list(published) == ["caddy"]
    assert sorted(str(p).split(":")[-1] for p in published["caddy"]) == ["443", "80"]


def test_app_runs_factcheck_entrypoint_with_one_worker():
    app = _compose()["services"]["app"]
    cmd = " ".join(app["command"]) if isinstance(app["command"], list) else app["command"]
    assert "app.factcheck_main:app" in cmd
    assert re.search(r"--workers[ =]1\b", cmd)
    assert "--proxy-headers" not in cmd  # 신뢰 프록시는 앱이 FACTCHECK_TRUSTED_PROXIES로 판단한다


def test_restart_healthcheck_and_dependencies():
    svcs = _compose()["services"]
    for name, s in svcs.items():
        assert s.get("restart") == "unless-stopped", name
        assert s.get("healthcheck"), name
    deps = svcs["app"]["depends_on"]
    assert {k: v["condition"] for k, v in deps.items()} == {
        "postgres": "service_healthy", "qdrant": "service_healthy", "ollama": "service_healthy"}
    assert svcs["caddy"]["depends_on"]["app"]["condition"] == "service_started"


def test_secrets_and_data_are_read_only_mounts_not_env_values():
    app = _compose()["services"]["app"]
    vols = [str(v) for v in app["volumes"]]
    assert any(v.endswith(":/root/.config/typesafe/api_key:ro") for v in vols)
    assert any(v.endswith(":/data/factcheck:ro") for v in vols)
    env = app["environment"]
    assert "TYPESAFE_API_KEY" not in env
    assert env["FACTCHECK_ALLOWED_ORIGINS"] == "https://${FACTCHECK_DOMAIN:?Set FACTCHECK_DOMAIN in .env.factcheck}"
    assert env["FACTCHECK_DATA_DIR"] == "/data/factcheck"
    assert env["COOKIE_SECURE"] == "true" and env["TRUST_PROXY"] == "true"


def test_volumes_for_state():
    assert set(_compose()["volumes"]) >= {"postgres_data", "qdrant_data", "ollama_models", "caddy_data",
                                          "caddy_config"}


def test_env_example_has_no_secret_values_and_real_env_is_ignored():
    example = (ROOT / ".env.factcheck.example").read_text(encoding="utf-8")
    values = dict(line.split("=", 1) for line in example.splitlines() if line and not line.startswith("#"))
    assert values["POSTGRES_PASSWORD"] in ("", "change-me")
    assert "TYPESAFE_API_KEY" not in values
    assert all(not re.search(r"(sk-|ts_live|BEGIN [A-Z ]*KEY)", v) for v in values.values())
    out = subprocess.run(["git", "check-ignore", ".env.factcheck"], cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0  # 실제 비밀 파일은 커밋되지 않는다


def test_caddyfile_proxies_domain_to_app():
    caddy = (ROOT / "deploy" / "factcheck" / "Caddyfile").read_text(encoding="utf-8")
    assert "{$FACTCHECK_DOMAIN}" in caddy and "reverse_proxy app:8000" in caddy
