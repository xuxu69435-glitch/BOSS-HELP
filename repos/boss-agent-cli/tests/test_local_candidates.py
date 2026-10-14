import json
from unittest.mock import Mock

import pytest

from boss_agent_cli.cache.store import CacheStore
from boss_agent_cli.delivery import collect_batch, seed_batch_from_local
from boss_agent_cli.delivery_store import DeliveryStore
from client_fixture import fixture_client
from test_delivery_quotas_history import params


def options(count=10):
	p = params(
		{
			"recruitment_quotas": {"campus": count // 2, "internship": count // 2},
			"history_dedupe": "job",
			"target_roles": ["ai_development"],
			"city": ["北京"],
		},
		count=count,
	)
	p["resume"] = "test"
	return p


def fact(number, kind="campus", **updates):
	return {
		"job_id": f"cached-{number}",
		"security_id": f"synthetic-{number}",
		"title": "2027届校招AI开发" if kind == "campus" else "AI开发实习生",
		"company": f"Fixture {number}",
		"city": "北京",
		"education": "本科",
		"salary": "15-25K" if kind == "campus" else "300-500元/天",
		"description": "本科及以上学历。负责AI应用开发。",
		"detail_source": "search_browser_job_card",
		**updates,
	}


def cache_fact(cache, number, kind="campus", **updates):
	cache.put_search({"fixture": number}, json.dumps(fact(number, kind, **updates), ensure_ascii=False))


def test_seed_fills_exact_quotas_without_any_platform(tmp_path):
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		for i in range(1, 11):
			cache_fact(cache, i, "campus" if i <= 5 else "internship")
		bid = store.create(options())
		assert seed_batch_from_local(store, bid, cache, Mock()) == 10
		assert store.get(bid)["stats"]["recruitment_counts"] == {"campus": 5, "internship": 5}
		assert store.get(bid)["stats"]["network_requests"] == 0
		platform = Mock()
		collect_batch(store, bid, platform, cache, Mock())
		platform.search_jobs.assert_not_called()
		assert len(store.get(bid)["items"]) == 10


def test_unfinished_source_is_preserved_and_can_only_be_carried_once(tmp_path):
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		source = store.create(options(2))
		job = fact(1, "internship")
		store.add(source, 1, job)
		store.save_draft(source, 1, {**job, "greeting": "Unreviewed previous wording"})
		store.progress(source, {}, "request_limit")
		before = store.get(source)
		cache_fact(cache, 2)
		bid = store.create(options(2))
		assert seed_batch_from_local(store, bid, cache, Mock()) == 2
		assert store.get(source) == before
		assert store.get(bid)["params"]["source_batches"] == [source]
		assert all("greeting" not in item["payload"] for item in store.get(bid)["items"])
		next_id = store.create(options(2))
		assert seed_batch_from_local(store, next_id, cache, Mock()) == 1
		assert store.get(next_id)["items"][0]["job_id"] == "cached-2"


@pytest.mark.parametrize("status,expected", [("approved", 1), ("uncertain", 1), ("sent", 0)])
def test_local_facts_only_skip_confirmed_greetings(tmp_path, status, expected):
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		source = store.create(options(2))
		job = fact(1)
		store.add(source, 1, job)
		store.save_draft(source, 1, {**job, "greeting": "Approved wording"})
		store.review(source, approve={1}, reject=set())
		if status != "approved":
			store.claim(source, 1)
			store.finish(source, 1, "uncertain")
			if status == "sent":
				with store.db:
					store.db.execute("UPDATE items SET status='sent' WHERE batch_id=?", (source,))
		cache_fact(cache, 1)
		before = store.get(source)
		bid = store.create(options(2))
		assert seed_batch_from_local(store, bid, cache, Mock()) == expected
		assert store.get(bid)["params"]["source_batches"] == []
		assert store.get(source) == before
		assert all("greeting" not in item["payload"] for item in store.get(bid)["items"])


def test_local_greeting_exclusions_survive_subsequent_collection(tmp_path):
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		source = store.create(options(2))
		store.add(source, 1, fact(1))
		cache.record_greet("rotated-security", "cached-1")
		cache_fact(cache, 1)
		bid = store.create(options(2))
		assert seed_batch_from_local(store, bid, cache, Mock()) == 0
		records = store.get(bid)["stats"]["excluded"]
		assert records[0]["company"] == "Fixture 1"
		assert records[0]["reasons"] == ["已打招呼的岗位"]
		platform = Mock()
		platform.search_jobs.return_value = {"code": 0, "zpData": {"jobList": [], "hasMore": False}}
		platform.is_success.return_value = True
		platform.unwrap_data.side_effect = lambda response: response["zpData"]
		collect_batch(store, bid, platform, cache, Mock())
		assert store.get(bid)["stats"]["excluded"] == records
		collect_batch(store, bid, platform, cache, Mock())
		assert store.get(bid)["stats"]["excluded"] == records


def test_rejected_local_job_and_same_company_jobs_can_be_selected_again(tmp_path):
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		source = store.create(options(2))
		store.add(source, 1, fact(1, company="Same"))
		store.review(source, approve=set(), reject={1})
		cache_fact(cache, 1, company="Same")
		cache_fact(cache, 2, "internship", company="Same")
		settings = options(2)
		settings["rules"]["history_dedupe"] = "company"
		bid = store.create(settings)
		assert seed_batch_from_local(store, bid, cache, Mock()) == 2
		assert {item["job_id"] for item in store.get(bid)["items"]} == {"cached-1", "cached-2"}
		assert store.get(bid)["stats"]["excluded"] == []


def test_cache_join_keeps_explicit_internship_type_from_list(tmp_path):
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		cache_fact(cache, 1, "internship", title="2027校招AI开发", raw_job_type=None)
		cache.put_search(
			{"fixture-list": 1},
			json.dumps(
				{
					"jobList": [
						{
							"encryptJobId": "cached-1",
							"securityId": "synthetic-1",
							"jobName": "2027校招AI开发",
							"jobType": 4,
							"salaryDesc": "300-500元/天",
							"cityName": "北京",
							"jobDegree": "本科",
						}
					]
				}
			),
		)
		bid = store.create(options(2))
		assert seed_batch_from_local(store, bid, cache, Mock()) == 1
		assert store.get(bid)["items"][0]["payload"]["recruitment_kind"] == "internship"


def test_cached_low_pay_and_unknown_undergraduate_eligibility_remain_excluded(tmp_path):
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		cache_fact(cache, 1, salary="14-30K")
		cache_fact(cache, 2, "internship", salary="299-500元/天")
		cache_fact(cache, 3, education="硕士", description="硕士及以上学历")
		bid = store.create(options())
		assert seed_batch_from_local(store, bid, cache, Mock()) == 0


def test_controller_can_complete_from_cache_while_boss_stays_blocked(tmp_path, monkeypatch):
	controller, _ = fixture_client(tmp_path)
	try:
		with CacheStore(tmp_path / "cache/boss_agent.db") as cache:
			for i in range(1, 11):
				cache_fact(cache, i, "campus" if i <= 5 else "internship")
		controller._set_browser_state("blocked", "Platform verification required", blocked_until=10**10)
		monkeypatch.setattr(
			"boss_agent_cli.client_controller.BossClient", Mock(side_effect=AssertionError("No browser requests"))
		)
		controller._service_and_resume = Mock(return_value=(Mock(), "fixture resume"))
		def complete_drafts(store, batch_id, *args, **kwargs):
			for item in store.get(batch_id)["items"]:
				store.save_draft(batch_id, item["number"], {**item["payload"], "summary": "Fixture", "greeting": "Fixture"})

		monkeypatch.setattr("boss_agent_cli.client_controller.draft_batch", complete_drafts)
		controller._run("prepare", {})
		assert controller.operation["status"] == "complete"
		assert controller.state()["browser"]["status"] == "blocked"
		batch = controller.batch(controller.operation["batch_id"])
		assert len(batch["items"]) == 10 and batch["stats"]["network_requests"] == 0
	finally:
		controller.close()
