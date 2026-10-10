"""Loopback security, review integrity, and controller behavior."""

import threading
from unittest.mock import Mock

import httpx
import pytest

from boss_agent_cli.delivery import send_batch
from boss_agent_cli.delivery_store import DeliveryStore
from boss_agent_cli.cache.store import CacheStore
from boss_agent_cli.local_client import ClientServer
from client_fixture import fixture_client
from test_delivery import FakePlatform


@pytest.fixture
def client(tmp_path):
	controller, batch_id = fixture_client(tmp_path)
	yield controller, batch_id
	controller.close()


@pytest.fixture
def http_client(client):
	controller, batch_id = client
	server = ClientServer(("127.0.0.1", 0), controller)
	thread = threading.Thread(target=server.serve_forever, daemon=True)
	thread.start()
	with httpx.Client(base_url=server.origin, trust_env=False, headers={"X-Client-Token": server.token}) as browser:
		yield browser, server, batch_id
	server.shutdown()
	server.server_close()
	thread.join(timeout=3)


def request_headers(server):
	return {"Origin": server.origin, "X-Client-Token": server.token, "Content-Type": "application/json"}


def approval_body(controller, batch_id):
	batch = controller.batch(batch_id)
	return {
		"batch_id": batch_id,
		"approve": [1],
		"reject": [],
		"expected_updates": {str(i["number"]): i["updated_at"] for i in batch["items"]},
	}


def test_loading_page_and_state_has_no_external_actions(http_client):
	browser, server, batch_id = http_client
	server.controller.start = Mock(side_effect=AssertionError("No actions on GET"))
	assert browser.get("/").status_code == 200
	assert server.token not in browser.get("/").text
	result = browser.get("/api/state")
	assert result.status_code == 200
	assert result.json()["browser"]["status"] == "needs_connection"
	assert result.json()["browser"]["mode"] == "existing-browser"
	assert browser.get("/api/login").status_code == 404
	data = browser.get("/api/batch", params={"id": batch_id}).text
	assert "test-private-security" not in data and "test-private-context" not in data
	server.controller.start.assert_not_called()


def test_connect_and_legacy_login_only_request_existing_chrome(http_client):
	browser, server, _ = http_client
	server.controller.start = Mock()
	for path in ("/api/connect", "/api/login"):
		assert browser.post(path, json={}, headers=request_headers(server)).status_code == 200
		server.controller.start.assert_called_with("connect", {})
	assert browser.post("/api/confirm-login", json={}, headers=request_headers(server)).status_code == 404


def test_resume_requires_same_origin_capability_and_explicit_batch(http_client):
	browser, server, bid = http_client
	server.controller.start = Mock()
	assert browser.post("/api/resume", json={"batch_id": bid}).status_code == 403
	server.controller.start.assert_not_called()
	assert browser.post("/api/resume", json={"batch_id": bid}, headers=request_headers(server)).status_code == 200
	server.controller.start.assert_called_once_with("resume", {"batch_id": bid})


def test_profile_api_is_authenticated_and_saving_is_local(http_client, monkeypatch):
	browser, server, bid = http_client
	monkeypatch.setattr("boss_agent_cli.client_controller._create_ai_service", Mock(side_effect=AssertionError("No model call")))
	monkeypatch.setattr("boss_agent_cli.client_controller.BossClient", Mock(side_effect=AssertionError("No platform call")))
	assert browser.get("/api/profile", headers={"X-Client-Token": "wrong"}).status_code == 403
	assert browser.get("/api/profile", headers={"Origin": "https://evil.test"}).status_code == 403
	profile = browser.get("/api/profile").raise_for_status().json()
	profile["data"]["title"] = "本机资料测试"
	body = {"expected_version": profile["version"], "data": profile["data"]}
	assert browser.post("/api/profile", json=body).status_code == 403
	response = browser.post("/api/profile", json=body, headers=request_headers(server))
	assert response.status_code == 200 and response.json()["data"]["title"] == "本机资料测试"
	assert browser.post("/api/profile", json=body, headers=request_headers(server)).status_code == 400
	assert "data" not in server.controller.state().get("profile", {})
	with DeliveryStore(server.controller.data_dir) as store:
		assert not store.db.execute("SELECT 1 FROM attempts").fetchone()


