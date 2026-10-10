"""Client pacing and pagination regressions, with fixture-only platform requests."""

import copy
import json
from collections import Counter, deque
from unittest.mock import Mock

import pytest

from boss_agent_cli.api.zhipin_errors import EnvironmentRiskError
from boss_agent_cli.cache.store import CacheStore
from boss_agent_cli.delivery import COLLECTION_POLICY, _client_pending_pages, client_collection_policy, collect_batch
from boss_agent_cli.delivery_store import DeliveryStore
from test_collection_policy import clock  # noqa: F401
from test_delivery import FakePlatform
from test_delivery_quotas_history import candidate, params, platform_for


def options():
	p = params({"recruitment_quotas": {"campus": 5, "internship": 5}})
	p.update(browser_source="existing-browser", collection_policy=dict(COLLECTION_POLICY), completion_first=True, continue_after_cooldown=True)
	return p


def test_old_client_policy_is_spread_across_window_without_mutating_snapshot():
	old = dict(COLLECTION_POLICY)
	assert old["interval_seconds"] == 12
	new = client_collection_policy(old)
	assert new["interval_seconds"] == 30
	assert old == COLLECTION_POLICY
	assert {k: v for k, v in new.items() if k != "interval_seconds"} == {k: v for k, v in old.items() if k != "interval_seconds"}
	assert client_collection_policy({**old, "interval_seconds": 60})["interval_seconds"] == 60
	assert client_collection_policy({**old, "request_limit": 4})["interval_seconds"] == 150


def test_client_finishes_five_plus_five_without_a_twelve_second_burst(tmp_path, clock):  # noqa: F811
	p = options()
	platform = platform_for([candidate(i) for i in range(1, 6)], [candidate(i, "internship") for i in range(6, 11)])
	read_times, end_times = [], []
	search, detail = platform.search_jobs.side_effect, platform.job_card_browser
	def record(function, *args, **kwargs):
		read_times.append(clock.now)
		value = function(*args, **kwargs)
		clock.now += 4
		end_times.append(clock.now)
		return value
	platform.search_jobs.side_effect = lambda *a, **kw: record(search, *a, **kw)
	platform.job_card_browser = lambda *a, **kw: record(detail, *a, **kw)
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		bid = store.create(p)
		collect_batch(store, bid, platform, cache, Mock())
		batch = store.get(bid)
		assert len(batch["items"]) == 10
		assert batch["stats"]["recruitment_counts"] == {"campus": 5, "internship": 5}
		assert batch["stats"]["effective_interval_seconds"] == 30
		assert store.get(bid)["params"] == p
		assert len(read_times) == 12
		assert all(start - finish >= 30 for start, finish in zip(read_times[1:], end_times[:-1]))
		assert all(sum(at <= other < at + 60 for other in read_times) <= 2 for at in read_times)
		assert not platform.sent


def test_recent_completion_still_limits_new_batch_after_cache_restart(tmp_path, clock):  # noqa: F811
	p = options()
	p.update(count=1)
	p["rules"]["recruitment_quotas"] = {"campus": 1, "internship": 0}
	platform = platform_for([candidate(1)], [])
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		bid = store.create(p)
		collect_batch(store, bid, platform, cache, Mock())
		finished = clock.now
	clock.now += 5
	p["searches"] = [{"query": "new-query", "city": "北京", "kind": "campus"}]
	times = []
	def empty(*args, **kwargs):
		times.append(clock.now)
		return {"code": 0, "zpData": {"jobList": [], "hasMore": False}}
	platform.search_jobs.side_effect = empty
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		collect_batch(store, store.create(p), platform, cache, Mock())
	assert times == [finished + 30]


