"""A successful login never clears a still-refused search endpoint. Offline only."""

import json
from unittest.mock import Mock

import httpx
import pytest

from boss_agent_cli.api import endpoints
from boss_agent_cli.api.zhipin_errors import EnvironmentRiskError
from boss_agent_cli.cache.store import CacheStore
from boss_agent_cli.client_controller import ClientController
from boss_agent_cli.delivery import COLLECTION_POLICY, collect_batch
from boss_agent_cli.delivery_store import DeliveryStore
from test_client_chrome_connection import chrome  # noqa: F401
from test_collection_policy import clock  # noqa: F401
from test_delivery_quotas_history import platform_for


def risk():
	return {"code": 37, "message": "您的环境存在异常.", "_bridge_meta": {
		"source": "api_response", "request_sent": True, "http_status": 200,
		"endpoint": "/wapi/zpgeek/search/joblist.json", "verification": {"detected": False, "evidence": []},
	}}


def pause(controller, bid, test_clock):
	exc = EnvironmentRiskError.from_response(risk())
	with DeliveryStore(controller.data_dir) as store:
		params = store.get(bid)["params"]
		params.update(collection_policy=dict(COLLECTION_POLICY), completion_first=True, continue_after_cooldown=True)
		with store.db:
			store.db.execute("UPDATE batches SET params=? WHERE id=?", (json.dumps(params), bid))
		store.collection_progress(bid, {
			"version": 1, "queue": [["Agent 校招", "上海", 1, "campus", {"experience": "应届生", "experience_code": "102"}]],
			"seen": [], "page_fingerprints": {}, "held_interns": [],
		}, {"network_requests": 29, "last_error": {"diagnostic": exc.diagnostic}}, "environment_risk")
	controller.operation["batch_id"] = bid
	controller._pause_for_risk(exc)
	controller._set_browser_state("blocked", controller._browser_state["message"], blocked_until=test_clock.now - 1)
	controller.operation = {"status": "running", "kind": "connect", "batch_id": None, "logs": []}


@pytest.mark.parametrize("failure", ["code37", "transport", "malformed", "unusable"])
def test_login_success_followed_by_failed_search_stays_blocked(chrome, clock, monkeypatch, failure):  # noqa: F811
	controller, bid, _, browser, _, _ = chrome
	monkeypatch.setattr("boss_agent_cli.client_controller.time", clock)
	pause(controller, bid, clock)
	with DeliveryStore(controller.data_dir) as store:
		before = store.get(bid)["items"]
	if failure == "transport":
		outcome = httpx.ReadTimeout("fixture search timeout")
	else:
		outcome = risk() if failure == "code37" else {"code": 0, "zpData": {}}
		if failure == "unusable":
			outcome = {"code": 0, "zpData": {"jobList": [{"jobName": "No job identifier"}]}}
	browser.request.side_effect = [{"code": 0}, outcome]
	controller._run("connect", {})
	assert controller.operation["status"] == "error"
	assert controller.operation["phase"] == "finished" and controller.operation["wait_until"] is None
	assert controller._browser_state["last_search_check"]["status"] == "error"
	assert controller._browser_state["status"] == "blocked"
	assert controller._search_recovery_required()
	assert browser.request.call_count == 2
	assert browser.request.call_args_list[1].args == ("GET", endpoints.SEARCH_URL)
	assert browser.request.call_args_list[1].kwargs["params"]["query"] == "Agent 校招"
	assert browser.request.call_args_list[1].kwargs["params"]["experience"] == "102"
	with DeliveryStore(controller.data_dir) as store:
		batch = store.get(bid)
		assert batch["items"] == before
		assert batch["stats"]["network_requests"] == 30
		assert batch["stats"]["search_recovery_checks"] == 1
		assert batch["stats"]["last_search_recovery"]["status"] != "verified"
		assert not store.db.execute("SELECT 1 FROM attempts").fetchone()
	other = ClientController(controller.data_dir, controller.settings_path)
	try:
		assert other._search_recovery_required() and other._browser_state["status"] == "blocked"
		assert other._browser_state["last_search_check"] == controller._browser_state["last_search_check"]
	finally:
		other.close()


def test_verified_recovery_caches_exact_page_without_resuming_collection(chrome, clock, monkeypatch):  # noqa: F811
	controller, bid, _, browser, _, _ = chrome
	monkeypatch.setattr("boss_agent_cli.client_controller.time", clock)
	pause(controller, bid, clock)
	browser.request.side_effect = [{"code": 0}, {"code": 0, "zpData": {"jobList": [], "hasMore": False}}]
	controller._run("connect", {})
	assert controller._browser_state["status"] == "ready" and not controller._search_recovery_required()
	assert controller.operation["status"] == "complete"
	assert browser.request.call_count == 2
	with DeliveryStore(controller.data_dir) as store, CacheStore(controller.data_dir / "cache/boss_agent.db") as cache:
		assert len(store.get(bid)["items"]) == 2
		assert store.get(bid)["stats"]["last_search_recovery"]["status"] == "verified"
		assert cache.discovery_budget(limit=20, window_seconds=600)["count"] == 1
		platform = platform_for([], [])
		collect_batch(store, bid, platform, cache, Mock())
		platform.search_jobs.assert_not_called()
		assert store.get(bid)["stats"]["last_run_requests"] == 0
		assert store.get(bid)["stats"]["search_cache_hits"] == 1
		assert not platform.sent


