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


def _gitignored(path: str) -> bool:
    """저장소 루트 .gitignore 규칙으로 path가 무시되는지(git 없이). 지원: 주석·빈 줄, `!` 부정, 끝 `/`(디렉터리),
    `/`가 들어간 패턴은 루트 기준, 아니면 어느 경로 조각에나 맞춘다. 마지막으로 맞은 규칙이 이긴다."""
    import fnmatch

    gi = ROOT / ".gitignore"
    if not gi.exists():
        import pytest

        pytest.skip(".gitignore가 없는 트리(배포 이미지 등)")
    parts = path.strip("/").split("/")
    prefixes = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
    ignored = False
    for raw in gi.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negate = line.startswith("!")
        pat = line[1:] if negate else line
        dir_only = pat.endswith("/")
        pat = pat.strip("/")
        if "/" in pat:  # 루트 기준
            cands = prefixes[:-1] if dir_only else prefixes
            hit = any(fnmatch.fnmatchcase(c, pat) for c in cands)
        else:  # 어느 조각에나
            cands = parts[:-1] if dir_only else parts
            hit = any(fnmatch.fnmatchcase(c, pat) for c in cands)
        if hit:
            ignored = not negate
    return ignored


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
    assert _gitignored(".env.factcheck")  # 실제 비밀 파일은 커밋되지 않는다


def test_caddyfile_proxies_domain_to_app():
    caddy = (ROOT / "deploy" / "factcheck" / "Caddyfile").read_text(encoding="utf-8")
    assert "{$FACTCHECK_DOMAIN}" in caddy and "reverse_proxy app:8000" in caddy


# ── #64 검수 반영 ───────────────────────────────────────────────────────────

def test_snapshot_files_are_ignored_and_default_output_is_under_ignored_data_dir():
    """스냅샷(DART 파생, 수백 MB)이 public 저장소 작업 트리에 커밋 가능한 채로 생기지 않게(#64 검수 1)."""
    for path in ("factcheck_passages-20261006.snapshot", "deploy/x.snapshot"):
        assert _gitignored(path), path
    script = (ROOT / "deploy" / "factcheck" / "snapshot_create.sh").read_text(encoding="utf-8")
    default = re.search(r'OUT="\$\{1:-([^}]+)\}"', script).group(1)
    assert default.startswith("lab/data/factcheck/")
    assert _gitignored("lab/data/factcheck/snapshots/a")


def test_trusted_proxy_is_exactly_caddy_address_and_caddy_does_not_trust_forwarded():
    """앱이 믿는 프록시 = Caddy 고정 주소, Caddy는 들어온 XFF를 믿지 않는다. 한쪽만 바뀌면 모든 사용자가 Caddy 주소 키 하나로
    묶여 사이트 전체가 하루 3회를 나눠 쓰는 조용한 회귀가 된다(#64 검수 3)."""
    svcs = _compose()["services"]
    caddy_ip = svcs["caddy"]["networks"]["edge"]["ipv4_address"]
    assert svcs["app"]["environment"]["FACTCHECK_TRUSTED_PROXIES"] == f"{caddy_ip}/32"
    caddy = (ROOT / "deploy" / "factcheck" / "Caddyfile").read_text(encoding="utf-8")
    directives = "\n".join(line.split("#", 1)[0] for line in caddy.splitlines())  # 주석은 빼고 지시어만
    assert "trusted_proxies" not in directives


def test_edge_dynamic_range_excludes_caddy_fixed_address():
    import ipaddress

    compose = _compose()
    pool = compose["networks"]["edge"]["ipam"]["config"][0]
    caddy_ip = ipaddress.ip_address(compose["services"]["caddy"]["networks"]["edge"]["ipv4_address"])
    assert pool["ip_range"] == "172.30.1.128/25"
    assert caddy_ip in ipaddress.ip_network(pool["subnet"]) and caddy_ip not in ipaddress.ip_network(pool["ip_range"])


def test_app_disables_uvicorn_proxy_headers_explicitly():
    cmd = _compose()["services"]["app"]["command"]
    assert "--no-proxy-headers" in cmd  # uvicorn 기본은 켜짐(127.0.0.1만 신뢰) — 명시적으로 끈다


