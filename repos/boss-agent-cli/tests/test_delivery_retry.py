"""Bounded retries must preserve approvals and never replay uncertain writes."""

import hashlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from boss_agent_cli import delivery_retry as retry
from boss_agent_cli.delivery_store import DeliveryStore


@pytest.fixture
def batch(tmp_path):
	with DeliveryStore(tmp_path) as store:
		batch_id = store.create({})
		for number in (1, 2):
			job = {"job_id": f"job-{number}", "greeting": f"Approved text {number}", "company": f"Company {number}"}
			store.add(batch_id, number, job)
			store.save_draft(batch_id, number, job)
		store.review(batch_id, approve={1, 2}, reject=set())
		for number in (1, 2):
			store.claim(batch_id, number)
			store.finish(batch_id, number, "uncertain", "legacy acknowledgement")
	return tmp_path, batch_id


def authorize(store, batch_id, numbers=None):
	store.authorize_retry(
		"explicit-authorization",
		batch_id,
		numbers or {1},
		excluded_companies={"Company 2"},
		reason="User explicitly approved only Company 1",
	)


def receipt(job):
	return {
		"verification": "history_match",
		"message_id": "9007199254740993",
		"recipient_id": "22",
		"job_id": job["job_id"],
		"text_sha256": hashlib.sha256(job["greeting"].encode()).hexdigest(),
	}


def test_retry_scope_is_immutable_and_exclusions_are_enforced(batch):
	root, bid = batch
	with DeliveryStore(root) as store:
		authorize(store, bid)
		authorize(store, bid)
		with pytest.raises(ValueError, match="排除"):
			authorize(store, bid, {1, 2})
		with pytest.raises(ValueError, match="覆盖"):
			store.authorize_retry(
				"explicit-authorization", bid, {1, 2}, excluded_companies=set(), reason="Cannot expand"
			)
		with pytest.raises(ValueError, match="范围"):
			store.claim_retry("explicit-authorization", 2)


def test_claim_survives_restart_and_original_attempt_is_preserved(batch):
	root, bid = batch
	with DeliveryStore(root) as store:
		before = dict(store.db.execute("SELECT * FROM attempts WHERE number=1").fetchone())
		authorize(store, bid)
		assert store.claim_retry("explicit-authorization", 1)
	with DeliveryStore(root) as store:
		authorize(store, bid)
		assert store.claim_retry("explicit-authorization", 1) is None
		assert dict(store.db.execute("SELECT * FROM attempts WHERE number=1").fetchone()) == before
		store.finish_retry("explicit-authorization", 1, "uncertain")
		with pytest.raises(ValueError, match="改回"):
			store.finish_retry("explicit-authorization", 1, "not_submitted")
		assert store.claim_retry("explicit-authorization", 1) is None


def test_only_proven_not_submitted_can_be_prepared_again_and_is_bounded(batch):
	root, bid = batch
	with DeliveryStore(root) as store:
		authorize(store, bid)
		for _ in range(3):
			assert store.claim_retry("explicit-authorization", 1)
			store.finish_retry("explicit-authorization", 1, "not_submitted")
		assert store.claim_retry("explicit-authorization", 1) is None


def test_retry_verified_requires_matching_saved_receipt(batch):
	root, bid = batch
	with DeliveryStore(root) as store:
		authorize(store, bid)
		job = store.claim_retry("explicit-authorization", 1)
		with pytest.raises(ValueError, match="回执"):
			store.finish_retry("explicit-authorization", 1, "verified")
		store.finish(bid, 1, "sent", receipt=receipt(job))
		store.finish_retry("explicit-authorization", 1, "verified")
		assert store.claim_retry("explicit-authorization", 1) is None


def test_revoked_approval_cannot_be_claimed(batch):
	root, bid = batch
	with DeliveryStore(root) as store:
		authorize(store, bid)
		with store.db:
			store.db.execute("UPDATE items SET approved_hash='' WHERE number=1")
		with pytest.raises(ValueError, match="变化"):
			store.claim_retry("explicit-authorization", 1)


