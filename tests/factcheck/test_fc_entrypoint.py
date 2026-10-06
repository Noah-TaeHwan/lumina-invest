# tests/factcheck/test_fc_entrypoint.py
"""팩트체커 별도 진입점(Outside Voice #6)과 기본 앱 회귀.

- app/factcheck_main.py는 필요한 모듈만 import한다: Redis·Neo4j·원본 라우터를 끌어오지 않는다(새 인터프리터에서 확인).
- 공개 경로는 정적 화면·/api/health·/api/factcheck/*뿐이다.
- 기본 앱 app/main.py의 라우터 등록은 그대로다(팩트체커 라우터를 넣지 않는다). app.main은 Python 3.12 문법 파일을
  import하므로(3.11에서 SyntaxError) import하지 않고 소스를 AST로 읽어 등록 순서를 고정한다.
"""
import ast
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

FORBIDDEN = ("app.main", "app.database.neo4j", "app.lib.redis_cache", "app.lib.session", "app.celery_app",
             "app.services.graph_service", "app.services.sync_scheduler", "app.services.langgraph_agent",
             "app.services.evidence.background", "app.services.evidence.store")
ORIGINAL_ROUTERS = ("auth", "chat", "stocks", "library", "admin", "system", "quant", "ml", "macro", "documents",
                    "notification", "graph", "conversations", "tasks", "ingest", "paper", "openapi", "lean",
                    "rebalance", "tradingview", "formula", "evidence", "journal", "watchlist")


def _fresh_import() -> dict:
    code = ("import json, sys; import app.factcheck_main as m; "
            "print(json.dumps({'mods': sorted(k for k in sys.modules if k.startswith(('app.', 'redis', 'neo4j'))), "
            "'paths': sorted({getattr(r, 'path', '') for r in m.app.routes} | set(m.app.openapi()['paths']))}))")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120,
                         env={"PATH": "/usr/bin:/bin", "DATABASE_URL": "postgresql+asyncpg://x:x@localhost/x",
                              "PYTHONPATH": str(ROOT)})
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_factcheck_entrypoint_does_not_import_redis_neo4j_or_original_routers():
    mods = _fresh_import()["mods"]
    assert not [m for m in mods if m in FORBIDDEN or m.startswith(("redis", "neo4j"))], mods
    assert not [m for m in mods if m.startswith("app.routes.") and m.split(".")[2] in ORIGINAL_ROUTERS], mods
    assert "app.routes.factcheck" in mods


def test_factcheck_entrypoint_exposes_only_allowlisted_paths():
    paths = set(_fresh_import()["paths"])
    api = {p for p in paths if p.startswith("/api")}
    assert api == {"/api/health", "/api/factcheck/companies", "/api/factcheck", "/api/factcheck/{job_id}",
                   "/api/factcheck/{job_id}/recheck/{idx}"}
    assert {"/", "/factcheck.html", "/js", "/css"} <= paths
    assert "/app.html" not in paths and "/login.html" not in paths


def _main_router_registrations() -> list[str]:
    """app/main.py에서 app.include_router(<이름>.router)의 <이름>을 순서대로."""
    tree = ast.parse((ROOT / "app" / "main.py").read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "include_router"
                and isinstance(node.args[0], ast.Attribute) and isinstance(node.args[0].value, ast.Name)):
            out.append((node.lineno, node.args[0].value.id))
    return [name for _, name in sorted(out)]


def test_default_app_router_registration_is_unchanged():
    assert _main_router_registrations() == [
        "auth", "ingest", "health", "chat", "stocks", "library", "admin", "system", "quant", "ml", "macro",
        "documents", "notification", "graph", "conversations", "tasks", "paper", "openapi", "lean",
        "rebalance_routes", "tradingview_routes", "formula_routes", "evidence_routes", "journal_routes",
        "watchlist_routes"]
    assert "factcheck" not in (ROOT / "app" / "main.py").read_text(encoding="utf-8")
