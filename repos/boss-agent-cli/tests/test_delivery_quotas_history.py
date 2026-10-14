"""Fixed campus/intern quotas and greeting-only deduplication."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from boss_agent_cli.cache.store import CacheStore
from boss_agent_cli.delivery import collect_batch, write_preview
from boss_agent_cli.delivery_store import DeliveryStore
from test_delivery import FakePlatform, job


def candidate(number, kind="campus", company=None):
	return {
		**job(number),
		"jobName": "AI开发实习生" if kind == "internship" else "2027届秋招AI开发",
		"jobType": 4 if kind == "internship" else None,
		"salaryDesc": "300-500元/天" if kind == "internship" else "15-25K",
		"brandName": company or f"Company {number}",
	}


def params(rules=None, count=10, searches=None):
	return {
		"query": "campus",
		"count": count,
		"max_pages": 10,
		"max_details": 60,
		"max_seconds": 60,
		"searches": searches
		or [
			{"query": "campus", "city": "北京", "kind": "campus"},
			{"query": "intern", "city": "北京", "kind": "internship"},
		],
		"rules": {
			"graduation_year": 2027,
			"bachelor_eligible": True,
			"min_salary": 15,
			"min_daily_salary": 300,
			**(rules or {}),
		},
	}


def platform_for(campus, interns):
	platform = FakePlatform()
	platform.search_jobs = Mock(
		side_effect=lambda query, **kwargs: {
			"code": 0,
			"zpData": {"jobList": campus if query == "campus" else interns, "hasMore": False},
		}
	)
	return platform


def run(store, cache, platform, settings):
	batch_id = store.create(settings)
	collect_batch(store, batch_id, platform, cache, Mock())
	return store.get(batch_id)


@pytest.mark.parametrize("intern_first", [False, True])
def test_exact_five_plus_five_stops_reading_filled_kind(tmp_path, intern_first):
	platform = platform_for([candidate(i) for i in range(1, 21)], [candidate(i, "internship") for i in range(21, 41)])
	settings = params({"recruitment_quotas": {"campus": 5, "internship": 5}})
	if intern_first:
		settings["searches"].reverse()
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		batch = run(store, cache, platform, settings)
		assert batch["stats"]["recruitment_counts"] == {"campus": 5, "internship": 5}
		assert batch["stats"]["quota_shortfall"] == {"campus": 0, "internship": 0}
		assert batch["stop_reason"] == "target_reached"
		assert len(platform.details) == 10
		assert len(batch["items"]) == 10
		assert platform.sent == []


@pytest.mark.parametrize(
	"campus_count,intern_count,expected",
	[(20, 2, {"campus": 5, "internship": 2}), (2, 20, {"campus": 2, "internship": 5})],
)
def test_shortfall_is_not_filled_with_other_kind(tmp_path, campus_count, intern_count, expected):
	platform = platform_for(
		[candidate(i) for i in range(1, campus_count + 1)],
		[candidate(100 + i, "internship") for i in range(intern_count)],
	)
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		batch = run(store, cache, platform, params({"recruitment_quotas": {"campus": 5, "internship": 5}}))
		assert batch["stats"]["recruitment_counts"] == expected
		assert len(batch["items"]) == 7
		assert batch["stats"]["quota_shortfall"] == {kind: 5 - count for kind, count in expected.items()}
		preview = write_preview(store, batch["id"])
		assert "本批不满配额" in Path(preview).read_text(encoding="utf-8")


def test_filled_search_kind_is_not_requested_again(tmp_path):
	platform = platform_for([candidate(i) for i in range(1, 6)], [candidate(i, "internship") for i in range(6, 11)])
	settings = params({"recruitment_quotas": {"campus": 5, "internship": 5}})
	settings["searches"].insert(1, {"query": "unneeded", "city": "上海", "kind": "campus"})
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		run(store, cache, platform, settings)
	assert [call.args[0] for call in platform.search_jobs.call_args_list] == ["campus", "intern"]


@pytest.mark.parametrize(
	"bad_rules",
	[
		{"recruitment_quotas": {"campus": 6, "internship": 5}},
		{"recruitment_quotas": {"campus": -1, "internship": 11}},
		{"recruitment_quotas": {"campus": 5, "internship": 5}, "max_internship_ratio": 0.5},
		{"history_dedupe": "unsupported"},
	],
)
def test_invalid_settings_fail_before_network(tmp_path, bad_rules):
	platform = platform_for([], [])
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		with pytest.raises(ValueError):
			run(store, cache, platform, params(bad_rules))
	platform.search_jobs.assert_not_called()


def seed_prior(store, status):
	batch_id = store.create(params(count=1))
	payload = {"job_id": "job-1", "company": "ＡＣＭＥ 科技", "greeting": "Approved later"}
	store.add(batch_id, 1, payload)
	if status != "draft":
		store.save_draft(batch_id, 1, payload)
	if status == "rejected":
		store.review(batch_id, approve=set(), reject={1})
	if status in {"approved", "sending", "uncertain", "not_sent", "sent"}:
		store.review(batch_id, approve={1}, reject=set())
	if status in {"sending", "uncertain", "not_sent", "sent"}:
		store.claim(batch_id, 1)
		if status == "sent":
			receipt = FakePlatform().greet("sec-1", "job-1", payload["greeting"])["zpData"]["greeting_receipt"]
			store.finish(batch_id, 1, status, receipt=receipt)
		elif status != "sending":
			store.finish(batch_id, 1, status)
	return batch_id


@pytest.mark.parametrize("status", ["draft", "pending", "rejected", "approved", "sending", "uncertain", "not_sent"])
@pytest.mark.parametrize("scope", ["", "job", "company", "greeted"])
def test_history_dedupe_keeps_jobs_without_confirmed_greetings(tmp_path, status, scope):
	platform = platform_for([candidate(1, company="ACME 科技"), candidate(2, company="acme科技"), candidate(3)], [])
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		prior = seed_prior(store, status)
		before = store.get(prior)
		batch = run(store, cache, platform, params({"history_dedupe": scope}))
		assert [item["job_id"] for item in batch["items"]] == ["job-1", "job-2", "job-3"]
		assert platform.details == ["job-1", "job-2", "job-3"]
		assert batch["stats"]["excluded"] == []
		assert store.get(prior) == before
		assert platform.sent == []


def test_legacy_company_mode_keeps_all_ungreeted_company_jobs(tmp_path):
	platform = platform_for([candidate(1, company="Same"), candidate(2, company="Same"), candidate(3)], [])
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		batch = run(store, cache, platform, params({"history_dedupe": "company"}))
		assert [item["job_id"] for item in batch["items"]] == ["job-1", "job-2", "job-3"]
		assert batch["stats"]["excluded"] == []
		preview = Path(write_preview(store, batch["id"])).read_text(encoding="utf-8")
		assert "仅跳过已打招呼的岗位" in preview
		assert "以前入选过的公司不再入选" not in preview


@pytest.mark.parametrize("record_source", ["delivery", "cache_job", "cache_security"])
def test_only_greeted_job_is_skipped_and_same_company_remains(tmp_path, record_source):
	platform = platform_for([candidate(1, company="Same"), candidate(2, company="Same"), candidate(3)], [])
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		if record_source == "delivery":
			seed_prior(store, "sent")
		elif record_source == "cache_job":
			cache.record_greet("rotated-security", "job-1")
		else:
			cache.record_greet("sec-1", "old-job-id")
		batch = run(store, cache, platform, params({"history_dedupe": "company"}))
		assert [item["job_id"] for item in batch["items"]] == ["job-2", "job-3"]
		assert platform.details == ["job-2", "job-3"]
		excluded = batch["stats"]["excluded"]
		assert len(excluded) == 1 and excluded[0]["job_id"] == "job-1"
		assert excluded[0]["reasons"] == ["已打招呼的岗位"]
		assert excluded[0]["company"] == "Same"
		assert excluded[0]["city"] == "北京" and excluded[0]["salary"] == "15-25K"
		assert excluded[0]["removed_at"] > 0
		assert not any("security_id" in row or "lid" in row for row in excluded)
		assert platform.sent == []


def test_clear_dedupe_history_keeps_other_exclusions_and_delivery_records(tmp_path):
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		prior = seed_prior(store, "sent")
		cache.record_greet("old-security", "job-1")
		checkpoint = {"version": 2, "seen": ["job-1"], "queue": []}
		stats = {"collected": 1, "excluded": [
			{"job_id": "job-2", "reasons": ["历史批次已入选同一岗位"]},
			{"title": "legacy", "reasons": ["历史批次或本批已入选同一公司"]},
			{"job_id": "job-1", "reasons": ["已打招呼的岗位", "薪资低于下限"]},
			{"title": "unrelated", "reasons": ["信息缺失:完整JD"]},
		]}
		store.collection_progress(prior, checkpoint, stats, "results_exhausted")
		before = store.get(prior)
		protected = ("attempts", "delivery_events", "message_receipts", "collection_checkpoints")
		records = {table: list(map(tuple, store.db.execute(f"SELECT * FROM {table}"))) for table in protected}
		for _ in range(100):
			store.create(params())
		assert prior not in {row["id"] for row in store.list_batches()}
		assert store.clear_dedupe_history() == 3
		after = store.get(prior)
		assert after == {**before, "stats": {**stats, "excluded": [
			{"job_id": "job-1", "reasons": ["薪资低于下限"]},
			{"title": "unrelated", "reasons": ["信息缺失:完整JD"]},
		]}}
		assert {table: list(map(tuple, store.db.execute(f"SELECT * FROM {table}"))) for table in protected} == records
		assert cache.is_greeted_job("job-1") and store.is_greeted_job("job-1")
		assert store.clear_dedupe_history() == 0


def test_three_hundred_is_accepted_but_lower_is_not(tmp_path):
	rows = [candidate(1, "internship"), {**candidate(2, "internship"), "salaryDesc": "299-500元/天"}]
	platform = platform_for([], rows)
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		batch = run(store, cache, platform, params())
		assert [item["job_id"] for item in batch["items"]] == ["job-1"]
