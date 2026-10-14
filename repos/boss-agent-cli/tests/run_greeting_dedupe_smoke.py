"""Verify greeting-only deduplication UI with synthetic data and loopback requests."""

import json
import sys
import threading
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright

from boss_agent_cli.delivery_store import DeliveryStore
from boss_agent_cli.local_client import ClientServer
from client_fixture import fixture_client


def main():
	root = Path(sys.argv[1]).resolve()
	root.mkdir(parents=True, exist_ok=True)
	controller, batch_id = fixture_client(root / "data")
	controller.start = Mock(side_effect=AssertionError("No platform actions during UI verification"))
	with DeliveryStore(controller.data_dir) as store:
		original_items = store.get(batch_id)["items"]
		store.progress(batch_id, {"excluded": [
			{"job_id": "greeted-fixture", "title": "已打招呼的合成岗位", "company": "测试企业", "reasons": ["已打招呼的岗位"]},
			{"job_id": "legacy-fixture", "title": "<script>window.dedupeXss=true</script>", "reasons": ["历史批次已入选同一岗位"]},
			{"title": "薪资不足的合成岗位", "reasons": ["薪资低于下限"]},
		]})
	server = ClientServer(("127.0.0.1", 0), controller)
	thread = threading.Thread(target=server.serve_forever, daemon=True)
	thread.start()
	errors, posts = [], []
	try:
		with sync_playwright() as pw:
			browser = pw.chromium.launch(channel="chrome", headless=True, chromium_sandbox=True)
			try:
				page = browser.new_page(viewport={"width": 1440, "height": 1000})
				page.on("pageerror", lambda error: errors.append(str(error)))

				def guard(route):
					assert urlparse(route.request.url).netloc == urlparse(server.origin).netloc
					if route.request.method == "POST":
						posts.append(urlparse(route.request.url).path)
					route.continue_()

				page.route("**/*", guard)
				page.goto(server.origin + "#client_token=" + server.token)
				page.get_by_role("button", name="筛选条件", exact=True).click()
				expect(page.locator('input[name="history_dedupe"]')).to_have_count(0)
				expect(page.locator("#settings-form")).to_contain_text("仅跳过已经打过招呼的岗位")
				expect(page.locator('#dedupe-history [role="listitem"]')).to_have_count(2)
				expect(page.locator("#dedupe-history")).not_to_contain_text("薪资不足的合成岗位")
				assert page.evaluate("() => window.dedupeXss") is None
				assert page.locator("#dedupe-history script").count() == 0
				with page.expect_response("**/api/settings") as saved:
					page.locator("#save-settings").click()
				assert saved.value.status == 200
				assert controller.settings()["rules"]["history_dedupe"] == "greeted"
				with DeliveryStore(controller.data_dir) as store:
					assert store.clear_dedupe_history() == 2
					assert store.get(batch_id)["items"] == original_items
					assert not store.db.execute("SELECT 1 FROM attempts").fetchone()
				expect(page.locator('#dedupe-history [role="listitem"]')).to_have_count(0)
				expect(page.locator("#dedupe-summary")).to_have_text("暂无去重记录，生成批次后会显示已跳过的已打招呼岗位。")
				page.screenshot(path=str(root / "greeting-dedupe-desktop.png"), full_page=True)
				page.reload()
				page.get_by_role("button", name="筛选条件", exact=True).click()
				expect(page.locator('#dedupe-history [role="listitem"]')).to_have_count(0)
				for width in (768, 390, 320):
					page.set_viewport_size({"width": width, "height": 844})
					assert page.evaluate("() => document.documentElement.scrollWidth <= innerWidth"), width
					page.screenshot(path=str(root / f"greeting-dedupe-{width}.png"), full_page=True)
				assert not errors, errors
				assert posts == ["/api/settings"]
				controller.start.assert_not_called()
			finally:
				browser.close()
		print(json.dumps({"ok": True, "history_cleared": True, "settings_saved": True, "browser_errors": errors, "boss_requests": 0}))
	finally:
		server.shutdown()
		server.server_close()
		thread.join(timeout=5)
		controller.close()


if __name__ == "__main__":
	main()
