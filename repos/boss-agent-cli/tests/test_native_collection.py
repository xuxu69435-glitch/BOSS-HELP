from unittest.mock import Mock

import httpx
import pytest

from boss_agent_cli.api import endpoints
from boss_agent_cli.api.browser_client import BrowserSession
from boss_agent_cli.api.browser_source import BrowserNativeCollectionError
from boss_agent_cli.api.client import BossClient
from boss_agent_cli.api.zhipin_errors import EnvironmentRiskError
from boss_agent_cli.bridge.client import BridgeClient, BridgeRequestError
from boss_agent_cli.delivery import read_job_detail
from boss_agent_cli.platforms.zhipin import BossPlatform
from test_api_client_retry import FakeAuthManager


@pytest.mark.parametrize("url,kind", [(endpoints.SEARCH_URL, "search"), (endpoints.JOB_CARD_URL, "detail")])
def test_existing_client_uses_native_page_without_extra_fetch(url, kind):
	session = BrowserSession({}, "", existing_browser_only=True)
	session._started = True
	session._is_bridge = True
	session._bridge_client = Mock()
	session._bridge_client.collect_native.return_value = {"code": 0, "zpData": {}}
	session._throttle = Mock()
	assert session.request("GET", url, params={"page": 1})["code"] == 0
	session._bridge_client.collect_native.assert_called_once_with(kind, {"page": 1})
	session._bridge_client.fetch_json.assert_not_called()
	session.close()


def test_native_transport_loss_does_not_resubmit_or_fallback(monkeypatch):
	post = Mock(side_effect=httpx.ReadTimeout("lost result"))
	monkeypatch.setattr("boss_agent_cli.bridge.client.httpx.post", post)
	bridge = BridgeClient(max_retries=4)
	with pytest.raises(BridgeRequestError):
		bridge.collect_native("search", {"query": "AI", "city": "101010100", "page": 1})
	assert post.call_count == 1


def test_native_remote_error_retains_actual_source_and_stops():
	with BossClient(FakeAuthManager(), browser_source="existing-browser") as client:
		browser = Mock()
		client._browser_session = browser
		browser.request.return_value = {"code": 37, "message": "您的环境存在异常.", "_bridge_meta": {"source": "api_response", "transport": "native-page", "endpoint": "/wapi/zpgeek/job/detail.json", "http_status": 200, "request_sent": True}}
		with pytest.raises(EnvironmentRiskError) as exc:
			client.job_card_browser("fixture-security")
		assert exc.value.diagnostic["endpoint"] == "/wapi/zpgeek/job/detail.json"
		assert exc.value.diagnostic["transport"] == "native-page"
		browser.request.assert_called_once()


def test_native_local_timeout_does_not_impersonate_platform_rejection():
	with BossClient(FakeAuthManager(), browser_source="existing-browser") as client:
		client._browser_session = Mock()
		client._browser_session.request.return_value = {"code": "BRIDGE_NATIVE_READ_TIMEOUT", "message": "fixture timeout", "_bridge_meta": {"source": "page_context", "transport": "native-page", "request_sent": None}}
		with pytest.raises(BrowserNativeCollectionError) as exc:
			client.search_jobs("AI", city="北京")
		assert exc.value.diagnostic["source"] == "page_context"
		assert "response_code" not in exc.value.diagnostic


def test_verification_after_native_read_does_not_claim_no_request_was_sent():
	with BossClient(FakeAuthManager(), browser_source="existing-browser") as client:
		client._browser_session = Mock()
		client._browser_session.request.return_value = {"code": "BRIDGE_PAGE_VERIFICATION", "_bridge_meta": {"source": "page_verification", "transport": "native-page", "request_sent": True, "verification": {"detected": True, "evidence": [{"kind": "visible_verification_prompt", "cue": "请完成安全验证"}]}}}
		with pytest.raises(EnvironmentRiskError) as exc:
			client.search_jobs("AI", city="北京")
		assert exc.value.diagnostic["request_sent"] is True
		assert exc.value.diagnostic["source"] == "page_verification"
		assert "读取已发起" in str(exc.value)


def test_saved_job_context_reaches_native_reader_and_jd_id_is_checked():
	with BossClient(FakeAuthManager(), browser_source="existing-browser") as client:
		browser = Mock()
		client._browser_session = browser
		browser.request.return_value = {"code": 0, "zpData": {"jobCard": {"encryptJobId": "fixture-job", "postDescription": "2027届，本科及以上，开发Agent应用。"}}, "_bridge_meta": {"transport": "native-page"}}
		job = {"job_id": "fixture-job", "security_id": "fixture-security", "title": "AI", "company": "Fixture", "city": "北京"}
		result = read_job_detail(BossPlatform(client), job)
		assert result["description"].startswith("2027")
		assert result["detail_source"] == "native_page_job_detail"
		assert browser.request.call_args.kwargs["params"]["nativeJob"]["encryptJobId"] == "fixture-job"
