"""브라우저 E2E 스모크: 로그인 후 app.html 의 모든 뷰를 열어 활성화 여부·JS 오류를 점검한다.

pytest 수집 대상이 아니며(파일명이 test_* 가 아님) 서버가 떠 있을 때 수동/CI 후속 단계에서 실행한다.
    pip install playwright && playwright install chromium
    BASE_URL=http://localhost:8966 E2E_EMAIL=... E2E_PASSWORD=... python tests/e2e/smoke_views.py
종료 코드 0 = 문제 없음, 1 = 비활성 뷰 또는 pageerror 존재.
"""
import asyncio, os, re, sys

from playwright.async_api import async_playwright

BASE = os.environ.get("BASE_URL", "http://localhost:8966")
EMAIL = os.environ.get("E2E_EMAIL", "rbtest@example.com")
PASSWORD = os.environ.get("E2E_PASSWORD", "Test1234!")
ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


async def main() -> int:
    html = open(os.path.join(ROOT, "public", "app.html"), encoding="utf-8").read()
    views = re.findall(r'class="view" data-view="([a-z-]+)"', html)
    async with async_playwright() as p:
        br = await p.chromium.launch()
        pg = await br.new_page(viewport={"width": 1400, "height": 1000})
        errors: list[str] = []
        pg.on("pageerror", lambda e: errors.append(f"pageerror {e}"))
        await pg.goto(f"{BASE}/login.html")
        await pg.fill("input[name=email]", EMAIL); await pg.fill("input[name=password]", PASSWORD)
        await pg.click("button[type=submit]"); await pg.wait_for_timeout(2500)
        problems = []
        for v in views:
            n0 = len(errors)
            await pg.goto(f"{BASE}/app.html?v={v}#{v}"); await pg.wait_for_timeout(900)
            active = await pg.evaluate(f"document.querySelector('.view[data-view=\"{v}\"]')?.classList.contains('active')")
            if not active or len(errors) > n0:
                problems.append((v, active, errors[n0:n0 + 2]))
        await br.close()
    print(f"views={len(views)} problems={len(problems)} pageerrors={len(errors)}")
    for pr in problems:
        print("  ", pr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
