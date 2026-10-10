"""Existing-browser mode must never silently launch or import credentials."""

from unittest.mock import Mock

import pytest

from boss_agent_cli.api.browser_client import BrowserSession
from boss_agent_cli.api.browser_source import BrowserSearchPageRequired, BrowserSessionNotFound, require_existing_boss_tab
from boss_agent_cli.api.zhipin_errors import EnvironmentRiskError
from boss_agent_cli.api.client import BossClient
from boss_agent_cli.commands._platform import get_platform_instance
from test_api_client_retry import FakeAuthManager


def test_existing_browser_missing_stops_before_playwright_or_headless(monkeypatch):
	session = BrowserSession(cookies={}, user_agent="", existing_browser_only=True)
	monkeypatch.setattr(session, "_try_bridge", lambda: False)
	playwright = Mock()
	monkeypatch.setattr("boss_agent_cli.api.browser_client.sync_playwright", playwright)
	with pytest.raises(BrowserSessionNotFound):
		session._ensure_started()
	playwright.assert_not_called()
	assert session._browser is None


def test_existing_browser_does_not_read_or_copy_stored_cookies(monkeypatch):
	auth = Mock()
	auth.get_token.side_effect = AssertionError("Must not read stored cookies")
	with BossClient(auth, browser_source="existing-browser") as client:
		session = client._get_browser()
		assert session._existing_browser_only
		assert session._cookies == {}
		assert session._user_agent == ""
		auth.get_token.assert_not_called()


def test_existing_browser_close_keeps_user_tabs_open():
	session = BrowserSession(cookies={}, user_agent="", existing_browser_only=True)
	session._is_bridge = True
	bridge = Mock()
	session._bridge_client = bridge
	session.close()
	bridge.close_window.assert_not_called()


def test_existing_bridge_commands_have_one_attempt(monkeypatch):
	bridge = Mock()
	bridge.is_running.return_value = True
	bridge.is_extension_connected.return_value = True
	factory = Mock(return_value=bridge)
	monkeypatch.setattr("boss_agent_cli.bridge.client.BridgeClient", factory)
	session = BrowserSession(cookies={}, user_agent="", existing_browser_only=True)
	assert session._try_bridge()
	factory.assert_called_once_with(max_retries=1)
	session.close()


def test_context_selects_existing_source_without_changing_default(monkeypatch):
	factory = Mock()
	monkeypatch.setattr("boss_agent_cli.commands._platform.BossClient", factory)
	auth = FakeAuthManager()
	ctx = Mock(obj={"platform": "zhipin", "browser_source": "existing-browser", "delay": (5, 8)})
	get_platform_instance(ctx, auth)
	factory.assert_called_once_with(auth, delay=(5, 8), cdp_url=None, browser_source="existing-browser")


@pytest.mark.parametrize(
	"data,error",
	[
		({"host": "www.zhipin.com", "path": "/web/geek/jobs", "verificationVisible": False}, None),
		({"host": "www.zhipin.com", "path": "/beijing/", "verificationVisible": False}, None),
		({"host": "www.zhipin.com", "path": "/shanghai/", "verificationVisible": False}, None),
		({"host": "www.zhipin.com", "path": "/", "verificationVisible": False}, None),
		({"host": "www.zhipin.com", "path": "/job_detail/job.html", "verificationVisible": True}, EnvironmentRiskError),
		({"host": "www.zhipin.com", "path": "/beijing/", "verificationVisible": True}, EnvironmentRiskError),
		({"host": "example.com", "path": "/web/geek/jobs"}, BrowserSessionNotFound),
		({"host": "www.zhipin.com", "path": "/web/user/login"}, BrowserSessionNotFound),
		({"host": "www.zhipin.com", "path": "/login/"}, BrowserSessionNotFound),
		({"host": "www.zhipin.com", "path": "/verify/"}, BrowserSessionNotFound),
	],
)
def test_existing_tab_preflight_is_read_only(monkeypatch, data, error):
	bridge = Mock()
	bridge.is_extension_connected.return_value = True
	bridge.send_command.return_value = Mock(ok=True, data=data)
	monkeypatch.setattr("boss_agent_cli.api.browser_source.BridgeClient", lambda **kwargs: bridge)
	if error:
		with pytest.raises(error):
			require_existing_boss_tab()
	else:
		require_existing_boss_tab()
	assert bridge.send_command.call_count == 1
	assert bridge.send_command.call_args.args == ("inspect",)
	assert "code" not in bridge.send_command.call_args.kwargs


def test_stale_tab_id_does_not_claim_the_user_closed_the_page(monkeypatch):
	bridge = Mock()
	bridge.is_extension_connected.return_value = True
	bridge.send_command.return_value = Mock(ok=False, error="No tab with id: 123")
	monkeypatch.setattr("boss_agent_cli.api.browser_source.BridgeClient", lambda **kwargs: bridge)
	with pytest.raises(BrowserSessionNotFound, match="不代表你关闭了网页"):
		require_existing_boss_tab()


@pytest.mark.parametrize("path,allowed", [("/web/geek/jobs", True), ("/web/geek/job/", True), ("/web/geek/chat", False), ("/beijing/", False), ("/job_detail/fixture.html", False)])
def test_search_preflight_requires_a_search_page_without_fetching(monkeypatch, path, allowed):
	bridge = Mock()
	bridge.is_extension_connected.return_value = True
	bridge.send_command.return_value = Mock(ok=True, data={"host": "www.zhipin.com", "path": path, "verificationVisible": False})
	monkeypatch.setattr("boss_agent_cli.api.browser_source.BridgeClient", lambda **kwargs: bridge)
	if allowed:
		require_existing_boss_tab(for_search=True)
	else:
		with pytest.raises(BrowserSearchPageRequired) as exc:
			require_existing_boss_tab(for_search=True)
		assert exc.value.diagnostic == {"source": "page_context", "request_sent": False, "page_path": path}
	bridge.send_command.assert_called_once_with("inspect", workspace="boss")
	bridge.fetch_json.assert_not_called()


def test_chat_preflight_remains_available_for_approved_messages(monkeypatch):
	bridge = Mock()
	bridge.is_extension_connected.return_value = True
	bridge.send_command.return_value = Mock(ok=True, data={"host": "www.zhipin.com", "path": "/web/geek/chat"})
	monkeypatch.setattr("boss_agent_cli.api.browser_source.BridgeClient", lambda **kwargs: bridge)
	require_existing_boss_tab()
	bridge.fetch_json.assert_not_called()
