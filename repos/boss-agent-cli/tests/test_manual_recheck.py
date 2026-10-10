"""A local wait override grants one confirmed read probe, never a platform bypass."""

from unittest.mock import Mock

import pytest

from boss_agent_cli.cache.store import CacheStore
from boss_agent_cli.api.browser_source import BrowserSearchPageRequired
from boss_agent_cli.client_controller import ClientController
from boss_agent_cli.delivery_store import DeliveryStore
from test_client_chrome_connection import chrome  # noqa: F401
from test_collection_policy import clock  # noqa: F401
from test_search_recovery import pause, risk


def setup_pause(controller, bid, test_clock):
	pause(controller, bid, test_clock)
	controller.operation = {"status": "idle", "kind": "", "batch_id": None, "logs": []}
	controller._set_browser_state("blocked", controller._browser_state["message"], blocked_until=test_clock.now + 900)
	controller._executor.submit = Mock()
	return {"confirm_once": True, "expected_pause": controller.manual_recheck_state()["revision"]}


def execute(controller):
	call = controller._executor.submit.call_args
	call.args[0](*call.args[1:])


def test_manual_wait_skip_makes_one_probe_and_remains_blocked_on_refusal(chrome, clock, monkeypatch):  # noqa: F811
	controller, bid, _, browser, _, _ = chrome
	monkeypatch.setattr("boss_agent_cli.client_controller.time", clock)
	body = setup_pause(controller, bid, clock)
	until = controller._browser_state["blocked_until"]
	browser.request.side_effect = [{"code": 0}, risk()]
	controller.start("check_now", body)
	assert controller._browser_state["status"] == "blocked" and controller._browser_state["blocked_until"] == until
	clock.now += 8.2
	execute(controller)
	assert browser.request.call_count == 2
	assert controller.operation["status"] == "error" and controller._browser_state["status"] == "blocked"
	assert controller._browser_state["manual_recheck_used"] is True
	result = controller._browser_state["last_search_check"]
	assert result["status"] == "error" and result["error"]["diagnostic"]["response_code"] == 37
	assert result["finished_at"] - result["started_at"] == pytest.approx(8.2)
	assert controller.operation["phase"] == "finished" and controller.operation["wait_until"] is None
	assert not controller.manual_recheck_state()["available"]
	with pytest.raises(ValueError):
		controller.start("check_now", {"confirm_once": True, "expected_pause": controller.manual_recheck_state()["revision"]})
	with pytest.raises(ValueError, match="冷却"):
		controller.start("connect", {"skip_cooldown": True})
	with pytest.raises(ValueError, match="冷却"):
		controller.start("send", {"batch_id": bid, "skip_cooldown": True})
	with DeliveryStore(controller.data_dir) as store:
		assert len(store.get(bid)["items"]) == 2 and not store.db.execute("SELECT 1 FROM attempts").fetchone()
	other = ClientController(controller.data_dir, controller.settings_path)
	try:
		assert other._browser_state["manual_recheck_used"] is True
		assert not other.manual_recheck_state()["available"]
		assert other._browser_state["last_search_check"] == result
	finally:
		other.close()


@pytest.mark.parametrize("kind", ["missing_confirmation", "stale_pause", "page_verification", "account_risk", "unknown_source", "other_endpoint"])
def test_manual_skip_rejects_missing_consent_or_platform_controls(chrome, clock, monkeypatch, kind):  # noqa: F811
	controller, bid, _, browser, _, _ = chrome
	monkeypatch.setattr("boss_agent_cli.client_controller.time", clock)
	body = setup_pause(controller, bid, clock)
	if kind == "missing_confirmation":
		body["confirm_once"] = False
	elif kind == "stale_pause":
		body["expected_pause"] = "old"
	elif kind == "page_verification":
		controller._browser_state["pause_diagnostic"]["page_verification"] = True
	elif kind == "account_risk":
		controller._browser_state["pause_diagnostic"]["response_code"] = 36
	elif kind == "other_endpoint":
		controller._browser_state["pause_diagnostic"]["endpoint"] = "/wapi/zpgeek/user/info.json"
	else:
		controller._browser_state["pause_diagnostic"]["source"] = "unknown"
	if kind not in {"missing_confirmation", "stale_pause"}:
		body["expected_pause"] = controller.manual_recheck_state()["revision"]
		assert not controller.manual_recheck_state()["supported"]
	with pytest.raises(ValueError, match="无法跳过"):
		controller.start("check_now", body)
	browser.request.assert_not_called()
	controller._executor.submit.assert_not_called()