def test_page_two_can_fill_the_batch_before_114_unrelated_first_pages(tmp_path, clock):  # noqa: F811
	p = options()
	p["searches"] = [
		{"query": "campus", "city": "北京", "kind": "campus"},
		{"query": "intern", "city": "北京", "kind": "internship"},
		*[{"query": f"unneeded-{i}", "city": "上海", "kind": "internship"} for i in range(112)],
	]
	platform = FakePlatform()
	def search(query, *, page, **kwargs):
		if query not in {"campus", "intern"}:
			raise AssertionError("A later city must not starve the useful second page")
		kind = "campus" if query == "campus" else "internship"
		if page == 1:
			jobs = [{**candidate(100 if kind == "campus" else 200, kind), "salaryDesc": "14-20K" if kind == "campus" else "200-300元/天"}]
		else:
			jobs = [candidate(i, kind) for i in (range(1, 6) if kind == "campus" else range(6, 11))]
		return {"code": 0, "zpData": {"jobList": jobs, "hasMore": page == 1}}
	platform.search_jobs = Mock(side_effect=search)
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		bid = store.create(p)
		collect_batch(store, bid, platform, cache, Mock())
		batch = store.get(bid)
		assert len(batch["items"]) == 10 and batch["stop_reason"] == "target_reached"
		assert [(call.args[0], call.kwargs["page"]) for call in platform.search_jobs.call_args_list] == [("campus", 1), ("campus", 2), ("intern", 1), ("intern", 2)]
		assert batch["stats"]["network_requests"] == 14 and batch["stats"]["pages"] == 4
		assert batch["stats"]["recruitment_counts"] == {"campus": 5, "internship": 5}
		assert not platform.sent


def test_three_page_segments_yield_then_return_without_losing_page_four(tmp_path, clock):  # noqa: F811
	p = options()
	platform = FakePlatform()
	def search(query, *, page, **kwargs):
		job = {**candidate(page if query == "campus" else page + 10), "salaryDesc": "14-20K"}
		return {"code": 0, "zpData": {"jobList": [job], "hasMore": page < 4}}
	platform.search_jobs = Mock(side_effect=search)
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		bid = store.create(p)
		collect_batch(store, bid, platform, cache, Mock())
		assert [(c.args[0], c.kwargs["page"]) for c in platform.search_jobs.call_args_list] == [("campus", 1), ("campus", 2), ("campus", 3), ("intern", 1), ("intern", 2), ("intern", 3), ("campus", 4), ("intern", 4)]
		assert store.get(bid)["stop_reason"] == "results_exhausted"
		assert not platform.details


def test_resumed_queue_keeps_failed_head_exact_and_brings_page_two_forward():
	searches = [{"query": name, "city": "北京", "kind": "internship", "filters": {"experience_code": "108", "experience": "在校生"}} for name in ["old", "failed", "new"]]
	def entry(index, page):
		s = searches[index]
		return [s["query"], s["city"], page, s["kind"], s["filters"]]
	queue = deque([entry(1, 1), entry(2, 1), entry(0, 4), entry(0, 2)])
	before = copy.deepcopy(queue)
	actual = _client_pending_pages(queue, searches)
	assert actual[0] == before[0]
	assert [(e[0], e[2]) for e in actual] == [("failed", 1), ("old", 2), ("new", 1), ("old", 4)]
	assert Counter(json.dumps(e, sort_keys=True) for e in actual) == Counter(json.dumps(e, sort_keys=True) for e in before)
	assert queue == before


def test_refused_head_is_not_skipped_for_a_different_query_or_page(tmp_path, clock):  # noqa: F811
	p = options()
	platform = platform_for([], [])
	platform.search_jobs.side_effect = None
	platform.search_jobs.return_value = {"code": 37, "message": "您的环境存在异常."}
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		bid = store.create(p)
		queue = [["intern", "北京", 1, "internship", {}], ["campus", "北京", 2, "campus", {}]]
		store.collection_progress(bid, {"version": 2, "queue": queue, "seen": [], "page_fingerprints": {}, "deferred_queue": []}, {}, "environment_risk")
		with pytest.raises(EnvironmentRiskError):
			collect_batch(store, bid, platform, cache, Mock())
		assert platform.search_jobs.call_count == 1
		assert platform.search_jobs.call_args.args == ("intern",)
		assert store.get(bid)["checkpoint"]["queue"][0] == queue[0]
		assert not platform.sent
