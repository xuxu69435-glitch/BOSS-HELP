"""Dedicated profile isolation and stop behavior, without real BOSS traffic."""

import json
from threading import Event
from unittest.mock import Mock

import pytest

from boss_agent_cli.api import endpoints
from boss_agent_cli.api.client import BossClient
from boss_agent_cli.api.managed_browser import (
	ManagedBrowserError,
	ManagedBrowserSession,
	OperationCancelled,
	ProfileLock,
	browser_state,
)
from boss_agent_cli.api.zhipin_errors import EnvironmentRiskError


@pytest.fixture
def managed(tmp_path, monkeypatch):
	monkeypatch.setattr(
		"boss_agent_cli.api.managed_browser._private_directory", lambda p: p.mkdir(parents=True, exist_ok=True)
	)
	page = Mock()
	page.url = "https://www.zhipin.com/web/user/"
	page.is_closed.return_value = False
	page.evaluate.side_effect = [
		{"host": "www.zhipin.com", "path": "/", "verification": False},
		{"code": 0, "zpData": {}},
	]
	context = Mock(pages=[page])
	context.cookies.return_value = [{"name": "wt2", "value": "test-secret-never-exported"}]
	engine = Mock()
	engine.chromium.launch_persistent_context.return_value = context
	start = Mock(return_value=engine)
	monkeypatch.setattr("boss_agent_cli.api.managed_browser._start_playwright", start)
	return page, context, engine, start


def ready_state(data_dir):
	directory = data_dir / "managed-browser"
	directory.mkdir(parents=True, exist_ok=True)
	(directory / "state.json").write_text(json.dumps({"status": "ready"}), encoding="utf-8")


def test_managed_mode_never_reads_old_cookies_or_uses_bridge(tmp_path, managed, monkeypatch):
	auth = Mock()
	auth.get_token.side_effect = AssertionError("No old credentials")
	bridge = Mock(side_effect=AssertionError("No browser fallback"))
	monkeypatch.setattr("boss_agent_cli.api.browser_client.BrowserSession._try_bridge", bridge)
	ready_state(tmp_path)
	with BossClient(auth, browser_source="managed", managed_data_dir=tmp_path) as client:
		client.prepare_managed_browser()
		options = managed[2].chromium.launch_persistent_context.call_args
		assert options.args == (str(tmp_path / "managed-browser/profile"),)
		assert options.kwargs["headless"] is True
		assert options.kwargs["chromium_sandbox"] is True
		assert options.kwargs["ignore_https_errors"] is False
		assert "args" not in options.kwargs
		managed[1].add_cookies.assert_not_called()
		auth.get_token.assert_not_called()
		bridge.assert_not_called()
	assert browser_state(tmp_path)["status"] == "ready"
	assert "test-secret" not in (tmp_path / "managed-browser/state.json").read_text(encoding="utf-8")
	managed[1].close.assert_called_once()


def test_missing_session_does_not_launch_or_import_credentials(tmp_path, managed):
	with ManagedBrowserSession(tmp_path) as session:
		with pytest.raises(ManagedBrowserError, match="先在控制台登录"):
			session.prepare()
	managed[3].assert_not_called()


@pytest.mark.parametrize("code", [9, 36, 37])
def test_risk_persists_and_new_operations_do_not_retry(tmp_path, managed, code):
	session = ManagedBrowserSession(tmp_path)
	session.directory.mkdir()
	session._ready = True
	session._started = True
	session._page = managed[0]
	managed[0].evaluate.side_effect = None
	managed[0].evaluate.return_value = {"code": code, "message": "环境存在异常"}
	with pytest.raises(EnvironmentRiskError):
		session.request("GET", endpoints.SEARCH_URL)
	assert browser_state(tmp_path)["status"] == "blocked"
	with ManagedBrowserSession(tmp_path) as second:
		with pytest.raises(EnvironmentRiskError):
			second.prepare()
	managed[3].assert_not_called()


def test_local_profile_is_exclusive_and_released(tmp_path):
	first, second = ProfileLock(tmp_path / "lock"), ProfileLock(tmp_path / "lock")
	first.acquire()
	with pytest.raises(ManagedBrowserError, match="另一项任务"):
		second.acquire()
	first.close()
	second.acquire()
	second.close()


