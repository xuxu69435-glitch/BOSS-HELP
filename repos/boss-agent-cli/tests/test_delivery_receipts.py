"""A successful contact request is never proof of message delivery."""

import hashlib
from unittest.mock import Mock

import pytest

from boss_agent_cli.api.client import BossClient, GreetingDeliveryUnverified
from boss_agent_cli.cache.store import CacheStore
from boss_agent_cli.delivery import DeliveryError, send_batch
from boss_agent_cli.delivery_store import DeliveryStore
from boss_agent_cli.platforms.zhipin import BossPlatform
from client_fixture import fixture_client
from test_delivery import FakePlatform


@pytest.fixture
def approved(tmp_path):
	controller, batch_id = fixture_client(tmp_path)
	with DeliveryStore(tmp_path) as store:
		store.review(batch_id, approve={1, 2}, reject=set())
	yield controller, batch_id
	controller.close()


def receipt_for(job, **overrides):
	return {
		"verification": "history_match",
		"message_id": "fixture-receipt",
		"recipient_id": "fixture-person",
		"job_id": job["job_id"],
		"text_sha256": hashlib.sha256(job["greeting"].encode("utf-8")).hexdigest(),
		**overrides,
	}


def test_real_boss_greet_is_blocked_before_network():
	with BossClient(Mock(), browser_source="existing-browser") as client:
		client._browser_request = Mock(side_effect=AssertionError("No contact-only write"))
		with pytest.raises(GreetingDeliveryUnverified):
			client.greet("fixture-security", "fixture-job", "approved text")
		client._browser_request.assert_not_called()


def test_unsupported_sender_is_blocked_before_claim(approved):
	controller, batch_id = approved
	client = Mock()
	with DeliveryStore(controller.data_dir) as store, CacheStore(controller.data_dir / "test-cache.db") as cache:
		with pytest.raises(DeliveryError, match="自动发送已暂停"):
			send_batch(store, batch_id, BossPlatform(client), cache, limit=2, interval=0)
		assert not store.attempted("fixture-job-1")
		assert store.get(batch_id)["items"][0]["status"] == "approved"
	client.greet.assert_not_called()


def test_code_zero_without_receipt_is_uncertain_and_stops_batch(approved):
	controller, batch_id = approved
	platform = FakePlatform()
	platform.greet = Mock(return_value={"code": 0, "zpData": {"friendId": "fixture-person"}})
	with DeliveryStore(controller.data_dir) as store, CacheStore(controller.data_dir / "test-cache.db") as cache:
		with pytest.raises(ValueError, match="消息回执"):
			send_batch(store, batch_id, platform, cache, limit=2, interval=0)
		assert platform.greet.call_count == 1
		assert [item["status"] for item in store.get(batch_id)["items"]] == ["uncertain", "approved"]
		assert store.attempted("fixture-job-1") and not cache.is_contacted_job("fixture-job-1")
		assert send_batch(store, batch_id, platform, cache, limit=1, interval=0, allowed_numbers={1}) == 0
		assert platform.greet.call_count == 1


@pytest.mark.parametrize(
	"change",
	[
		{"job_id": "wrong"},
		{"text_sha256": "wrong"},
		{"message_id": ""},
		{"recipient_id": ""},
		{"verification": "http_ok"},
	],
)
def test_receipt_must_identify_the_exact_job_and_text(approved, change):
	controller, batch_id = approved
	with DeliveryStore(controller.data_dir) as store:
		job = store.claim(batch_id, 1)
		with pytest.raises(ValueError, match="消息回执"):
			store.finish(batch_id, 1, "sent", receipt=receipt_for(job, **change))
		assert store.get(batch_id)["items"][0]["status"] == "sending"
		assert store.db.execute("SELECT count(*) FROM message_receipts").fetchone()[0] == 0


def test_matching_history_receipt_is_saved_atomically(approved):
	controller, batch_id = approved
	with DeliveryStore(controller.data_dir) as store:
		job = store.claim(batch_id, 1)
		store.finish(batch_id, 1, "sent", receipt=receipt_for(job))
		assert store.get(batch_id)["items"][0]["status"] == "sent"
		assert store.db.execute("SELECT count(*) FROM message_receipts").fetchone()[0] == 1
		with pytest.raises(ValueError, match="已核实消息回执"):
			store.correct_unverified_sent(batch_id, {1}, "should not overwrite verified history")


def test_correct_legacy_success_keeps_payload_hash_and_attempt(approved):
	controller, batch_id = approved
	with DeliveryStore(controller.data_dir) as store:
		store.claim(batch_id, 1)
		with store.db:
			store.db.execute("UPDATE items SET status='sent' WHERE batch_id=? AND number=1", (batch_id,))
			store.db.execute("UPDATE attempts SET status='sent' WHERE batch_id=? AND number=1", (batch_id,))
		before = store.get(batch_id)["items"][0]
		assert store.correct_unverified_sent(batch_id, {1}, "Legacy contact-only acknowledgement") == 1
		after = store.get(batch_id)["items"][0]
		assert after["status"] == "uncertain"
		assert after["payload"] == before["payload"] and after["approved_hash"] == before["approved_hash"]
		assert store.attempted("fixture-job-1") and store.claim(batch_id, 1) is None
		assert store.db.execute("SELECT previous_status,new_status FROM status_corrections").fetchone()[:] == (
			"sent",
			"uncertain",
		)
