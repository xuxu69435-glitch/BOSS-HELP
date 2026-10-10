"""Pause evidence distinguishes local guards from remote responses without replaying requests."""

import json
import time
from unittest.mock import Mock

import pytest

from boss_agent_cli.api.zhipin_errors import EnvironmentRiskError, raise_if_environment_risk
from boss_agent_cli.cache.store import CacheStore
from boss_agent_cli.client_controller import ClientController
from boss_agent_cli.delivery import collect_batch
from boss_agent_cli.delivery_store import DeliveryStore
from client_fixture import fixture_client
from test_delivery_quotas_history import candidate, params, platform_for


def api_error():
	return {
		"code": 37, "message": '环境存在异常; token=SECRET; "securityId":"PRIVATE"; https://example.test/?credential=HIDDEN',
		"zpData": {"cookie": "DO_NOT_LOG"},
		"_bridge_meta": {"source": "api_response", "request_sent": True, "http_status": 200, "endpoint": "/wapi/test?securityId=DO_NOT_KEEP", "verification": {"detected": False, "evidence": []}},
	}


def page_info():
	return {"detected": True, "evidence": [{"kind": "visible_verification_prompt", "cue": "请完成安全验证"}]}


def test_actual_response_keeps_sanitized_evidence_without_claiming_visible_captcha():
	with pytest.raises(EnvironmentRiskError) as caught:
		raise_if_environment_risk(api_error())
	d = caught.value.diagnostic
	assert d["source"] == "api_response" and d["request_sent"] is True
	assert d["response_code"] == 37 and d["http_status"] == 200
	assert d["page_verification"] is False
	assert d["endpoint"] == "/wapi/test"
	serialized = json.dumps(d)
	assert all(value not in serialized for value in ("SECRET", "PRIVATE", "HIDDEN", "DO_NOT_LOG", "DO_NOT_KEEP"))
	assert "不能仅凭错误码判断账号状态" in str(caught.value)


def test_bridge_records_known_page_context_without_query_or_credentials():
	response = api_error()
	response["_bridge_meta"]["page_path"] = "/web/geek/jobs?token=DO_NOT_LOG"
	assert EnvironmentRiskError.from_response(response).diagnostic["page_path"] == "/web/geek/jobs"
	response["_bridge_meta"]["page_path"] = "/arbitrary-private-path"
	assert "page_path" not in EnvironmentRiskError.from_response(response).diagnostic


def test_local_guard_does_not_invent_server_code_37():
	response = {"code": "BRIDGE_PAGE_VERIFICATION", "_bridge_meta": {"source": "page_verification", "request_sent": False, "verification": page_info()}}
	with pytest.raises(EnvironmentRiskError) as caught:
		raise_if_environment_risk(response)
	d = caught.value.diagnostic
	assert d["source"] == "page_verification" and d["request_sent"] is False
	assert d["page_verification"] is True
	assert d.get("response_code") is None
	assert d["page_evidence"][0]["cue"] == "请完成安全验证"


def test_old_boolean_detector_is_only_a_local_suspicion():
	error = EnvironmentRiskError.from_page({"verificationVisible": True})
	assert error.diagnostic["source"] == "local_guard"
	assert error.diagnostic["page_verification"] is None
	assert error.diagnostic["page_evidence"] == []
	assert "没有具体组件证据" in str(error)


def test_legacy_response_without_provenance_remains_unknown():
	with pytest.raises(EnvironmentRiskError) as caught:
		raise_if_environment_risk({"code": 37, "message": "环境存在异常"})
	assert caught.value.diagnostic["source"] == "unknown"
	assert caught.value.diagnostic["request_sent"] is None


def test_http_response_cannot_impersonate_a_local_page_guard():
	response = api_error()
	response["_bridge_meta"] = {"source": "page_verification", "request_sent": False, "verification": page_info()}
	error = EnvironmentRiskError.from_response(response, endpoint="https://www.zhipin.com/wapi/test?token=PRIVATE", transport_source="api_response", http_status=403)
	assert error.diagnostic["source"] == "api_response"
	assert error.diagnostic["request_sent"] is True
	assert error.diagnostic["page_verification"] is None
	assert error.diagnostic["endpoint"] == "/wapi/test"


def test_api_cannot_impersonate_reserved_local_guard_codes():
	response = {"code": "BRIDGE_PAGE_VERIFICATION", "_bridge_meta": {"source": "api_response", "request_sent": True}}
	raise_if_environment_risk(response)


def test_collection_persists_provenance_with_partial_progress(tmp_path):
	platform = platform_for([candidate(1)], [])
	platform.job_card_browser = Mock(return_value=api_error())
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		bid = store.create(params())
		with pytest.raises(EnvironmentRiskError):
			collect_batch(store, bid, platform, cache, Mock())
		b = store.get(bid)
		assert b["stats"]["last_error"]["diagnostic"]["source"] == "api_response"
		assert b["stop_reason"] == "environment_risk"
		platform.job_card_browser.assert_called_once()
		assert platform.sent == []


def test_controller_persists_evidence_and_preserves_pause_across_restart(tmp_path):
	controller, _ = fixture_client(tmp_path)
	try:
		controller._network_operation = Mock(side_effect=EnvironmentRiskError.from_response(api_error()))
		controller._run("prepare", {})
		browser = controller.state()["browser"]
		assert browser["status"] == "blocked" and browser["pause_diagnostic"]["source"] == "api_response"
		assert "未检测到可见验证组件" in browser["message"]
		assert "请在官方页面处理校验" not in browser["message"]
		assert controller.operation["error"]["diagnostic"]["response_code"] == 37
		other = ClientController(controller.data_dir, controller.settings_path)
		try:
			assert other.state()["browser"]["pause_diagnostic"] == browser["pause_diagnostic"]
			assert other.state()["browser"]["blocked_until"] == browser["blocked_until"]
			with pytest.raises(ValueError, match="不是BOSS官方处罚"):
				other.start("connect", {})
		finally:
			other.close()
	finally:
		controller.close()


def test_legacy_pause_is_relabelled_without_clearing_or_extending_it(tmp_path):
	controller, _ = fixture_client(tmp_path)
	try:
		until = time.time() + 900
		controller._set_browser_state("blocked", "请在官方页面处理校验", blocked_until=until)
		other = ClientController(controller.data_dir, controller.settings_path)
		try:
			browser = other.state()["browser"]
			assert browser["status"] == "blocked" and browser["blocked_until"] == until
			assert browser["pause_diagnostic"]["source"] == "unknown"
			assert "请在官方页面处理校验" not in browser["message"]
			assert "历史记录未区分" in browser["message"]
			with pytest.raises(EnvironmentRiskError) as caught:
				other._assert_boss_ready("prepare")
			other._pause_for_risk(caught.value)
			assert other.state()["browser"]["blocked_until"] == until
		finally:
			other.close()
	finally:
		controller.close()
