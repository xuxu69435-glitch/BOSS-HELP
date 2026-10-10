"""Offline reproduction of code 37 environment warnings, without live access."""

import json
from concurrent.futures import Future
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from boss_agent_cli.api import endpoints
from boss_agent_cli.api.client import BossClient
from boss_agent_cli.api.zhipin_errors import EnvironmentRiskError, classify_code_37
from boss_agent_cli.delivery_store import DeliveryStore
from boss_agent_cli.main import cli
from boss_agent_cli.platforms.zhipin import BossPlatform
from boss_agent_cli.wizard.actions import execute_candidate_detail
from boss_agent_cli.wizard.models import WorkflowPlan
from boss_agent_cli.wizard.runner import WorkflowRunner
from boss_agent_cli.wizard.store import WorkflowStore
from boss_agent_cli.search_filters import SearchFilterCriteria, _check_details_parallel
from test_api_client_retry import FakeAuthManager, FakeHttpxClient, FakeResponse
from test_delivery import environment as environment, invoke


@pytest.mark.parametrize(
	"response,expected",
	[
		({"code": 37, "message": "您的环境存在异常."}, "environment_risk"),
		({"code": 37, "message": "环境异常，stoken验证失败"}, "environment_risk"),
		({"code": 37, "msg": "访问环境异常"}, "environment_risk"),
		({"code": 37, "zpData": "环境存在异常"}, "environment_risk"),
		({"code": 37}, "environment_risk"),
		({"code": 37, "message": "unknown"}, "environment_risk"),
		({"code": 37, "message": "stoken expired"}, "token_expired"),
		({"code": 37, "message": "登录态过期"}, "token_expired"),
	],
)
def test_code37_is_classified_by_context(response, expected):
	assert classify_code_37(response) == expected
	code, _ = BossPlatform(Mock()).parse_error(response)
	assert code == ("ENVIRONMENT_RISK" if expected == "environment_risk" else "TOKEN_REFRESH_FAILED")


@pytest.mark.parametrize("status,text", [(200, ""), (403, "安全验证")])
def test_http_environment_warning_never_refreshes_retries_or_sleeps(monkeypatch, status, text):
	auth = FakeAuthManager()
	sleep = Mock()
	monkeypatch.setattr("boss_agent_cli.api._base_client.time.sleep", sleep)
	with BossClient(auth) as client:
		http = FakeHttpxClient(
			[
				FakeResponse(status_code=status, text=text, payload={"code": 37, "message": "您的环境存在异常."}),
				FakeResponse(payload={"code": 0}),
			]
		)
		client._client = http
		client._throttle.wait = lambda: None
		client._throttle.mark = lambda: None
		with pytest.raises(EnvironmentRiskError):
			client.job_detail("synthetic-job")
		assert len(http.calls) == 1
		assert len(http.responses) == 1
		assert auth.refresh_calls == []
		sleep.assert_not_called()


def test_browser_environment_warning_is_typed_and_not_retried():
	with BossClient(FakeAuthManager()) as client:
		browser = Mock()
		browser.request.return_value = {"code": 37, "message": "您的环境存在异常."}
		client._get_browser = Mock(return_value=browser)
		with pytest.raises(EnvironmentRiskError):
			client._browser_request("GET", endpoints.JOB_CARD_URL)
		browser.request.assert_called_once()


def test_normal_json_job_text_does_not_trigger_token_refresh(monkeypatch):
	auth = FakeAuthManager()
	sleep = Mock()
	monkeypatch.setattr("boss_agent_cli.api._base_client.time.sleep", sleep)
	data = {"code": 0, "zpData": {"postDescription": "负责安全验证、测试与研发"}}
	with BossClient(auth) as client:
		http = FakeHttpxClient([FakeResponse(status_code=200, text="负责安全验证、测试与研发", payload=data)])
		client._client = http
		client._throttle.wait = lambda: None
		client._throttle.mark = lambda: None
		assert client.job_detail("fixture-job") == data
		assert len(http.calls) == 1 and auth.refresh_calls == []
		sleep.assert_not_called()


def test_bridge_login_guard_is_not_an_environment_code37():
	from boss_agent_cli.api.client import AuthError
	with BossClient(FakeAuthManager()) as client:
		browser = Mock()
		browser.request.return_value = {"code": "BRIDGE_LOGIN_REQUIRED", "_bridge_meta": {"source": "page_login", "request_sent": False}}
		client._get_browser = Mock(return_value=browser)
		with pytest.raises(AuthError) as error:
			client._browser_request("GET", endpoints.USER_INFO_URL)
		assert error.value.diagnostic == {"source": "page_login", "request_sent": False}
		browser.request.assert_called_once()


