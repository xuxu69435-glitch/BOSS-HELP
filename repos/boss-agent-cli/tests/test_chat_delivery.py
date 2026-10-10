"""The regular approval queue must verify actual outgoing messages as well."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from boss_agent_cli import chat_delivery as delivery
from boss_agent_cli.api import endpoints
from boss_agent_cli.api.zhipin_errors import EnvironmentRiskError
from boss_agent_cli.cache.store import CacheStore
from boss_agent_cli.delivery import send_batch
from boss_agent_cli.delivery_store import DeliveryStore
from client_fixture import fixture_client


@pytest.fixture
def setup(monkeypatch):
	state = {
		"actions": [],
		"created": True,
		"present": False,
		"verify": True,
		"requests": [],
		"path": "/web/geek/chat",
		"version": "1.2.3",
		"document_stamp": 1000,
		"risk": False,
		"history_more": False,
	}
	client = Mock()
	client._browser_source = "existing-browser"

	def request(method, url, **kwargs):
		state["requests"].append((method, url))
		if url == endpoints.FRIEND_LIST_URL:
			friends = (
				[
					{
						"uid": 22,
						"encryptJobId": "fixture-job-1",
						"securityId": "fixture",
						"brandName": "Company",
						"name": "HR",
					}
				]
				if state["created"]
				else []
			)
			return {"code": 0, "zpData": {"result": friends}}
		if url == endpoints.GREET_URL:
			assert "greeting" not in kwargs["data"]
			state["created"] = True
			return {"code": 0, "zpData": {"friendId": 22}}
		assert method == "GET" and url == endpoints.CHAT_HISTORY_URL
		messages = (
			[
				{
					"mid": "9007199254740993",
					"from": {"uid": 11},
					"to": {"uid": 22},
					"body": {"type": 1, "text": state["text"]},
				}
			]
			if state["present"]
			else []
		)
		return {"code": 0, "zpData": {"messages": messages, "hasMore": state["history_more"]}}

	client._browser_request.side_effect = request
	bridge = Mock()
	bridge.status.side_effect = lambda: {"extensionConnected": True, "extensionVersion": state["version"]}

	def command(action, **kwargs):
		state["actions"].append(action)
		if state.get("fail_action") == action:
			return SimpleNamespace(
				ok=True,
				data={
					"ok": False,
					"submitted": False,
					"stage": "before_submit",
					"error": "The target contact cannot be identified uniquely",
				},
			)
		if action == "select-chat" and state.get("contact_missing"):
			return SimpleNamespace(
				ok=True,
				data={
					"ok": False,
					"submitted": False,
					"stage": "before_submit",
					"code": "CONTACT_NOT_LOADED",
					"error": "The target contact cannot be identified uniquely",
				},
			)
		if action == "refresh-chat":
			state["contact_missing"] = False
			previous = state["document_stamp"]
			state["document_stamp"] += 1
			return SimpleNamespace(ok=True, data={"ok": True, "submitted": False, "stage": "refreshed", "previousDocumentStamp": previous})
		if action == "inspect":
			return SimpleNamespace(
				ok=True, data={"path": state["path"], "verificationVisible": state["risk"], "chatReady": True, "documentStamp": state["document_stamp"]}
			)
		if action == "send-chat":
			state["text"] = kwargs["data"]["text"]
			state["present"] = state["verify"]
			return SimpleNamespace(ok=True, data={"ok": True, "submitted": True})
		return SimpleNamespace(ok=True, data={"ok": True, "submitted": False})

	bridge.send_command.side_effect = command
	monkeypatch.setattr(delivery, "BridgeClient", Mock(return_value=bridge))
	return delivery.VerifiedChatPlatform(client), state


def test_normal_sender_returns_only_server_receipt(setup):
	platform, state = setup
	result = platform.greet("sid", "fixture-job-1", "Approved text")
	assert result["zpData"]["greeting_receipt"]["message_id"] == "9007199254740993"
	assert state["actions"].count("send-chat") == 1


def test_missing_contact_refreshes_only_once_then_verifies_the_text(setup):
	platform, state = setup
	state["contact_missing"] = True
	assert platform.greet("sid", "fixture-job-1", "Approved text")["zpData"]["greeting_receipt"]
	assert state["actions"].count("refresh-chat") == 1
	assert state["actions"].count("select-chat") == 2
	assert state["actions"].count("send-chat") == 1
	assert all(method == "GET" for method, _ in state["requests"])
	platform.greet("sid", "fixture-job-1", "Approved text")
	assert state["actions"].count("send-chat") == 1


def test_new_contact_acknowledgement_without_text_never_reports_success(setup):
	platform, state = setup
	state.update(created=False, verify=False)
	with pytest.raises(delivery.ChatDeliveryError, match="未找到原文"):
		platform.greet("sid", "fixture-job-1", "Approved text")
	assert state["requests"].count(("POST", endpoints.GREET_URL)) == 1
	assert state["actions"].count("send-chat") == 1


def test_new_contact_then_verified_text(setup):
	platform, state = setup
	state["created"] = False
	assert platform.greet("sid", "fixture-job-1", "Approved text")["zpData"]["greeting_receipt"]
	assert state["requests"].count(("POST", endpoints.GREET_URL)) == 1


def test_create_contact_denied_before_injection_is_not_sent_not_uncertain(setup):
	from boss_agent_cli.bridge.client import BridgeRequestError
	from boss_agent_cli.bridge.protocol import BridgeResult
	platform, state = setup
	state["created"] = False
	original = platform._client._browser_request.side_effect
	def request(method, url, **kwargs):
		if method == "POST":
			raise BridgeRequestError(BridgeResult(id="fixture", ok=False, code="BROWSER_PAGE_ACCESS_DENIED", stage="before_injection", executed=False))
		return original(method, url, **kwargs)
	platform._client._browser_request.side_effect = request
	with pytest.raises(BridgeRequestError) as raised:
		platform.greet("sid", "fixture-job-1", "Approved text")
	assert raised.value.delivery_submitted is False
	assert raised.value.delivery_stage == "create_contact"
	assert "send-chat" not in state["actions"]


def test_unknown_create_contact_timeout_keeps_uncertain_outcome(setup):
	platform, state = setup
	state["created"] = False
	original = platform._client._browser_request.side_effect
	def request(method, url, **kwargs):
		if method == "POST":
			raise TimeoutError("Unknown result")
		return original(method, url, **kwargs)
	platform._client._browser_request.side_effect = request
	with pytest.raises(TimeoutError) as raised:
		platform.greet("sid", "fixture-job-1", "Approved text")
	assert raised.value.delivery_submitted is None
	assert "send-chat" not in state["actions"]


@pytest.mark.parametrize(
	"change", [{"path": "/web/geek/jobs"}, {"version": "1.2.0"}, {"version": "1.2.1"}, {"version": "1.2.2"}, {"risk": True}]
)
def test_wrong_page_old_extension_and_verification_stop_before_requests(setup, change):
	platform, state = setup
	state.update(change)
	with pytest.raises((delivery.ChatDeliveryError, EnvironmentRiskError)):
		platform.greet("sid", "fixture-job-1", "Approved text")
	assert state["requests"] == []
	assert "send-chat" not in state["actions"]


def test_incomplete_history_cannot_trigger_resend(setup):
	platform, state = setup
	state["history_more"] = True
	with pytest.raises(delivery.ChatDeliveryError, match="缺失"):
		platform.greet("sid", "fixture-job-1", "Approved text")
	assert "prepare-chat" not in state["actions"]


def test_approval_queue_preserves_attempt_after_unconfirmed_write(tmp_path, setup):
	platform, state = setup
	state["verify"] = False
	controller, batch_id = fixture_client(tmp_path)
	try:
		with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
			store.review(batch_id, approve={1}, reject=set())
			with pytest.raises(delivery.ChatDeliveryError):
				send_batch(store, batch_id, platform, cache, limit=1, interval=0)
			assert store.get(batch_id)["items"][0]["status"] == "uncertain"
			assert not cache.is_contacted_job("fixture-job-1")
			assert send_batch(store, batch_id, platform, cache, limit=1, interval=0) == 0
			assert state["actions"].count("send-chat") == 1
	finally:
		controller.close()


def test_missing_chat_page_does_not_consume_approval(tmp_path, setup):
	platform, state = setup
	state["path"] = "/web/geek/jobs"
	controller, batch_id = fixture_client(tmp_path)
	try:
		with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
			store.review(batch_id, approve={1}, reject=set())
			with pytest.raises(delivery.ChatDeliveryError):
				send_batch(store, batch_id, platform, cache, limit=1, interval=0)
			assert not store.attempted("fixture-job-1")
			assert store.get(batch_id)["items"][0]["status"] == "approved"
	finally:
		controller.close()


def test_proven_before_submit_failure_is_retryable_without_deleting_attempts(tmp_path, setup):
	platform, state = setup
	state["fail_action"] = "select-chat"
	controller, bid = fixture_client(tmp_path)
	try:
		with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache.db") as cache:
			store.review(bid, approve={1}, reject=set())
			before = store.get(bid)["items"][0]
			with pytest.raises(delivery.ChatDeliveryError):
				send_batch(store, bid, platform, cache, limit=1, interval=0)
			after = store.get(bid)["items"][0]
			assert after["status"] == "not_sent" and "唯一定位" in after["error"]
			assert after["approved_hash"] == before["approved_hash"] and after["payload"] == before["payload"]
			assert store.attempted("fixture-job-1")
			assert "send-chat" not in state["actions"]
			state.pop("fail_action")
			assert send_batch(store, bid, platform, cache, limit=1, interval=0) == 1
			assert state["actions"].count("send-chat") == 1
			assert (
				store.db.execute(
					"SELECT COUNT(*) FROM delivery_events WHERE batch_id=? AND number=1", (bid,)
				).fetchone()[0]
				== 4
			)
	finally:
		controller.close()
