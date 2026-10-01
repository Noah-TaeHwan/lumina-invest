"""실제 lifespan의 동기화 시작 분기를 외부 서비스 없이 실행한다."""
import ast
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock


ROOT = Path(__file__).resolve().parents[1]


class StartupDataSyncTest(unittest.TestCase):
    """기본 동작과 로컬 비활성화 및 종료 정리를 확인한다."""

    def test_startup_sync_switch_and_cleanup(self):
        """실제 lifespan 함수를 추출해 두 설정값의 호출 차이를 검사한다."""
        config = ast.parse((ROOT / "app/config.py").read_text())
        settings_class = next(node for node in config.body if isinstance(node, ast.ClassDef) and node.name == "Settings")
        default = next(node.value for node in settings_class.body if isinstance(node, ast.AnnAssign)
                       and node.target.id == "STARTUP_DATA_SYNC_ENABLED")
        self.assertIs(ast.literal_eval(default), True)
        module = ast.parse((ROOT / "app/main.py").read_text())
        lifespan = next(node for node in module.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan")
        code = compile(ast.Module(body=[lifespan], type_ignores=[]), str(ROOT / "app/main.py"), "exec")
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                namespace = {"asynccontextmanager": asynccontextmanager, "FastAPI": object,
                             "settings": SimpleNamespace(RUN_MIGRATIONS_ON_STARTUP=False, STARTUP_DATA_SYNC_ENABLED=enabled),
                             "start_sync_scheduler": Mock(), "stop_sync_scheduler": Mock()}
                for name in ("connect_redis", "connect_postgres", "connect_neo4j", "ensure_graph_schema", "seed_graph",
                             "close_redis", "close_postgres", "close_neo4j"):
                    namespace[name] = AsyncMock()
                exec(code, namespace)

                async def run():
                    async with namespace["lifespan"](object()):
                        self.assertEqual(namespace["start_sync_scheduler"].call_count, int(enabled))
                        namespace["stop_sync_scheduler"].assert_not_called()

                asyncio.run(run())
                namespace["stop_sync_scheduler"].assert_called_once_with()
                for name in ("close_redis", "close_postgres", "close_neo4j"):
                    namespace[name].assert_awaited_once_with()


if __name__ == "__main__":
    unittest.main()
