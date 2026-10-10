"""Explicit deadline and drafting regressions; all platform and AI traffic is mocked."""

import threading
import time
from unittest.mock import Mock

import pytest

from boss_agent_cli.cache.store import CacheStore
from boss_agent_cli.client_controller import search_plan
from boss_agent_cli.delivery import COLLECTION_POLICY, collect_batch, draft_batch
from boss_agent_cli.delivery_store import DeliveryStore
from client_fixture import fixture_client
from test_api_client_methods import _make_client
from test_collection_policy import clock  # noqa: F401
from test_delivery import FakeAI
from test_delivery_quotas_history import candidate, params, platform_for


def test_live_verified_experience_codes_reach_the_api_without_legacy_mapping():
	options = params()
	options.update(query="AI开发 校招", extra_queries=[])
	options["rules"]["city"] = ["北京"]
	with _make_client() as client:
		for search, expected in zip(search_plan(options), ["102", "108"], strict=True):
			client.search_jobs(search["query"], city=search["city"], **search["filters"])
			actual = client._browser_request.call_args.kwargs["params"]
			assert actual["experience"] == expected
			assert "jobType" not in actual


def test_noisy_lists_fill_ten_with_twelve_spaced_reads_within_five_minutes(tmp_path, clock):  # noqa: F811
	noise = [{**candidate(100 + i), "jobName": "AI开发工程师", "jobExperience": "3-5年"} for i in range(30)]
	platform = platform_for(noise + [candidate(i) for i in range(1, 6)], [candidate(i, "internship") for i in range(6, 11)])
	options = params({"recruitment_quotas": {"campus": 5, "internship": 5}})
	options.update(max_seconds=240, collection_policy=dict(COLLECTION_POLICY), prioritize_explicit_evidence=True, continue_after_cooldown=True)
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		bid = store.create(options)
		start = clock.now
		collect_batch(store, bid, platform, cache, Mock(), deadline=start + 240)
		batch = store.get(bid)
		assert batch["stop_reason"] == "target_reached"
		assert batch["stats"]["recruitment_counts"] == {"campus": 5, "internship": 5}
		assert batch["stats"]["network_requests"] == 12
		assert 132 <= clock.now - start < 240
		assert len(platform.details) == 10 and platform.sent == []


def test_social_hire_page_does_not_consume_the_whole_detail_budget(tmp_path, clock):  # noqa: F811
	noise = [{**candidate(100 + i), "jobName": "AI开发工程师", "jobExperience": "经验不限"} for i in range(15)]
	platform = platform_for(noise, [candidate(i, "internship") for i in range(1, 6)])
	options = params()
	options.update(max_seconds=240, collection_policy=dict(COLLECTION_POLICY), prioritize_explicit_evidence=True)
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		bid = store.create(options)
		collect_batch(store, bid, platform, cache, Mock(), deadline=clock.now + 240)
		batch = store.get(bid)
		assert len(batch["items"]) == 5
		assert batch["stats"]["deferred_details"] == 14
		assert len(platform.details) == 6
		assert all(item["payload"]["recruitment_kind"] == "internship" for item in batch["items"])


def test_client_waits_for_budget_without_an_overall_deadline(tmp_path, clock, monkeypatch):  # noqa: F811
	controller, _ = fixture_client(tmp_path)
	try:
		monkeypatch.setattr("boss_agent_cli.client_controller.time", clock)
		controller._browser_state["collection_resume_at"] = clock.now + 650
		controller.cancel.wait = clock.sleep
		start = clock.now
		controller._wait_collection_start()
		assert clock.now - start == 650
		assert controller.operation["phase"] == "connecting"
	finally:
		controller.close()