def test_manual_recheck_api_requires_capability_and_confirmation(http_client):
	browser, server, _ = http_client
	server.controller.start = Mock()
	assert browser.post("/api/check_now", json={"confirm_once": True}).status_code == 403
	server.controller.start.assert_not_called()
	assert browser.post("/api/check_now", json={"confirm_once": True, "expected_pause": "fixture"}, headers=request_headers(server)).status_code == 200
	server.controller.start.assert_called_once_with("check_now", {"confirm_once": True, "expected_pause": "fixture"})


@pytest.mark.parametrize(
	"headers", [{}, {"Origin": "https://evil.test"}, {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"}]
)
def test_csrf_attempts_are_rejected(http_client, headers):
	browser, server, batch_id = http_client
	server.controller.start = Mock()
	assert browser.post("/api/login", json={}, headers=headers).status_code == 403
	server.controller.start.assert_not_called()


def test_dns_rebinding_and_path_traversal_are_rejected(http_client):
	browser, _, _ = http_client
	assert browser.get("/api/state", headers={"Host": "evil.test"}).status_code == 403
	assert browser.get("/api/state", headers={"Origin": "https://evil.test"}).status_code == 403
	assert browser.get("/assets/%2e%2e/preferences.json").status_code == 404


def test_review_is_local_and_edit_invalidates_approval(http_client):
	browser, server, batch_id = http_client
	controller = server.controller
	response = browser.post("/api/review", json=approval_body(controller, batch_id), headers=request_headers(server))
	assert response.status_code == 200
	item = controller.batch(batch_id)["items"][0]
	assert item["status"] == "approved"
	response = browser.post(
		"/api/edit",
		json={"batch_id": batch_id, "number": 1, "greeting": "修改后的招呼", "expected_update": item["updated_at"]},
		headers=request_headers(server),
	)
	assert response.status_code == 200
	with DeliveryStore(controller.data_dir) as store:
		item = store.get(batch_id)["items"][0]
		assert item["status"] == "pending" and item["approved_hash"] == ""
		assert not store.attempted("fixture-job-1")


def test_stale_approval_cannot_silently_approve_new_text(client):
	controller, batch_id = client
	body = approval_body(controller, batch_id)
	item = controller.batch(batch_id)["items"][0]
	controller.edit({"batch_id": batch_id, "number": 1, "greeting": "New text", "expected_update": item["updated_at"]})
	with pytest.raises(ValueError, match="已更新"):
		controller.review(body)


def test_send_requires_explicit_matching_recipient_list(client):
	controller, batch_id = client
	controller.review(approval_body(controller, batch_id))
	with pytest.raises(ValueError, match="发送清单已变化"):
		controller.start("send", {"batch_id": batch_id, "limit": 1})
	assert controller.operation["status"] == "idle"


def test_unverified_message_transport_is_blocked_before_work_starts(client, monkeypatch):
	from boss_agent_cli.chat_delivery import VerifiedChatPlatform

	monkeypatch.setattr(VerifiedChatPlatform, "verified_greeting_delivery", False)
	controller, batch_id = client
	controller.review(approval_body(controller, batch_id))
	with pytest.raises(ValueError, match="正文发送尚未可靠验证"):
		controller.start("send", {"batch_id": batch_id, "limit": 1, "approved_numbers": [1]})
	assert controller.operation["status"] == "idle"
	assert controller.state()["delivery"]["enabled"] is False
	with DeliveryStore(controller.data_dir) as store:
		assert not store.attempted("fixture-job-1")
		assert store.get(batch_id)["items"][0]["status"] == "approved"


def test_verified_transport_still_requires_explicit_approved_list(client):
	controller, batch_id = client
	controller.review(approval_body(controller, batch_id))
	controller._network_operation = Mock()
	controller.start("send", {"batch_id": batch_id, "limit": 1, "approved_numbers": [1]})
	controller._executor.shutdown(wait=True)
	assert controller.state()["delivery"]["enabled"] is True
	controller._network_operation.assert_called_once_with(
		"send", {"batch_id": batch_id, "limit": 1, "approved_numbers": [1], "allowed_numbers": {1}}
	)


def test_send_uses_all_eight_approved_ids_without_a_quantity_input(client):
	controller, bid = client
	with DeliveryStore(controller.data_dir) as store:
		base = store.get(bid)["items"][0]["payload"]
		for n in range(3, 11):
			job = {**base, "job_id": f"fixture-{n}", "security_id": f"fixture-sec-{n}"}
			store.add(bid, n, job)
			store.save_draft(bid, n, job)
		chosen = [1, 2, 3, 5, 6, 7, 8, 9]
		store.review(bid, approve=set(chosen), reject=set())
		versions = {str(i["number"]): i["updated_at"] for i in store.get(bid)["items"] if i["number"] in chosen}
	controller._network_operation = Mock()
	controller.start("send", {"batch_id": bid, "approved_numbers": chosen, "expected_updates": versions})
	controller._executor.shutdown(wait=True)
	body = controller._network_operation.call_args.args[1]
	assert body["limit"] == 8 and body["allowed_numbers"] == set(chosen)
	assert 4 not in body["allowed_numbers"] and 10 not in body["allowed_numbers"]


def test_stale_send_snapshot_does_not_send_newly_approved_text(client):
	controller, bid = client
	controller.review(approval_body(controller, bid))
	item = controller.batch(bid)["items"][0]
	controller._network_operation = Mock()
	with pytest.raises(ValueError, match="批准内容已变化"):
		controller.start(
			"send", {"batch_id": bid, "approved_numbers": [1], "expected_updates": {"1": item["updated_at"] - 1}}
		)
	controller._network_operation.assert_not_called()


def test_uncertain_message_requires_explicit_history_recheck_confirmation(client):
	controller, bid = client
	controller.review(approval_body(controller, bid))
	with DeliveryStore(controller.data_dir) as store:
		store.claim(bid, 1)
		store.finish(bid, 1, "uncertain")
	with pytest.raises(ValueError, match="先核查历史"):
		controller.start("send", {"batch_id": bid, "approved_numbers": [1]})
	controller._network_operation = Mock()
	controller.start("send", {"batch_id": bid, "approved_numbers": [1], "verify_before_retry": True})
	controller._executor.shutdown(wait=True)
	body = controller._network_operation.call_args.args[1]
	assert body["retry_numbers"] == {1} and body["retry_authorization_id"]


def test_user_stop_before_send_does_not_claim(client):
	controller, batch_id = client
	controller.review(approval_body(controller, batch_id))
	platform = FakePlatform()
	with DeliveryStore(controller.data_dir) as store, CacheStore(controller.data_dir / "cache.db") as cache:
		assert send_batch(store, batch_id, platform, cache, limit=2, interval=0, should_stop=lambda: True) == 0
		assert not store.attempted("fixture-job-1")
	assert platform.sent == []


def test_settings_change_keeps_prior_batch_snapshot(client):
	controller, batch_id = client
	controller.save_settings(
		{
			"campus_count": 5,
			"internship_count": 5,
			"min_salary": 15,
			"min_daily_salary": 350,
			"max_pages": 20,
			"max_details": 60,
			"max_seconds": 900,
			"cities": ["上海"],
			"roles": ["ai_development"],
			"history_dedupe": "company",
		}
	)
	assert controller.settings()["rules"]["min_daily_salary"] == 350
	assert controller.batch(batch_id)["params"]["rules"]["min_daily_salary"] == 300


def test_server_cannot_bind_to_public_interface(client):
	with pytest.raises(ValueError, match="loopback"):
		ClientServer(("0.0.0.0", 0), client[0])


def test_private_api_requires_launch_authorization(http_client):
	browser, server, _ = http_client
	with httpx.Client(base_url=server.origin, trust_env=False) as outsider:
		assert outsider.get("/").status_code == 200
		assert outsider.get("/api/state").status_code == 403
		assert outsider.get("/api/events").status_code == 403
		assert server.token not in outsider.get("/").text


def test_launcher_can_authenticate_the_existing_service(http_client):
	import hashlib
	import hmac

	browser, server, _ = http_client
	proof = browser.get("/health", params={"challenge": "fixture-challenge"}).json()["proof"]
	assert proof == hmac.new(server.token.encode(), b"fixture-challenge", hashlib.sha256).hexdigest()


def test_model_label_reads_public_configuration_without_disclosing_keys(client):
	import json

	controller = client[0]
	directory = controller.data_dir / "ai"
	directory.mkdir()
	(directory / "config.json").write_text(
		json.dumps({"ai_model": "deepseek-flash", "api_key": "fixture-secret"}), encoding="utf-8"
	)
	state = controller.state()
	assert state["model"] == "deepseek-flash"
	assert "fixture-secret" not in json.dumps(state)