def test_manual_probe_still_obeys_budget_and_can_be_cancelled(chrome, clock, monkeypatch):  # noqa: F811
	controller, bid, _, browser, _, _ = chrome
	monkeypatch.setattr("boss_agent_cli.client_controller.time", clock)
	body = setup_pause(controller, bid, clock)
	with CacheStore(controller.data_dir / "cache/boss_agent.db") as cache:
		for _ in range(20):
			assert cache.reserve_discovery_request(limit=20, window_seconds=600, interval=0)["allowed"]
	controller.cancel.wait = lambda seconds: controller.cancel.set()
	browser.request.return_value = {"code": 0}
	controller.start("check_now", body)
	execute(controller)
	assert browser.request.call_count == 1
	assert controller.operation["status"] == "stopped"
	assert controller.operation["phase"] == "finished" and controller.operation["wait_until"] is None
	assert controller._browser_state["last_search_check"]["status"] == "stopped"
	assert controller._browser_state["status"] == "blocked" and controller._browser_state["manual_recheck_used"] is True


def test_manual_success_allows_later_resume_but_never_starts_it(chrome, clock, monkeypatch):  # noqa: F811
	controller, bid, _, browser, _, _ = chrome
	monkeypatch.setattr("boss_agent_cli.client_controller.time", clock)
	body = setup_pause(controller, bid, clock)
	controller.cancel.wait = clock.sleep
	with CacheStore(controller.data_dir / "cache/boss_agent.db") as cache:
		cache.put_crawl_budget("delivery-discovery-completed", clock.now)
	browser.request.side_effect = [{"code": 0}, {"code": 0, "zpData": {"jobList": []}}]
	controller.start("check_now", body)
	execute(controller)
	assert clock.now >= 1030
	assert controller.operation["status"] == "complete" and controller._browser_state["status"] == "ready"
	assert controller.operation["phase"] == "finished" and controller.operation["wait_until"] is None
	assert controller._browser_state["last_search_check"]["status"] == "complete"
	assert controller._executor.submit.call_count == 1
	assert browser.request.call_count == 2
	with DeliveryStore(controller.data_dir) as store:
		assert len(store.get(bid)["items"]) == 2
		assert store.get(bid)["stats"]["last_search_recovery"]["finished_at"] >= 1030
		assert not store.db.execute("SELECT 1 FROM attempts").fetchone()


def test_direct_manual_operation_without_authorized_scope_is_rejected(chrome, clock, monkeypatch):  # noqa: F811
	controller, bid, _, browser, _, _ = chrome
	monkeypatch.setattr("boss_agent_cli.client_controller.time", clock)
	setup_pause(controller, bid, clock)
	controller._run("check_now", {"confirm_once": True})
	browser.request.assert_not_called()
	assert controller._browser_state["status"] == "blocked"


def test_new_pause_does_not_display_an_old_success(chrome, clock, monkeypatch):  # noqa: F811
	controller, bid, _, browser, _, _ = chrome
	monkeypatch.setattr("boss_agent_cli.client_controller.time", clock)
	body = setup_pause(controller, bid, clock)
	browser.request.side_effect = [{"code": 0}, {"code": 0, "zpData": {"jobList": []}}]
	controller.start("check_now", body)
	execute(controller)
	assert controller._browser_state["last_search_check"]["status"] == "complete"
	pause(controller, bid, clock)
	assert controller._browser_state["last_search_check"] is None


def test_wrong_page_never_reissues_search_or_resets_platform_pause(chrome, clock, monkeypatch):  # noqa: F811
	controller, bid, _, browser, _, _ = chrome
	monkeypatch.setattr("boss_agent_cli.client_controller.time", clock)
	body = setup_pause(controller, bid, clock)
	before = dict(controller._browser_state)
	check = Mock(side_effect=BrowserSearchPageRequired("/web/geek/chat"))
	monkeypatch.setattr("boss_agent_cli.client_controller.require_existing_boss_tab", check)
	controller.start("check_now", body)
	execute(controller)
	check.assert_called_once_with(for_search=True)
	browser.request.assert_not_called()
	assert controller._browser_state["status"] == "blocked"
	assert controller._browser_state["blocked_until"] == before["blocked_until"]
	assert controller._browser_state["pause_diagnostic"] == before["pause_diagnostic"]
	assert controller.operation["error"]["code"] == "BROWSER_SEARCH_PAGE_REQUIRED"
	assert controller.operation["error"]["diagnostic"]["request_sent"] is False
	with DeliveryStore(controller.data_dir) as store:
		assert store.get(bid)["stats"]["network_requests"] == 29
		assert not store.db.execute("SELECT 1 FROM attempts").fetchone()
