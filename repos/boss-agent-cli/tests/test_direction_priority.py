"""Read strong role evidence first, keeping weak candidates durable for fallback."""

from unittest.mock import Mock

import pytest

from boss_agent_cli.api.zhipin_errors import EnvironmentRiskError
from boss_agent_cli.cache.store import CacheStore
from boss_agent_cli.delivery import collect_batch
from boss_agent_cli.delivery_store import DeliveryStore
from test_collection_policy import clock  # noqa: F401
from test_delivery_quotas_history import candidate, params, platform_for


def options(count):
	p = params({"target_roles": ["ai_development", "fullstack", "agent_development", "ai_infra"]}, count=count)
	p.update(prioritize_explicit_evidence=True, completion_first=True)
	return p


def noisy():
	return [{**candidate(i), "jobName": f"27届校招-系统研发工程师 {i}"} for i in range(1, 14)]


def test_noisy_page_reads_one_weak_jd_then_other_directions(tmp_path):
	platform = platform_for(noisy(), [candidate(i, "internship") for i in range(30, 35)])
	original_search = platform.search_jobs.side_effect
	platform.search_jobs.side_effect = lambda query, **kw: (
		{"code": 0, "zpData": {"jobList": [candidate(i) for i in range(40, 45)]}}
		if query == "precise" else original_search(query, **kw)
	)
	p = options(10)
	p["rules"]["recruitment_quotas"] = {"campus": 5, "internship": 5}
	p["searches"].append({"query": "precise", "city": "北京", "kind": "campus"})
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		bid = store.create(p)
		collect_batch(store, bid, platform, cache, Mock())
		batch = store.get(bid)
		assert len(batch["items"]) == 10
		assert platform.details == ["job-1", *[f"job-{i}" for i in range(30, 35)], *[f"job-{i}" for i in range(40, 45)]]
		assert batch["stats"]["recruitment_counts"] == {"campus": 5, "internship": 5}
		assert batch["stats"]["deferred_details"] == 12
		assert batch["checkpoint"]["deferred_queue"]
		assert not platform.sent


def test_weak_candidate_with_relevant_jd_is_not_lost_after_restart(tmp_path):
	platform = platform_for(noisy(), [])
	read = platform.job_card_browser
	def description(sid, lid=""):
		result = read(sid, lid)
		if sid == "sec-13":
			result["zpData"]["jobCard"]["postDescription"] = "2027届本科校招，负责大模型应用开发。"
		return result
	platform.job_card_browser = description
	original_search = platform.search_jobs.side_effect
	def search(query, **kwargs):
		if query == "intern":
			return {"code": 37, "message": "您的环境存在异常."}
		return original_search(query, **kwargs)
	platform.search_jobs.side_effect = search
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		bid = store.create(options(1))
		with pytest.raises(EnvironmentRiskError):
			collect_batch(store, bid, platform, cache, Mock())
		checkpoint = store.get(bid)["checkpoint"]
		assert len(checkpoint["deferred_queue"]) == 1
		assert "job-13" not in checkpoint["seen"]
	platform.search_jobs.side_effect = original_search
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		collect_batch(store, bid, platform, cache, Mock())
		batch = store.get(bid)
		assert [item["job_id"] for item in batch["items"]] == ["job-13"]
		assert len(platform.details) == len(set(platform.details)) == 13
		assert batch["checkpoint"]["deferred_pass"] is True
		assert batch["stop_reason"] == "target_reached"


def test_repeating_page_is_still_bounded_when_weak_candidates_are_deferred(tmp_path):
	platform = platform_for(noisy(), [])
	platform.search_jobs.side_effect = lambda *a, **kw: {"code": 0, "zpData": {"jobList": noisy(), "hasMore": True}}
	p = options(10)
	p["searches"] = [{"query": "campus", "city": "北京", "kind": "campus"}]
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		bid = store.create(p)
		collect_batch(store, bid, platform, cache, Mock())
		assert store.get(bid)["stop_reason"] == "results_exhausted"
		assert platform.search_jobs.call_count == 3
		assert len(platform.details) == 13
