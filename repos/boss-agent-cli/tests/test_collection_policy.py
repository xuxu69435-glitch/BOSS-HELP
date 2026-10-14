"""Offline rate, cache and cooldown tests. Never contacts BOSS or DeepSeek."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from boss_agent_cli.api.zhipin_errors import EnvironmentRiskError
from boss_agent_cli.cache.store import CacheStore
from boss_agent_cli.delivery import COLLECTION_POLICY, DeliveryError, collect_batch
from boss_agent_cli.delivery_store import DeliveryStore
from client_fixture import fixture_client
from test_delivery_quotas_history import candidate, params, platform_for


@pytest.fixture
def clock(monkeypatch):
	clock = SimpleNamespace(now=1000.0, sleeps=[])
	clock.time = lambda: clock.now
	clock.monotonic = lambda: clock.now

	def sleep(seconds):
		clock.sleeps.append(seconds)
		clock.now += seconds

	clock.sleep = sleep
	monkeypatch.setattr("boss_agent_cli.delivery.time", clock)
	monkeypatch.setattr("boss_agent_cli.cache.store.time", clock)
	return clock


def settings(**changes):
	result = params(searches=[{"query": "campus", "city": "北京", "kind": "campus"}])
	result.update(max_seconds=900, collection_policy=dict(COLLECTION_POLICY), **changes)
	return result


def collect(store, cache, platform, options, should_stop=None):
	bid = store.create(options)
	collect_batch(store, bid, platform, cache, Mock(), should_stop=should_stop)
	return store.get(bid)


def test_rejected_jd_is_cached_and_new_rules_are_reapplied(tmp_path, clock):
	platform = platform_for([candidate(1)], [])
	options = settings()
	options["rules"]["require"] = ["Unmatched requirement"]
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		first = collect(store, cache, platform, options)
		assert first["items"] == [] and first["stats"]["network_requests"] == 2
		options["rules"]["require"] = []
		second = collect(store, cache, platform, options)
		assert len(second["items"]) == 1
		assert second["stats"]["network_requests"] == 0
		assert second["stats"]["search_cache_hits"] == 1
		assert second["stats"]["detail_cache_hits"] == 1
		assert platform.search_jobs.call_count == 1 and platform.details == ["job-1"]
		assert clock.now >= 1012


def test_list_and_full_detail_have_separate_freshness_windows(tmp_path, clock):
	platform = platform_for([candidate(1)], [])
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		collect(store, cache, platform, settings())
		clock.now += 901
		second = collect(store, cache, platform, settings())
		assert second["stats"]["network_requests"] == 1
		assert second["stats"]["detail_cache_hits"] == 1
		clock.now += 21601
		third = collect(store, cache, platform, settings())
		assert third["stats"]["network_requests"] == 2
		assert third["stats"]["detail_cache_hits"] == 0


def test_slow_read_still_leaves_twelve_seconds_after_completion(tmp_path, clock):
	platform = platform_for([candidate(1)], [])
	search = platform.search_jobs.side_effect

	def slow_search(*args, **kwargs):
		clock.now += 8
		return search(*args, **kwargs)

	platform.search_jobs.side_effect = slow_search
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		collect(store, cache, platform, settings())
	assert clock.now >= 1020


def test_budget_keeps_partial_batch_and_does_not_reset_on_next_batch(tmp_path, clock):
	platform = platform_for([candidate(1), candidate(2), candidate(3)], [])
	options = settings()
	options["collection_policy"]["request_limit"] = 2
	options["rules"]["history_dedupe"] = "job"
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		first = collect(store, cache, platform, options)
		assert first["stop_reason"] == "request_limit"
		assert len(first["items"]) == 1 and first["items"][0]["status"] == "draft"
		assert first["stats"]["network_requests"] == 2
		second = collect(store, cache, platform, options)
		assert second["stop_reason"] == "request_limit"
		assert second["stats"]["network_requests"] == 0
		assert platform.details == ["job-1"]
		assert store.get(first["id"])["items"] == first["items"]


def test_budget_is_durable_across_connections_and_expires_without_auto_requests(tmp_path, clock):
	path = tmp_path / "cache.db"
	with CacheStore(path) as one, CacheStore(path) as two:
		assert one.reserve_discovery_request(limit=1, window_seconds=600, interval=12)["allowed"]
		assert two.reserve_discovery_request(limit=1, window_seconds=600, interval=12)["reason"] == "request_limit"
	with CacheStore(path) as restored:
		assert restored.discovery_budget(limit=1, window_seconds=600)["count"] == 1
		clock.now += 601
		assert restored.discovery_budget(limit=1, window_seconds=600)["count"] == 0
		assert restored.reserve_discovery_request(limit=1, window_seconds=600, interval=12)["allowed"]


def test_cancel_during_spacing_does_not_request_detail(tmp_path, clock):
	platform = platform_for([candidate(1)], [])
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		with pytest.raises(DeliveryError, match="已停止"):
			collect(store, cache, platform, settings(), should_stop=lambda: bool(clock.sleeps))
		assert platform.search_jobs.call_count == 1
		assert platform.details == []
		assert cache.discovery_budget(limit=20, window_seconds=600)["count"] == 1


def test_environment_warning_is_never_cached_or_retried(tmp_path, clock):
	platform = platform_for([], [])
	platform.search_jobs.side_effect = None
	platform.search_jobs.return_value = {"code": 37, "message": "环境存在异常"}
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		with pytest.raises(EnvironmentRiskError):
			collect(store, cache, platform, settings())
		assert platform.search_jobs.call_count == 1
		assert cache._conn.execute("SELECT count(*) FROM search_cache").fetchone()[0] == 0
		batch = store.get(store.list_batches()[0]["id"])
		assert batch["stats"]["network_requests"] == 1 and batch["stop_reason"] == "environment_risk"


def test_incomplete_detail_is_not_cached(tmp_path, clock):
	platform = platform_for([candidate(1)], [])
	platform.job_card_browser = Mock(return_value={"code": 0, "zpData": {"jobCard": {}}})
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		collect(store, cache, platform, settings())
		second = collect(store, cache, platform, settings())
		assert second["items"] == []
		assert platform.job_card_browser.call_count == 2
		assert second["stats"]["search_cache_hits"] == 1 and second["stats"]["detail_cache_hits"] == 0


def test_same_company_history_selection_is_kept_from_cached_rows(tmp_path, clock):
	platform = platform_for([candidate(1)], [])
	options = settings()
	options["rules"]["history_dedupe"] = "company"
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		collect(store, cache, platform, options)
		second = collect(store, cache, platform, options)
		assert [item["job_id"] for item in second["items"]] == ["job-1"]
		assert second["stats"]["network_requests"] == 0
		assert second["stats"]["detail_cache_hits"] == 1
		assert second["stats"]["excluded"] == []
		assert platform.details == ["job-1"]


def test_controller_accepts_one_waiting_task_without_starting_extra_workers(tmp_path):
	controller, _ = fixture_client(tmp_path)
	try:
		with CacheStore(tmp_path / "cache/boss_agent.db") as cache:
			for _ in range(20):
				assert cache.reserve_discovery_request(limit=20, window_seconds=600, interval=0)["allowed"]
		controller._executor.submit = Mock()
		controller.start("prepare", {})
		assert controller.operation["status"] == "running"
		controller._executor.submit.assert_called_once()
		with pytest.raises(ValueError, match="正在运行"):
			controller.start("prepare", {})
	finally:
		controller.close()


def test_single_call_waits_and_fills_five_plus_five(tmp_path, clock):
	platform = platform_for([candidate(i) for i in range(1, 6)], [candidate(i, "internship") for i in range(6, 11)])
	options = params({"recruitment_quotas": {"campus": 5, "internship": 5}})
	options.update(
		max_seconds=900,
		collection_policy={**COLLECTION_POLICY, "request_limit": 4, "window_seconds": 60, "interval_seconds": 1},
		continue_after_cooldown=True,
	)
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		batch = collect(store, cache, platform, options)
		assert batch["stop_reason"] == "target_reached"
		assert batch["stats"]["recruitment_counts"] == {"campus": 5, "internship": 5}
		assert batch["stats"]["network_requests"] == 12
		assert batch["stats"]["cooldown_waits"] >= 2
		assert len(store.list_batches()) == 1
		assert all(item["status"] == "draft" for item in batch["items"])
		assert platform.sent == []


def test_stop_during_budget_wait_preserves_already_found_jobs(tmp_path, clock):
	platform = platform_for([candidate(1), candidate(2)], [])
	options = settings(continue_after_cooldown=True)
	options["collection_policy"]["request_limit"] = 2
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		with pytest.raises(DeliveryError, match="停止等待"):
			collect(store, cache, platform, options, should_stop=lambda: clock.now >= 1020)
		batch = store.get(store.list_batches()[0]["id"])
		assert len(batch["items"]) == 1
		assert batch["stats"]["network_requests"] == 2
		assert platform.details == ["job-1"]


def test_automatic_local_wait_never_retries_a_real_platform_verification(tmp_path, clock):
	platform = platform_for([candidate(1)], [])
	platform.job_card_browser = Mock(return_value={"code": 37, "message": "环境存在异常"})
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		with pytest.raises(EnvironmentRiskError):
			collect(store, cache, platform, settings(continue_after_cooldown=True))
		platform.job_card_browser.assert_called_once()
		assert store.get(store.list_batches()[0]["id"])["stop_reason"] == "environment_risk"


def test_local_drafts_remain_available_during_platform_cooldown(tmp_path, monkeypatch):
	controller, bid = fixture_client(tmp_path)
	try:
		controller._set_browser_state("blocked", "Verification required", blocked_until=10**10)
		controller._service_and_resume = Mock(return_value=(Mock(), "fixture"))
		controller._network_operation = Mock(side_effect=AssertionError("No BOSS access"))
		draft = Mock()
		monkeypatch.setattr("boss_agent_cli.client_controller.draft_batch", draft)
		controller._run("draft", {"batch_id": bid})
		assert controller.operation["status"] == "complete"
		assert controller.state()["browser"]["status"] == "blocked"
		draft.assert_called_once()
		controller._network_operation.assert_not_called()
	finally:
		controller.close()


def test_rate_limit_also_persists_the_cooldown(tmp_path):
	controller, _ = fixture_client(tmp_path)
	try:
		controller._network_operation = Mock(side_effect=DeliveryError("RATE_LIMITED", "Rate limited"))
		controller._run("prepare", {})
		assert controller.state()["browser"]["status"] == "blocked"
		assert controller.state()["browser"]["blocked_until"] > 0
		with pytest.raises(ValueError, match="冷却"):
			controller.start("connect", {})
	finally:
		controller.close()
