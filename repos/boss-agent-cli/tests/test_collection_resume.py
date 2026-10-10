"""Durable, manually resumed discovery; all platform and model calls are fixtures."""

import copy
import json
from collections import Counter
from unittest.mock import Mock

import httpx
import pytest

from boss_agent_cli.ai.service import AIServiceError
from boss_agent_cli.api.zhipin_errors import EnvironmentRiskError
from boss_agent_cli.bridge.client import BridgeClient, BridgeRequestError
from boss_agent_cli.bridge.protocol import BridgeResult
from boss_agent_cli.cache.store import CacheStore
from boss_agent_cli.delivery import COLLECTION_POLICY, DeliveryError, collect_batch, draft_batch, public_batch, send_batch
from boss_agent_cli.delivery_store import DeliveryStore
from client_fixture import fixture_client
from test_collection_policy import clock  # noqa: F401
from test_delivery import FakeAI, FakePlatform
from test_delivery_quotas_history import candidate, params, platform_for


def options():
	p = params({"recruitment_quotas": {"campus": 5, "internship": 5}, "history_dedupe": "job"})
	p.update(collection_policy=dict(COLLECTION_POLICY), completion_first=True, continue_after_cooldown=True, resume="test")
	return p


def candidates():
	return platform_for([candidate(i) for i in range(1, 6)], [candidate(i, "internship") for i in range(6, 11)])


def test_restart_resumes_failed_jd_and_preserves_reviews_and_ambiguous_send(tmp_path, clock):  # noqa: F811
	platform = candidates()
	read = platform.job_card_browser

	def fail_fourth(security_id, lid=""):
		if security_id == "sec-4":
			return {"code": 37, "message": "您的环境存在异常."}
		return read(security_id, lid)

	platform.job_card_browser = Mock(side_effect=fail_fourth)
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		bid = store.create(options())
		with pytest.raises(EnvironmentRiskError):
			collect_batch(store, bid, platform, cache, Mock())
		batch = store.get(bid)
		assert len(batch["items"]) == 3
		assert batch["checkpoint"]["queue"][0][:3] == ["campus", "北京", 1]
		assert "job-4" not in batch["checkpoint"]["seen"]
		assert batch["stats"]["read_retries"] == 0
		assert platform.job_card_browser.call_count == 4
		draft_batch(store, bid, FakeAI(), "Python项目经历", Mock(), workers=3)
		store.review(bid, approve={1, 3}, reject={2})
		sender = FakePlatform()
		sender.raise_on_send = True
		with pytest.raises(TimeoutError):
			send_batch(store, bid, sender, cache, limit=1, interval=0, allowed_numbers={3})
		before = store.get(bid)["items"]
		attempts = [dict(row) for row in store.db.execute("SELECT * FROM attempts")]
		assert before[2]["status"] == "uncertain"

	clock.now += 901
	platform.job_card_browser = Mock(side_effect=read)
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		collect_batch(store, bid, platform, cache, Mock())
		batch = store.get(bid)
		assert len(batch["items"]) == 10 and batch["stop_reason"] == "target_reached"
		assert batch["items"][:3] == before
		assert batch["stats"]["recruitment_counts"] == {"campus": 5, "internship": 5}
		assert batch["stats"]["resume_count"] == 1
		assert platform.details == [f"job-{i}" for i in range(1, 11)]
		assert [dict(row) for row in store.db.execute("SELECT * FROM attempts")] == attempts
		assert len(sender.sent) == 1 and not platform.sent
		assert not public_batch(batch)["collection_resume"]["available"]
		assert "checkpoint" not in public_batch(batch)
		assert "sec-" not in json.dumps(public_batch(batch))


def test_search_failure_keeps_next_page_and_does_not_repeat_completed_page(tmp_path, clock):  # noqa: F811
	platform = candidates()
	p = options()
	p["searches"] = [{"query": "campus", "city": "北京", "kind": "campus"}]

	def search(query, *, page, **kwargs):
		if page == 2:
			return {"code": 37, "message": "您的环境存在异常."}
		return {"code": 0, "zpData": {"jobList": [candidate(1)], "hasMore": True}}

	platform.search_jobs.side_effect = search
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		bid = store.create(p)
		with pytest.raises(EnvironmentRiskError):
			collect_batch(store, bid, platform, cache, Mock())
		assert store.get(bid)["checkpoint"]["queue"][0][2] == 2
	clock.now += 901
	platform.search_jobs.reset_mock()
	platform.search_jobs.side_effect = lambda *a, **k: {"code": 0, "zpData": {"jobList": [candidate(2)], "hasMore": False}}
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		collect_batch(store, bid, platform, cache, Mock())
		assert [c.kwargs["page"] for c in platform.search_jobs.call_args_list] == [2]
		assert [i["job_id"] for i in store.get(bid)["items"]] == ["job-1", "job-2"]
		assert not public_batch(store.get(bid))["collection_resume"]["available"]


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("fixture"), httpx.ConnectError("fixture")])
def test_transport_retries_are_bounded_and_consume_the_same_persistent_budget(tmp_path, clock, failure):  # noqa: F811
	platform = candidates()
	platform.search_jobs.side_effect = failure
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		bid = store.create(options())
		with pytest.raises(type(failure)):
			collect_batch(store, bid, platform, cache, Mock())
		batch = store.get(bid)
		assert platform.search_jobs.call_count == 3
		assert batch["stats"]["read_retries"] == 2
		assert batch["stats"]["network_requests"] == 3
		assert cache.discovery_budget(limit=20, window_seconds=600)["count"] == 3
		assert clock.now >= 1024
		assert public_batch(batch)["collection_resume"]["available"]