def test_ollama_image_and_embed_model_are_pinned():
    svcs = _compose()["services"]
    assert not svcs["ollama"]["image"].endswith(":latest")
    assert "${OLLAMA_TAG:?" in svcs["ollama"]["image"]
    model = svcs["app"]["environment"]["EMBED_MODEL"]
    assert model.startswith("${OLLAMA_EMBED_MODEL:?")
    assert "${OLLAMA_EMBED_MODEL" in " ".join(svcs["ollama"]["entrypoint"])  # 받는 모델 = 앱이 쓰는 모델
    example = (ROOT / ".env.factcheck.example").read_text(encoding="utf-8")
    assert "OLLAMA_TAG=" in example and "OLLAMA_EMBED_MODEL=" in example


def _fake_docker(tmp_path, fail_on: str | None = None):
    """docker 대신 호출 인자를 한 줄씩 적는 가짜 실행 파일. fail_on이 인자에 있으면 실패한다."""
    bindir, log = tmp_path / "bin", tmp_path / "docker.log"
    bindir.mkdir()
    fake = bindir / "docker"
    fake.write_text("#!/usr/bin/env bash\n"
                    f'echo "$*" >> "{log}"\n'
                    + (f'case "$*" in *"{fail_on}"*) exit 1;; esac\n' if fail_on else "")
                    + "exit 0\n")
    fake.chmod(0o755)
    return bindir, log


def _run_restore(tmp_path, fail_on=None):
    import os

    bindir, log = _fake_docker(tmp_path, fail_on)
    snap = tmp_path / "x.snapshot"
    snap.write_bytes(b"snap")
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}
    out = subprocess.run(["bash", str(ROOT / "deploy" / "factcheck" / "snapshot_restore.sh"), str(snap)], cwd=ROOT,
                         env=env, capture_output=True, text=True, timeout=60)
    return out, log.read_text().splitlines()


def test_restore_stops_app_during_restore_and_waits_for_qdrant(tmp_path):
    """서비스 중 컬렉션을 덮어쓰면 검수가 빈 컬렉션을 볼 수 있다: 앱을 멈추고 복원한 뒤 다시 켠다(#64 검수 5)."""
    out, calls = _run_restore(tmp_path)
    assert out.returncode == 0, out.stderr
    idx = {k: next(i for i, c in enumerate(calls) if c.endswith(k) or f" {k} " in f" {c} ")
           for k in ("up -d --wait qdrant", "stop app", "start app")}
    i_run = next(i for i, c in enumerate(calls) if " run " in f" {c} ")
    assert idx["up -d --wait qdrant"] < idx["stop app"] < i_run < idx["start app"]
    assert sum(c.endswith("start app") for c in calls) == 1
    doc = (ROOT / "docs" / "deploy" / "factcheck.md").read_text(encoding="utf-8")
    section5 = doc[doc.index("## 5."):doc.index("## 6.")]
    assert "stop app" in section5 and "start app" in section5


def test_restore_failure_still_starts_app(tmp_path):
    """복원(run 단계)이 실패해도 종료할 때 앱을 다시 켠다 — 데모가 꺼진 채 남지 않게(trap)."""
    out, calls = _run_restore(tmp_path, fail_on=" run ")
    assert out.returncode != 0
    assert any(c.endswith("stop app") for c in calls)
    assert calls[-1].endswith("start app")


def test_git_dependent_checks_work_without_git(monkeypatch):
    """앱 이미지(git 없음)·git archive 트리(.git 없음)에서도 무시 규칙 검사가 돈다 — .gitignore를 직접 읽는다."""
    real = subprocess.run

    def no_git(args, *a, **kw):
        if args and args[0] == "git":
            raise FileNotFoundError("git")
        return real(args, *a, **kw)

    monkeypatch.setattr(subprocess, "run", no_git)
    test_env_example_has_no_secret_values_and_real_env_is_ignored()
    test_snapshot_files_are_ignored_and_default_output_is_under_ignored_data_dir()


def test_gitignore_matcher_follows_gitignore_rules():
    assert _gitignored(".env.factcheck") and _gitignored("deploy/x.snapshot")
    assert _gitignored("lab/data/factcheck/snapshots/a")
    assert not _gitignored(".env.factcheck.example") and not _gitignored("compose.factcheck.yml")
    assert not _gitignored("lab/evidence/attempts.jsonl")


def test_doc_explains_auth_probe_charge_while_blocked():
    """키가 막힌 동안 시험 호출(401)은 사용량 값이 없어 회당 요청 상한으로 auth-probe 키 한도에 정산된다(#64 후속 3)."""
    doc = (ROOT / "docs" / "deploy" / "factcheck.md").read_text(encoding="utf-8")
    assert "auth-probe" in doc and "28,000" in doc and "89회" in doc and "재시작" in doc