def test_job_card_does_not_switch_channel_after_environment_risk():
	with BossClient(FakeAuthManager()) as client:
		client.job_card_httpx = Mock(side_effect=EnvironmentRiskError("environment warning"))
		client._browser_request = Mock()
		with pytest.raises(EnvironmentRiskError):
			client.job_card("synthetic-security", "synthetic-lid")
		client._browser_request.assert_not_called()


def test_detail_command_returns_nonrecoverable_error_without_browser_fallback(tmp_path, monkeypatch):
	client = Mock()
	client.job_detail.return_value = {"code": 37, "message": "您的环境存在异常."}
	platform = BossPlatform(client)
	monkeypatch.setattr("boss_agent_cli.commands.detail.get_platform_instance", lambda *args: platform)
	result = CliRunner().invoke(
		cli, ["--data-dir", str(tmp_path), "--json", "detail", "synthetic-security", "--job-id", "synthetic-job"]
	)
	assert result.exit_code == 1, result.output
	error = json.loads(result.stdout)["error"]
	assert error["code"] == "ENVIRONMENT_RISK"
	assert error["recoverable"] is False
	assert "boss login" not in error["recovery_action"]
	client.job_card.assert_not_called()


def test_shared_detail_action_does_not_switch_channel():
	client = Mock()
	client.job_detail.return_value = {"code": 37, "message": "您的环境存在异常."}
	with pytest.raises(EnvironmentRiskError):
		execute_candidate_detail(BossPlatform(client), security_id="synthetic-security", job_id="synthetic-job")
	client.job_card.assert_not_called()


def test_batch_saves_progress_and_stops_at_first_environment_warning(tmp_path, environment, monkeypatch):
	platform = environment[0]
	detail = Mock(return_value={"code": 37, "message": "您的环境存在异常."})
	monkeypatch.setattr(platform, "job_detail", detail)
	result = invoke(tmp_path, "prepare", "AI", "--no-ai")
	assert result.exit_code == 1
	error = json.loads(result.stdout)["error"]
	assert error["code"] == "ENVIRONMENT_RISK"
	assert error["recoverable"] is False
	detail.assert_called_once()
	assert not platform.sent
	with DeliveryStore(tmp_path) as store:
		batch = store.get(store.list_batches()[0]["id"])
		assert batch["stop_reason"] == "environment_risk"
		assert batch["stats"]["last_error"]["code"] == "ENVIRONMENT_RISK"
		assert batch["items"] == []


def test_welfare_worker_does_not_swallow_environment_warning(monkeypatch):
	failed, pending = Future(), Future()
	failed.set_exception(EnvironmentRiskError("environment warning"))
	pool = Mock()
	pool.submit.side_effect = [failed, pending]
	manager = Mock()
	manager.__enter__ = Mock(return_value=pool)
	manager.__exit__ = Mock(return_value=False)
	monkeypatch.setattr("boss_agent_cli.search_filters.ThreadPoolExecutor", lambda **kwargs: manager)
	cache = Mock()
	cache.get_job_desc.return_value = None
	with pytest.raises(EnvironmentRiskError):
		_check_details_parallel(
			Mock(), cache, Mock(), [], SearchFilterCriteria("AI"), [{"encryptJobId": "a"}, {"encryptJobId": "b"}], []
		)
	assert pending.cancelled()


def test_workflow_environment_warning_is_terminal(tmp_path):
	action = Mock(side_effect=EnvironmentRiskError("environment warning"))
	next_action = Mock()
	plan = WorkflowPlan(
		role="candidate",
		platform="zhipin",
		goal="job_search",
		inputs={"query": "AI"},
		requested_steps=("first", "second"),
		mode="headless",
	)
	with WorkflowStore(tmp_path) as store:
		runner = WorkflowRunner(store, {"first": action, "second": next_action})
		result = runner.run(plan, None, max_retries=3)
	assert result["error"]["code"] == "ENVIRONMENT_RISK"
	assert result["error"]["recoverable"] is False
	action.assert_called_once()
	next_action.assert_not_called()


def test_legacy_batch_greet_does_not_retry_environment_warning(tmp_path, environment, monkeypatch):
	platform = environment[0]
	platform.name = "zhipin"
	monkeypatch.setattr("boss_agent_cli.commands.greet.get_platform_instance", lambda *args: platform)
	monkeypatch.setattr(
		platform,
		"search_jobs",
		lambda *args, **kwargs: {
			"code": 0,
			"zpData": {
				"jobList": [{"encryptJobId": "a", "securityId": "s-a"}, {"encryptJobId": "b", "securityId": "s-b"}]
			},
		},
	)
	send = Mock(return_value={"code": 37, "message": "您的环境存在异常."})
	monkeypatch.setattr(platform, "greet", send)
	result = CliRunner().invoke(cli, ["--data-dir", str(tmp_path), "--json", "batch-greet", "AI"])
	assert result.exit_code == 1
	assert json.loads(result.stdout)["error"]["code"] == "ENVIRONMENT_RISK"
	send.assert_called_once()