def test_transport_recovery_completes_same_batch_and_cancel_stops_retry(tmp_path, clock):  # noqa: F811
	platform = candidates()
	search = platform.search_jobs.side_effect
	first = True

	def flaky(*args, **kwargs):
		nonlocal first
		if first:
			first = False
			raise httpx.ConnectError("fixture")
		return search(*args, **kwargs)

	platform.search_jobs.side_effect = flaky
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		bid = store.create(options())
		collect_batch(store, bid, platform, cache, Mock())
		assert len(store.get(bid)["items"]) == 10
		assert store.get(bid)["stats"]["read_retries"] == 1
		assert len(store.list_batches()) == 1 and not platform.sent
		clock.now += 901
		other = store.create(options())
		platform.search_jobs.reset_mock()
		platform.search_jobs.side_effect = httpx.ReadTimeout("fixture")
		with pytest.raises(DeliveryError, match="已停止"):
			collect_batch(store, other, platform, cache, Mock(), should_stop=lambda: platform.search_jobs.call_count > 0)
		assert platform.search_jobs.call_count == 1


@pytest.mark.parametrize("failure", [
	{"code": 37, "message": "您的环境存在异常."},
	{"code": 38, "message": "访问受限"},
	{"code": 0, "zpData": []},
	{"code": 0, "zpData": {"jobList": None}},
])
def test_platform_denial_and_schema_errors_never_retry(tmp_path, clock, failure):  # noqa: F811
	platform = candidates()
	platform.search_jobs.side_effect = None
	platform.search_jobs.return_value = failure
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		bid = store.create(options())
		with pytest.raises((DeliveryError, EnvironmentRiskError)):
			collect_batch(store, bid, platform, cache, Mock())
		assert platform.search_jobs.call_count == 1
		assert store.get(bid)["stats"]["read_retries"] == 0


def test_bridge_transport_tag_applies_to_get_not_post_or_permissions(monkeypatch):
	bridge = BridgeClient()
	post = Mock(side_effect=httpx.ReadTimeout("fixture"))
	monkeypatch.setattr("boss_agent_cli.bridge.client.httpx.post", post)
	for method, expected in [("GET", True), ("POST", False)]:
		with pytest.raises(BridgeRequestError) as error:
			bridge.fetch_json("https://www.zhipin.com/wapi/zpgeek/friend/add.json", method=method)
		assert error.value.retryable_read is expected
	assert post.call_count == 2
	assert not BridgeRequestError(BridgeResult("test", False, code="BROWSER_PAGE_ACCESS_DENIED"), read_only=True).retryable_read


def test_resume_keeps_batch_snapshot_instead_of_current_settings(tmp_path, monkeypatch, clock):  # noqa: F811
	controller, old = fixture_client(tmp_path)
	p = options()
	platform = candidates()
	try:
		with DeliveryStore(tmp_path) as store:
			bid = store.create(p)
		controller._set_browser_state("ready", "Fixture only")
		controller._wait_collection_start = Mock()
		controller._check_chrome = Mock()
		controller._service_and_resume = Mock(return_value=(FakeAI(), "Python项目经历"))
		controller.settings = Mock(side_effect=AssertionError("Resume must not read changed settings"))
		monkeypatch.setattr("boss_agent_cli.client_controller.BossClient", Mock(return_value=FakePlatform()))
		monkeypatch.setattr("boss_agent_cli.client_controller.BossPlatform", Mock(return_value=platform))
		controller._run("resume", {"batch_id": bid})
		assert controller.operation["status"] == "complete"
		assert controller.operation["batch_id"] == bid
		controller._service_and_resume.assert_called_once_with(p)
		with DeliveryStore(tmp_path) as store:
			assert len(store.list_batches()) == 2 and len(store.get(bid)["items"]) == 10
			assert store.get(bid)["params"] == p
		assert any("旧批次没有搜索断点" in log["message"] for log in controller.operation["logs"])
		assert not platform.sent
	finally:
		controller.close()