def test_fresh_validation_never_repackages_incomplete_previews(tmp_path, monkeypatch):
	controller, _ = fixture_client(tmp_path)
	try:
		controller._set_browser_state("blocked", "Verification required", blocked_until=10**10)
		seed = Mock(side_effect=AssertionError("Fresh run cannot carry previous items"))
		monkeypatch.setattr("boss_agent_cli.client_controller.seed_batch_from_local", seed)
		monkeypatch.setattr("boss_agent_cli.client_controller.BossClient", Mock(side_effect=AssertionError("No BOSS access")))
		controller._run("prepare", {"fresh_only": True})
		seed.assert_not_called()
		batch = controller.batch(controller.operation["batch_id"])
		assert batch["items"] == []
		assert batch["params"]["fresh_only"] is True
		assert controller.operation["status"] == "error"
		with pytest.raises(ValueError, match="布尔"):
			controller.start("prepare", {"fresh_only": "yes"})
	finally:
		controller.close()


def test_deadline_reserves_time_for_inflight_read_and_never_resets_rate_budget(tmp_path, clock):  # noqa: F811
	platform = platform_for([candidate(i) for i in range(1, 20)], [])
	options = params()
	options.update(max_seconds=240, collection_policy=dict(COLLECTION_POLICY))
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		bid = store.create(options)
		collect_batch(store, bid, platform, cache, Mock(), deadline=clock.now + 60)
		batch = store.get(bid)
		assert batch["stop_reason"] == "time_limit"
		assert batch["stats"]["network_requests"] == 3
		assert len(batch["items"]) == 2
		assert cache.discovery_budget(limit=20, window_seconds=600)["count"] == 3


def test_parallel_drafts_keep_identity_order_and_main_thread_database_writes(tmp_path):
	class ConcurrentAI(FakeAI):
		def __init__(self):
			super().__init__()
			self.lock = threading.Lock()
			self.running = self.peak = 0

		def chat(self, *args, **kwargs):
			with self.lock:
				self.running += 1
				self.peak = max(self.peak, self.running)
			time.sleep(0.02)
			try:
				return super().chat(*args, **kwargs)
			finally:
				with self.lock:
					self.running -= 1

	service = ConcurrentAI()
	with DeliveryStore(tmp_path) as store:
		bid = store.create(params())
		for i in range(1, 11):
			store.add(bid, i, {"job_id": f"j{i}", "title": f"AI {i}", "company": f"Company {i}", "description": f"Full JD {i}"})
		draft_batch(store, bid, service, "Python项目经历", Mock(), workers=3, deadline=time.monotonic() + 10)
		items = store.get(bid)["items"]
		assert service.peak == 3
		assert all(item["status"] == "pending" for item in items)
		assert all(item["payload"]["summary"] == f"Full JD {item['number']}" for item in items)
		assert all(0 < opt["timeout"] <= 10 for opt in service.options)
		assert all(not store.attempted(item["job_id"]) for item in items)


def test_expired_draft_deadline_makes_no_ai_calls_and_preserves_unapproved_jobs(tmp_path):
	with DeliveryStore(tmp_path) as store:
		bid = store.create(params())
		store.add(bid, 1, {"job_id": "j1", "title": "AI", "company": "C", "description": "Full JD"})
		service = Mock()
		draft_batch(store, bid, service, "resume", Mock(), workers=3, deadline=time.monotonic() - 1)
		service.chat.assert_not_called()
		assert store.get(bid)["items"][0]["status"] == "draft"
		assert store.get(bid)["stats"]["drafts_pending"] == 1


def test_local_draft_repair_clears_only_resolved_error_metadata(tmp_path):
	with DeliveryStore(tmp_path) as store:
		bid = store.create(params())
		store.add(bid, 1, {"job_id": "j1", "title": "AI", "company": "C", "description": "Full JD"})
		store.progress(bid, {"drafts_pending": 1, "draft_errors": [{"number": 1, "code": "AI_PARSE_ERROR"}]}, "target_reached_local")
		draft_batch(store, bid, FakeAI(), "Python项目经历", Mock())
		batch = store.get(bid)
		assert batch["stats"]["drafts_pending"] == 0
		assert batch["stats"]["draft_errors"] == []
		assert batch["stop_reason"] == "target_reached_local"
