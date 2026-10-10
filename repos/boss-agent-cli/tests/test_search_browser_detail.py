"""Search and card-detail requests must stay in one selected browser session."""

from unittest.mock import Mock

import pytest

from boss_agent_cli.api import endpoints
from boss_agent_cli.api.client import BossClient
from boss_agent_cli.api.browser_source import BrowserSearchPageRequired
from boss_agent_cli.api.zhipin_errors import EnvironmentRiskError
from boss_agent_cli.platforms.zhipin import BossPlatform
from test_api_client_retry import FakeAuthManager


def test_search_and_card_reuse_the_same_browser_without_httpx():
	with BossClient(FakeAuthManager()) as client:
		browser = Mock()
		browser.request.side_effect = [
			{"code": 0, "zpData": {"jobList": []}},
			{"code": 0, "zpData": {"jobCard": {"postDescription": "JD"}}},
		]
		client._browser_session = browser
		client._request = Mock(side_effect=AssertionError("No independent httpx request"))
		platform = BossPlatform(client)
		platform.search_jobs("AI", city="北京")
		result = platform.job_card_browser("synthetic-security", "synthetic-lid")
		assert result["zpData"]["jobCard"]["postDescription"] == "JD"
		assert browser.request.call_count == 2
		assert browser.request.call_args_list[0].args == ("GET", endpoints.SEARCH_URL)
		assert browser.request.call_args_list[1].args == ("GET", endpoints.JOB_CARD_URL)
		assert browser.request.call_args_list[1].kwargs["params"] == {
			"securityId": "synthetic-security",
			"lid": "synthetic-lid",
		}
		client._request.assert_not_called()


def test_browser_card_risk_never_falls_back_to_httpx():
	with BossClient(FakeAuthManager()) as client:
		browser = Mock()
		browser.request.return_value = {"code": 37, "message": "您的环境存在异常."}
		client._browser_session = browser
		client._request = Mock()
		with pytest.raises(EnvironmentRiskError):
			client.job_card_browser("synthetic-security", "synthetic-lid")
		browser.request.assert_called_once()
		client._request.assert_not_called()


def test_legacy_card_httpx_first_behavior_is_preserved():
	with BossClient(FakeAuthManager()) as client:
		client.job_card_httpx = Mock(return_value={"code": 0})
		client._browser_request = Mock()
		assert client.job_card("synthetic-security", "synthetic-lid") == {"code": 0}
		client.job_card_httpx.assert_called_once_with("synthetic-security", "synthetic-lid")
		client._browser_request.assert_not_called()


def test_page_changed_before_search_stops_locally_without_retry_or_fallback():
	with BossClient(FakeAuthManager()) as client:
		browser = Mock()
		browser.request.return_value = {"code": "BRIDGE_SEARCH_PAGE_REQUIRED", "_bridge_meta": {"source": "page_context", "request_sent": False, "page_path": "/web/geek/chat"}}
		client._browser_session = browser
		client._request = Mock()
		with pytest.raises(BrowserSearchPageRequired):
			client.search_jobs("AI", city="北京")
		browser.request.assert_called_once()
		client._request.assert_not_called()