def test_full_budget_cancel_prevents_recovery_search(chrome, clock, monkeypatch):  # noqa: F811
	controller, bid, _, browser, _, _ = chrome
	monkeypatch.setattr("boss_agent_cli.client_controller.time", clock)
	pause(controller, bid, clock)
	with CacheStore(controller.data_dir / "cache/boss_agent.db") as cache:
		for _ in range(20):
			assert cache.reserve_discovery_request(limit=20, window_seconds=600, interval=0)["allowed"]
	controller.cancel.wait = lambda seconds: controller.cancel.set()
	browser.request.return_value = {"code": 0}
	controller._run("connect", {})
	assert controller.operation["status"] == "stopped"
	assert controller._browser_state["status"] == "blocked"
	assert browser.request.call_count == 1


def test_legacy_pause_locates_failed_search_but_never_invents_a_missing_cursor(chrome, clock, monkeypatch):  # noqa: F811
	controller, bid, _, browser, _, _ = chrome
	monkeypatch.setattr("boss_agent_cli.client_controller.time", clock)
	pause(controller, bid, clock)
	controller._browser_state.pop("search_recovery_batch_id")
	controller._browser_state.pop("search_recovery_required")
	with DeliveryStore(controller.data_dir) as store:
		with store.db:
			store.db.execute("DELETE FROM collection_checkpoints WHERE batch_id=?", (bid,))
	browser.request.return_value = {"code": 0}
	controller._run("connect", {})
	assert controller.operation["status"] == "error"
	assert "缺少失败搜索" in controller.operation["error"]["message"]
	assert controller._browser_state["status"] == "blocked" and browser.request.call_count == 1


def test_connection_or_login_failure_and_local_preparation_keep_the_original_search_latch(chrome, clock, monkeypatch):  # noqa: F811
	controller, bid, _, browser, _, _ = chrome
	monkeypatch.setattr("boss_agent_cli.client_controller.time", clock)
	pause(controller, bid, clock)
	controller.operation["batch_id"] = "different-local-only-batch"
	controller._pause_for_risk(EnvironmentRiskError(diagnostic={"source": "existing_pause"}))
	assert controller._browser_state["search_recovery_batch_id"] == bid
	controller.operation["batch_id"] = None
	browser.request.return_value = {"code": 37, "message": "登录态过期"}
	controller._run("connect", {})
	assert controller._browser_state["status"] == "blocked"
	assert controller._search_recovery_required() and browser.request.call_count == 1
	controller.operation = {"status": "running", "kind": "connect", "batch_id": None, "logs": []}
	browser.request.side_effect = [{"code": 0}, risk()]
	controller._run("connect", {})
	assert browser.request.call_count == 3
	assert controller._search_recovery_required() and controller._browser_state["status"] == "blocked"


def test_recovery_waits_for_spacing_before_its_single_search(chrome, clock, monkeypatch):  # noqa: F811
	controller, bid, _, browser, _, _ = chrome
	monkeypatch.setattr("boss_agent_cli.client_controller.time", clock)
	pause(controller, bid, clock)
	controller.cancel.wait = clock.sleep
	with CacheStore(controller.data_dir / "cache/boss_agent.db") as cache:
		cache.put_crawl_budget("delivery-discovery-completed", clock.now)
	browser.request.side_effect = [{"code": 0}, {"code": 0, "zpData": {"jobList": []}}]
	controller._run("connect", {})
	assert clock.now >= 1030 and browser.request.call_count == 2
	with CacheStore(controller.data_dir / "cache/boss_agent.db") as cache:
		entry = cache.get_search({"cache_schema": "delivery-search-v1", "platform": "zhipin", "query": "Agent 校招", "city": "上海", "page": 1, "filters": {"experience": "应届生", "experience_code": "102"}})
		assert json.loads(entry)["jobList"] == []


def test_two_explicit_refusals_never_report_restored_or_repeat_automatically(chrome, clock, monkeypatch):  # noqa: F811
	controller, bid, _, browser, _, _ = chrome
	monkeypatch.setattr("boss_agent_cli.client_controller.time", clock)
	pause(controller, bid, clock)
	for attempt in range(2):
		clock.now += 901
		controller.operation = {"status": "running", "kind": "connect", "batch_id": None, "logs": []}
		browser.request.side_effect = [{"code": 0}, risk()]
		controller._run("connect", {})
		assert controller.operation["status"] == "error"
		assert controller._browser_state["status"] == "blocked"
		assert controller._browser_state["search_recovery_batch_id"] == bid
		assert browser.request.call_count == 2 * (attempt + 1)
		with DeliveryStore(controller.data_dir) as store:
			batch = store.get(bid)
			assert batch["stats"]["network_requests"] == 30 + attempt
			assert batch["checkpoint"]["queue"][0][0] == "Agent 校招"
			assert len(batch["items"]) == 2
