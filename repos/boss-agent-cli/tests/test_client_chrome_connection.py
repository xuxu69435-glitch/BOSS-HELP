"""Existing Chrome connection tests, with no live browser or platform traffic."""

import time
from unittest.mock import Mock

import pytest

from boss_agent_cli.api import endpoints
from boss_agent_cli.api.client import BossClient
from boss_agent_cli.client_controller import ClientController
from boss_agent_cli.cache.store import CacheStore
from boss_agent_cli.delivery import DeliveryError, collect_batch
from boss_agent_cli.delivery_store import DeliveryStore
from client_fixture import fixture_client
from test_delivery import FakePlatform


@pytest.fixture
def chrome(tmp_path, monkeypatch):
	controller, batch_id = fixture_client(tmp_path)
	bridge = Mock()
	bridge.status.return_value = {"ok": True, "extensionConnected": True, "extensionVersion": "1.1.0"}
	monkeypatch.setattr("boss_agent_cli.client_controller.BridgeClient", Mock(return_value=bridge))
	launch = Mock(side_effect=AssertionError("The existing daemon should be reused"))
	monkeypatch.setattr("boss_agent_cli.client_controller.start_daemon_background", launch)
	tab = Mock()
	monkeypatch.setattr("boss_agent_cli.client_controller.require_existing_boss_tab", tab)
	browser = Mock(_is_cdp=False, _is_bridge=True)
	browser.request.return_value = {"code": 0, "zpData": {}}

	def existing_only(client):
		assert client._browser_source == "existing-browser"
		return browser

	monkeypatch.setattr(BossClient, "_get_browser", existing_only)
	monkeypatch.setattr(
		"boss_agent_cli.auth.manager.AuthManager.get_token", Mock(side_effect=AssertionError("No cookie import"))
	)
	monkeypatch.setattr(
		"boss_agent_cli.api.managed_browser.ManagedBrowserSession._ensure_started",
		Mock(side_effect=AssertionError("No dedicated window")),
	)
	yield controller, batch_id, bridge, browser, tab, launch
	controller.close()


def test_connect_uses_one_read_only_browser_request(chrome):
	controller, _, _, browser, tab, launch = chrome
	controller._run("connect", {})
	assert controller.operation["status"] == "complete"
	assert controller.state()["browser"]["status"] == "ready"
	browser.request.assert_called_once_with("GET", endpoints.USER_INFO_URL, params=None, data=None)
	tab.assert_called_once()
	launch.assert_not_called()


@pytest.mark.parametrize("version", ["1.0.0", "1.0.1", "", "2.0.0", "invalid"])
def test_outdated_extension_stops_before_accessing_the_page(chrome, version):
	controller, _, bridge, browser, tab, _ = chrome
	bridge.status.return_value["extensionVersion"] = version
	controller._run("connect", {})
	assert controller.operation["error"]["code"] == "EXTENSION_UPDATE_REQUIRED"
	assert controller.state()["browser"]["status"] == "needs_connection"
	browser.request.assert_not_called()
	tab.assert_not_called()


@pytest.mark.parametrize("code", [9, 36, 37])
def test_platform_verification_latches_until_explicit_reconnect(chrome, code):
	controller, _, _, browser, _, _ = chrome
	browser.request.return_value = {"code": code, "message": "环境存在异常"}
	controller._run("connect", {})
	assert controller.state()["browser"]["status"] == "blocked"
	controller._run("prepare", {})
	assert browser.request.call_count == 1
	assert controller.batch(controller.operation["batch_id"])["stats"]["network_requests"] == 0
	other = ClientController(controller.data_dir, controller.settings_path)
	try:
		assert other.state()["browser"]["status"] == "blocked"
		other._run("connect", {})
		assert other.state()["browser"]["status"] == "blocked"
		assert browser.request.call_count == 1
	finally:
		other.close()
	controller._browser_state["blocked_until"] = time.time() - 1
	browser.request.return_value = {"code": 0}
	controller._run("connect", {})
	assert controller.state()["browser"]["status"] == "ready"
	assert browser.request.call_count == 2


def test_expired_login_is_not_verified(chrome):
	controller, _, _, browser, _, _ = chrome
	browser.request.return_value = {"code": 37, "message": "登录态过期"}
	controller._run("connect", {})
	assert controller.state()["browser"]["status"] == "needs_login"
	assert controller.operation["error"]["code"] == "CHROME_LOGIN_REQUIRED"
	assert browser.request.call_count == 1


def test_disconnection_stops_before_any_send_claim(chrome, monkeypatch):
	controller, batch_id, bridge, browser, _, _ = chrome
	controller._run("connect", {})
	with DeliveryStore(controller.data_dir) as store:
		store.review(batch_id, approve={1}, reject=set())
	bridge.status.return_value["extensionConnected"] = False
	clock = Mock(monotonic=Mock(side_effect=[0, 10]), time=time.time)
	monkeypatch.setattr("boss_agent_cli.client_controller.time", clock)
	controller._run("send", {"batch_id": batch_id, "limit": 1, "allowed_numbers": {1}})
	assert controller.operation["error"]["code"] == "CHROME_CONNECTION_REQUIRED"
	assert browser.request.call_count == 1
	with DeliveryStore(controller.data_dir) as store:
		assert not store.attempted("fixture-job-1")
		assert store.get(batch_id)["items"][0]["status"] == "approved"


def test_prepare_records_existing_browser_and_keeps_quotas(chrome, monkeypatch):
	controller, _, _, _, _, _ = chrome
	controller._run("connect", {})
	monkeypatch.setattr(controller, "_service_and_resume", lambda params=None: (Mock(), "fixture"))
	collect = Mock()
	draft = Mock()
	monkeypatch.setattr("boss_agent_cli.client_controller.collect_batch", collect)
	monkeypatch.setattr("boss_agent_cli.client_controller.draft_batch", draft)
	controller._run("prepare", {})
	assert controller.operation["status"] == "partial"
	batch = controller.batch(controller.operation["batch_id"])
	assert batch["params"]["browser_source"] == "existing-browser"
	assert batch["params"]["rules"]["recruitment_quotas"] == {"campus": 5, "internship": 5}
	assert batch["params"]["rules"]["min_daily_salary"] == 300
	assert batch["params"]["collection_policy"]["request_limit"] == 20
	assert batch["params"]["continue_after_cooldown"] is True
	assert collect.call_args.kwargs["should_stop"] == controller.cancel.is_set
	draft.assert_called_once()


@pytest.mark.parametrize("before_search", [True, False])
def test_collection_cancel_stops_between_existing_browser_requests(chrome, before_search):
	controller, _, _, _, _, _ = chrome
	platform = FakePlatform()
	with DeliveryStore(controller.data_dir) as store, CacheStore(controller.data_dir / "fixture-cache.db") as cache:
		batch_id = store.create(controller.settings())
		with pytest.raises(DeliveryError) as error:
			collect_batch(
				store, batch_id, platform, cache, Mock(), should_stop=lambda: before_search or bool(platform.searches)
			)
		assert error.value.code == "CANCELLED"
	assert platform.searches == ([] if before_search else [1])
	assert not platform.details and not platform.sent
