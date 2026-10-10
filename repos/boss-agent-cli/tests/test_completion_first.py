"""Client completion targets are independent of wall time; safety still takes precedence."""

import copy
from unittest.mock import Mock

import pytest

from boss_agent_cli.api.zhipin_errors import EnvironmentRiskError
from boss_agent_cli.cache.store import CacheStore
from boss_agent_cli.delivery import COLLECTION_POLICY, DeliveryError, collect_batch
from boss_agent_cli.delivery_store import DeliveryStore
from client_fixture import fixture_client
from test_collection_policy import clock  # noqa: F401
from test_delivery_quotas_history import candidate, params, platform_for


def options():
	return {
		**params({"recruitment_quotas": {"campus": 5, "internship": 5}}),
		"max_seconds": 30, "max_pages": 1, "max_details": 1,
		"completion_first": True, "continue_after_cooldown": True,
		"collection_policy": {**COLLECTION_POLICY, "request_limit": 4},
	}


def test_one_run_completes_five_plus_five_after_multiple_budget_windows(tmp_path, clock):  # noqa: F811
	platform = platform_for([candidate(i) for i in range(1, 6)], [candidate(i, "internship") for i in range(6, 11)])
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		bid = store.create(options())
		start = clock.now
		collect_batch(store, bid, platform, cache, Mock())
		batch = store.get(bid)
		assert clock.now - start > 1200
		assert len(batch["items"]) == 10 and len(store.list_batches()) == 1
		assert batch["stop_reason"] == "target_reached"
		assert batch["stats"]["recruitment_counts"] == {"campus": 5, "internship": 5}
		assert batch["stats"]["network_requests"] == 12
		assert batch["stats"]["cooldown_waits"] >= 2
		assert platform.sent == []


def test_completion_mode_still_stops_on_real_verification_without_retry(tmp_path, clock):  # noqa: F811
	platform = platform_for([candidate(1)], [])
	platform.job_card_browser = Mock(return_value={"code": 37, "message": "环境存在异常"})
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		bid = store.create(options())
		with pytest.raises(EnvironmentRiskError):
			collect_batch(store, bid, platform, cache, Mock())
		assert store.get(bid)["stop_reason"] == "environment_risk"
		platform.job_card_browser.assert_called_once()


def test_user_can_cancel_during_completion_mode_budget_wait(tmp_path, clock):  # noqa: F811
	platform = platform_for([candidate(i) for i in range(1, 6)], [])
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		bid = store.create(options())
		with pytest.raises(DeliveryError, match="停止等待"):
			collect_batch(store, bid, platform, cache, Mock(), should_stop=lambda: clock.now >= 1100)
		assert len(store.get(bid)["items"]) == 3
		assert store.get(bid)["stats"]["network_requests"] == 4
		assert platform.sent == []


def test_repeating_last_page_does_not_loop_forever_or_fake_completion(tmp_path, clock):  # noqa: F811
	platform = platform_for([], [])
	platform.search_jobs.side_effect = None
	platform.search_jobs.return_value = {"code": 0, "zpData": {"jobList": [{**candidate(1), "salaryDesc": "14-30K"}], "hasMore": True}}
	p = options()
	p["searches"] = [{"query": "campus", "city": "北京", "kind": "campus"}]
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		bid = store.create(p)
		collect_batch(store, bid, platform, cache, Mock())
		batch = store.get(bid)
		assert batch["items"] == [] and batch["stop_reason"] == "results_exhausted"
		assert batch["stats"]["repeated_pages"] == 1
		assert platform.search_jobs.call_count == 2 and platform.details == []


def test_list_without_job_identifiers_is_not_paginated_forever(tmp_path, clock):  # noqa: F811
	platform = platform_for([], [])
	platform.search_jobs.side_effect = None
	platform.search_jobs.return_value = {"code": 0, "zpData": {"jobList": [{"jobName": "Malformed job"}], "hasMore": True}}
	p = options()
	p["searches"] = [{"query": "campus", "city": "北京", "kind": "campus"}]
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		bid = store.create(p)
		collect_batch(store, bid, platform, cache, Mock())
		batch = store.get(bid)
		assert batch["items"] == [] and batch["stop_reason"] == "results_incomplete"
		assert platform.search_jobs.call_count == 1


@pytest.mark.parametrize(
	"campus,intern,pending,expected",
	[(5, 5, 10, "complete"), (5, 4, 9, "partial"), (6, 4, 10, "partial"), (5, 5, 9, "partial")],
)
def test_controller_requires_count_quotas_and_ready_previews(tmp_path, monkeypatch, campus, intern, pending, expected):
	controller, old = fixture_client(tmp_path)
	try:
		with DeliveryStore(tmp_path) as store:
			template = store.get(old)["items"][0]["payload"]
		controller._set_browser_state("ready", "Fixture only")
		controller._wait_collection_start = Mock()
		controller._check_chrome = Mock()
		controller._service_and_resume = Mock(return_value=(Mock(), "Fixture resume"))
		client = Mock()
		client.__enter__ = Mock(return_value=client)
		client.__exit__ = Mock(return_value=False)
		monkeypatch.setattr("boss_agent_cli.client_controller.BossClient", Mock(return_value=client))
		monkeypatch.setattr("boss_agent_cli.client_controller.seed_batch_from_local", Mock(return_value=0))
		def collect(store, bid, *args, **kwargs):
			assert "deadline" not in kwargs
			assert store.get(bid)["params"]["completion_first"] is True
			for n in range(1, campus + intern + 1):
				p = copy.deepcopy(template)
				p.update(job_id=f"fresh-{n}", recruitment_kind="campus" if n <= campus else "internship")
				store.add(bid, n, p)
			store.progress(bid, {}, "results_exhausted" if campus + intern < 10 else "target_reached")
		def drafts(store, bid, *args, **kwargs):
			assert "deadline" not in kwargs
			for item in store.get(bid)["items"][:pending]:
				store.save_draft(bid, item["number"], item["payload"])
		monkeypatch.setattr("boss_agent_cli.client_controller.collect_batch", collect)
		monkeypatch.setattr("boss_agent_cli.client_controller.draft_batch", drafts)
		controller._run("prepare", {})
		assert controller.operation["status"] == expected
		assert "deadline_at" not in controller.operation
		if expected == "partial":
			assert "未完成" in controller.operation["shortfall_message"]
	finally:
		controller.close()


def test_client_settings_do_not_require_obsolete_page_or_time_limits(tmp_path):
	controller, _ = fixture_client(tmp_path)
	try:
		controller.save_settings({"campus_count": 5, "internship_count": 5, "min_salary": 15, "min_daily_salary": 300, "cities": ["北京"], "roles": ["agent_development"], "history_dedupe": "job"})
		assert controller.settings()["completion_first"] is True
		assert controller.settings()["count"] == 10
	finally:
		controller.close()