def test_visible_login_is_explicit_and_does_not_search_or_send(tmp_path, managed):
	confirmed = Event()
	confirmed.set()
	with ManagedBrowserSession(tmp_path, headless=False) as session:
		session.login(confirmed)
	assert managed[2].chromium.launch_persistent_context.call_args.kwargs["headless"] is False
	assert managed[0].goto.call_args.args[0] == "https://www.zhipin.com/web/user/"
	assert managed[0].evaluate.call_args.args[1]["url"] == endpoints.USER_INFO_URL
	assert managed[0].evaluate.call_args.args[1]["method"] == "GET"
	assert browser_state(tmp_path)["status"] == "ready"


def test_login_window_closed_without_confirmation_is_not_ready(tmp_path, managed):
	managed[0].is_closed.return_value = True
	with ManagedBrowserSession(tmp_path, headless=False) as session:
		with pytest.raises(ManagedBrowserError, match="窗口已关闭"):
			session.login(Event())
	managed[0].evaluate.assert_not_called()
	assert browser_state(tmp_path)["status"] == "needs_login"


def test_login_rejects_blank_page_before_asking_for_confirmation(tmp_path, managed):
	managed[0].url = "about:blank"
	with ManagedBrowserSession(tmp_path, headless=False) as session:
		with pytest.raises(ManagedBrowserError) as error:
			session.login(Event())
	assert error.value.code == "LOGIN_PAGE_UNAVAILABLE"
	assert browser_state(tmp_path)["status"] == "needs_login"
	assert "空白" in browser_state(tmp_path)["message"]
	managed[1].cookies.assert_not_called()
	managed[0].goto.assert_called_once()


def test_login_detects_page_turning_blank_while_waiting(tmp_path, managed):
	managed[0].wait_for_timeout.side_effect = lambda _: setattr(managed[0], "url", "about:blank")
	with ManagedBrowserSession(tmp_path, headless=False) as session:
		with pytest.raises(ManagedBrowserError) as error:
			session.login(Event())
	assert error.value.code == "LOGIN_PAGE_UNAVAILABLE"
	managed[0].wait_for_timeout.assert_called_once_with(250)
	managed[1].cookies.assert_not_called()
	managed[0].goto.assert_called_once()


def test_login_content_timeout_is_not_reported_as_waiting_for_login(tmp_path, managed):
	managed[0].wait_for_function.side_effect = TimeoutError("no rendered content")
	with ManagedBrowserSession(tmp_path, headless=False) as session:
		with pytest.raises(ManagedBrowserError, match="未能加载出内容"):
			session.login(Event())
	assert browser_state(tmp_path)["status"] == "needs_login"
	managed[1].cookies.assert_not_called()


def test_early_confirmation_keeps_window_open_until_user_confirms_again(tmp_path, managed):
	page, context = managed[:2]
	confirmed = Event()
	confirmed.set()
	context.cookies.side_effect = [[], [{"name": "wt2", "value": "fixture-only"}]]
	flags = {"host": "www.zhipin.com", "path": "/web/user/", "verification": False}
	page.evaluate.side_effect = [flags, flags, {"code": 0}]

	def confirm_after_login(_):
		assert not confirmed.is_set()
		context.close.assert_not_called()
		assert browser_state(tmp_path)["status"] == "waiting_login"
		confirmed.set()

	page.wait_for_timeout.side_effect = confirm_after_login
	with ManagedBrowserSession(tmp_path, headless=False) as session:
		session.login(confirmed)
	assert browser_state(tmp_path)["status"] == "ready"
	page.wait_for_timeout.assert_called_once_with(250)


def test_cancelled_read_does_not_start_browser(tmp_path, managed):
	cancel = Event()
	cancel.set()
	with ManagedBrowserSession(tmp_path, cancel=cancel) as session:
		with pytest.raises(OperationCancelled):
			session.request("GET", endpoints.SEARCH_URL)
	managed[3].assert_not_called()


def test_network_uncertainty_is_not_a_fake_success(tmp_path, managed):
	session = ManagedBrowserSession(tmp_path)
	session._page = managed[0]
	session._ready = True
	managed[0].evaluate.side_effect = None
	managed[0].evaluate.return_value = {"code": -1}
	with pytest.raises(ManagedBrowserError, match="不会自动重试"):
		session.request("POST", endpoints.GREET_URL, data={"greeting": "hello"})


def test_managed_source_requires_explicit_directory():
	with pytest.raises(ValueError, match="explicit local"):
		BossClient(Mock(), browser_source="managed")