def fake_transport(monkeypatch, *, history_present=False, outcome=None, confirmed=True):
	state = {"present": history_present, "actions": []}
	bridge = Mock()
	bridge.status.return_value = {"extensionConnected": True, "extensionVersion": "1.2.3"}

	def command(action, **kwargs):
		state["actions"].append(action)
		if action == "inspect":
			return SimpleNamespace(ok=True, data={"path": "/web/geek/chat", "verificationVisible": False})
		if action == "send-chat":
			if isinstance(outcome, Exception):
				raise outcome
			state["present"] = confirmed
			return SimpleNamespace(ok=True, data=outcome or {"ok": True, "submitted": True, "stage": "submitted"})
		return SimpleNamespace(ok=True, data={"ok": True, "submitted": False})

	bridge.send_command.side_effect = command
	client = Mock()
	client.__enter__ = Mock(return_value=client)
	client.__exit__ = Mock(return_value=False)

	def read(method, url, **kwargs):
		assert method == "GET"
		if url == retry.endpoints.FRIEND_LIST_URL:
			return {
				"code": 0,
				"zpData": {
					"result": [
						{
							"uid": 22,
							"encryptJobId": "job-1",
							"securityId": "fixture",
							"brandName": "Company 1",
							"name": "HR",
						}
					]
				},
			}
		assert url == retry.endpoints.CHAT_HISTORY_URL
		messages = (
			[
				{
					"mid": "9007199254740993",
					"from": {"uid": 11},
					"to": {"uid": 22},
					"body": {"type": 1, "text": "Approved text 1"},
				}
			]
			if state["present"]
			else []
		)
		return {"code": 0, "zpData": {"messages": messages, "hasMore": False}}

	client._browser_request.side_effect = read
	monkeypatch.setattr(retry, "BridgeClient", Mock(return_value=bridge))
	monkeypatch.setattr(retry, "BossClient", Mock(return_value=client))
	monkeypatch.setattr(retry, "AuthManager", Mock())
	monkeypatch.setattr(retry, "write_preview", Mock())
	return state


def run(batch, mode="send"):
	root, bid = batch
	return retry.retry_batch(root, bid, "explicit-authorization", {1}, {1}, {"Company 2"}, mode=mode)


def test_existing_server_text_is_recorded_without_submission(batch, monkeypatch):
	state = fake_transport(monkeypatch, history_present=True)
	assert run(batch)[0] == {"number": 1, "company": "Company 1", "verified": True, "resent": False}
	assert state["actions"] == ["inspect"]


def test_permission_denied_before_retry_submission_preserves_original_uncertainty(batch, monkeypatch):
	from boss_agent_cli.bridge.client import BridgeRequestError
	from boss_agent_cli.bridge.protocol import BridgeResult
	error = BridgeRequestError(BridgeResult(id="fixture", ok=False, code="BROWSER_PAGE_ACCESS_DENIED", stage="before_injection", executed=False))
	state = fake_transport(monkeypatch, outcome=error)
	with pytest.raises(BridgeRequestError):
		run(batch)
	assert state["actions"].count("send-chat") == 1
	with DeliveryStore(batch[0]) as store:
		assert store.get(batch[1])["items"][0]["status"] == "uncertain"
		assert store.db.execute("SELECT phase FROM retry_attempts WHERE number=1").fetchone()[0] == "not_submitted"


def test_success_requires_history_and_repeat_never_sends_twice(batch, monkeypatch):
	state = fake_transport(monkeypatch)
	assert run(batch)[0]["resent"] is True
	assert run(batch)[0]["resent"] is False
	assert state["actions"].count("send-chat") == 1
	with DeliveryStore(batch[0]) as store:
		assert store.get(batch[1])["items"][0]["status"] == "sent"
		assert store.get(batch[1])["items"][1]["status"] == "uncertain"


@pytest.mark.parametrize("outcome", [None, TimeoutError("lost reply")])
def test_unconfirmed_write_is_uncertain_and_cannot_be_replayed(batch, monkeypatch, outcome):
	state = fake_transport(monkeypatch, outcome=outcome, confirmed=False)
	with pytest.raises((ValueError, TimeoutError)):
		run(batch)
	with pytest.raises(ValueError, match="不会重新预填"):
		run(batch)
	assert state["actions"].count("send-chat") == 1
	assert state["actions"].count("prepare-chat") == 1
	with DeliveryStore(batch[0]) as store:
		assert store.get(batch[1])["items"][0]["status"] == "uncertain"
		assert store.db.execute("SELECT count(*) FROM message_receipts").fetchone()[0] == 0


def test_prepare_never_submits(batch, monkeypatch):
	state = fake_transport(monkeypatch)
	assert run(batch, "prepare")[0]["submitted"] is False
	assert "send-chat" not in state["actions"]
	with DeliveryStore(batch[0]) as store:
		assert store.db.execute("SELECT count(*) FROM retry_attempts").fetchone()[0] == 0