@pytest.mark.parametrize("remaining", [0, 1000])
def test_resume_cannot_clear_platform_pause_even_after_local_timer_expires(tmp_path, monkeypatch, remaining):
	controller, bid = fixture_client(tmp_path)
	try:
		import time
		controller._set_browser_state("blocked", "Fixture code37", blocked_until=time.time() + remaining)
		client = Mock(side_effect=AssertionError("No platform calls"))
		monkeypatch.setattr("boss_agent_cli.client_controller.BossClient", client)
		with pytest.raises(ValueError, match="手动|冷却"):
			controller.start("resume", {"batch_id": bid})
		assert controller.state()["browser"]["status"] == "blocked"
		client.assert_not_called()
	finally:
		controller.close()


def test_resume_preserves_approved_or_rejected_items_after_process_crash(tmp_path, clock):  # noqa: F811
	platform = candidates()
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		bid = store.create(options())
		original_add = store.add
		stale_checkpoint = None
		def crash_after_commit(batch_id, number, payload):
			nonlocal stale_checkpoint
			stale_checkpoint = store.get(batch_id)
			original_add(batch_id, number, payload)
			raise SystemExit("Simulated process termination after durable item insert")
		store.add = crash_after_commit
		with pytest.raises(SystemExit):
			collect_batch(store, bid, platform, cache, Mock())
		# A hard exit does not run finally: restore the last checkpoint from before the insert.
		store.collection_progress(bid, stale_checkpoint["checkpoint"], stale_checkpoint["stats"], "collecting")
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		collect_batch(store, bid, platform, cache, Mock())
		assert len(store.get(bid)["items"]) == 10
		assert platform.details.count("job-1") == 1


def test_mismatched_jd_does_not_retry_as_network_error(tmp_path, clock):  # noqa: F811
	platform = candidates()
	platform.job_card_browser = Mock(return_value={"code": 0, "zpData": {"jobCard": {"encryptJobId": "another-job", "postDescription": "JD"}}})
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
		bid = store.create(options())
		with pytest.raises(DeliveryError, match="标识与列表不一致"):
			collect_batch(store, bid, platform, cache, Mock())
		assert platform.job_card_browser.call_count == 1
		assert store.get(bid)["items"] == []


def test_failed_model_items_retry_alone_and_survivors_stay_immutable(tmp_path, clock):  # noqa: F811
	calls = Counter()
	fail = True
	class FlakyAI(FakeAI):
		def chat(self, messages, **kwargs):
			number = json.loads(messages[1]["content"])["title"]
			calls[number] += 1
			if number == "2" and fail:
				raise AIServiceError("fixture transient", retryable=True)
			return super().chat(messages, **kwargs)
	with DeliveryStore(tmp_path) as store:
		bid = store.create(options())
		for n in range(1, 4):
			store.add(bid, n, {"job_id": f"job-{n}", "title": str(n), "company": "Fixture", "description": "AI应用开发"})
		draft_batch(store, bid, FlakyAI(), "Python项目经历", Mock(), workers=3)
		assert calls == {"1": 1, "2": 2, "3": 1}
		store.review(bid, approve={1}, reject=set())
		before = copy.deepcopy(store.get(bid)["items"])
		assert before[1]["status"] == "draft"
		assert [e["number"] for e in store.get(bid)["stats"]["draft_errors"]] == [2]
		fail = False
		draft_batch(store, bid, FlakyAI(), "Python项目经历", Mock(), workers=3)
		after = store.get(bid)
		assert calls == {"1": 1, "2": 3, "3": 1}
		assert after["items"][0] == before[0] and after["items"][2] == before[2]
		assert after["stats"]["draft_errors"] == [] and after["stats"]["drafts_pending"] == 0


def test_draft_operation_reports_partial_without_platform_access(tmp_path, monkeypatch):
	controller, bid = fixture_client(tmp_path)
	try:
		with DeliveryStore(tmp_path) as store:
			store.add(bid, 3, {"job_id": "j3", "title": "3", "company": "Fixture", "city": "北京", "salary": "20-30K", "description": "JD"})
		service = Mock()
		service.chat.side_effect = AIServiceError("fixture authorization error", status_code=401)
		controller._service_and_resume = Mock(return_value=(service, "Python项目经历"))
		monkeypatch.setattr("boss_agent_cli.client_controller.BossClient", Mock(side_effect=AssertionError("No BOSS access")))
		controller._run("draft", {"batch_id": bid})
		assert controller.operation["status"] == "partial"
		assert service.chat.call_count == 1
		assert controller.batch(bid)["counts"] == {"pending": 2, "draft": 1}
	finally:
		controller.close()
