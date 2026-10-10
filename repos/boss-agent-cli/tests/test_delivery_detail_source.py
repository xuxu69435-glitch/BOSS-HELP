"""The batch must preserve list context and use the existing card-detail contract."""

import json
from unittest.mock import Mock

import pytest

from boss_agent_cli.api.zhipin_errors import EnvironmentRiskError
from boss_agent_cli.delivery import DeliveryError, read_job_detail
from boss_agent_cli.delivery_store import DeliveryStore
from test_delivery import environment as environment, invoke, job


def test_collection_forwards_security_and_lid_without_using_job_id_endpoint(tmp_path, environment, monkeypatch):
	platform = environment[0]
	monkeypatch.setattr(
		platform,
		"search_jobs",
		lambda *args, **kwargs: {
			"code": 0,
			"zpData": {"jobList": [{**job(1), "lid": "synthetic-list-context"}], "hasMore": False},
		},
	)
	card = Mock(
		return_value={
			"code": 0,
			"zpData": {
				"jobCard": {"encryptJobId": "job-1", "postDescription": "Complete original JD", "degreeName": "本科"}
			},
		}
	)
	monkeypatch.setattr(platform, "job_card_browser", card)
	legacy_detail = Mock(side_effect=AssertionError("Batch must not use the job-id-only endpoint"))
	monkeypatch.setattr(platform, "job_detail", legacy_detail)
	result = invoke(tmp_path, "prepare", "AI", "--no-ai", "--count", "1")
	assert result.exit_code == 0, result.output
	card.assert_called_once_with("sec-1", "synthetic-list-context")
	legacy_detail.assert_not_called()
	data = json.loads(result.stdout)["data"]
	assert data["items"][0]["description"] == "Complete original JD"
	assert data["items"][0]["detail_source"] == "search_browser_job_card"
	assert "lid" not in data["items"][0]
	assert "security_id" not in data["items"][0]
	with DeliveryStore(tmp_path) as store:
		assert store.get(data["batch_id"])["items"][0]["payload"]["lid"] == "synthetic-list-context"


def test_card_identity_mismatch_stops_without_fallback(environment):
	platform = environment[0]
	platform.job_card_browser = Mock(
		return_value={"code": 0, "zpData": {"jobCard": {"encryptJobId": "different", "postDescription": "Other job"}}}
	)
	platform.job_detail = Mock()
	with pytest.raises(DeliveryError, match="标识与列表不一致"):
		read_job_detail(platform, {"security_id": "sec-1", "job_id": "job-1", "lid": "context"})
	platform.job_detail.assert_not_called()


def test_card_environment_warning_stops_without_retry_or_fallback(environment):
	platform = environment[0]
	platform.job_card_browser = Mock(return_value={"code": 37, "message": "您的环境存在异常."})
	platform.job_detail = Mock()
	with pytest.raises(EnvironmentRiskError):
		read_job_detail(platform, {"security_id": "sec-1", "job_id": "job-1"})
	platform.job_card_browser.assert_called_once()
	platform.job_detail.assert_not_called()


def test_missing_original_jd_is_not_replaced_by_a_summary(environment):
	platform = environment[0]
	platform.job_card_browser = Mock(
		return_value={"code": 0, "zpData": {"jobCard": {"encryptJobId": "job-1", "summary": "Only a summary"}}}
	)
	assert read_job_detail(platform, {"security_id": "sec-1", "job_id": "job-1"})["description"] == ""
