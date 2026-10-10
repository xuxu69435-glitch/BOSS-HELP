"""Rejecting follow-up never erases an unknown past delivery."""

import pytest

from boss_agent_cli.delivery_store import DeliveryStore
from client_fixture import fixture_client


def test_reject_uncertain_preserves_attempt_body_and_audit(tmp_path):
	controller, bid = fixture_client(tmp_path)
	try:
		with DeliveryStore(tmp_path) as store:
			store.review(bid, approve={1}, reject=set())
			store.claim(bid, 1)
			store.finish(bid, 1, "uncertain", "Connection lost after contact request", stage="create_contact")
			before = store.get(bid)["items"][0]
			attempt = dict(store.db.execute("SELECT * FROM attempts WHERE job_id=?", (before["job_id"],)).fetchone())
			store.review(bid, approve=set(), reject={1}, expected_updates={1: before["updated_at"]})
			after = store.get(bid)["items"][0]
			assert after["status"] == "rejected" and after["approved_hash"] == ""
			assert after["payload"] == before["payload"]
			assert before["error"] in after["error"] and "仍待核实" in after["error"]
			assert dict(store.db.execute("SELECT * FROM attempts WHERE job_id=?", (before["job_id"],)).fetchone()) == attempt
			assert store.claim(bid, 1) is None
			with pytest.raises(ValueError, match="不允许批准"):
				store.review(bid, approve={1}, reject=set())
			with pytest.raises(ValueError):
				store.authorize_retry("fixture-auth", bid, {1}, excluded_companies=set(), reason="Fixture explicit scope")
			assert store.db.execute("SELECT count(*) FROM delivery_events WHERE stage='user_reject'").fetchone()[0] == 1
	finally:
		controller.close()


def test_uncertain_can_be_rejected_locally_during_platform_pause(tmp_path):
	controller, bid = fixture_client(tmp_path)
	try:
		with DeliveryStore(tmp_path) as store:
			store.review(bid, approve={1}, reject=set())
			store.claim(bid, 1)
			store.finish(bid, 1, "uncertain", "Fixture")
			before = store.get(bid)["items"][0]
		controller._set_browser_state("blocked", "Fixture platform pause", blocked_until=10**10)
		controller.review({"batch_id": bid, "approve": [], "reject": [1], "expected_updates": {"1": before["updated_at"]}})
		assert controller.batch(bid)["items"][0]["status"] == "rejected"
	finally:
		controller.close()


def test_stale_rejection_and_active_send_are_still_blocked(tmp_path):
	controller, bid = fixture_client(tmp_path)
	try:
		with DeliveryStore(tmp_path) as store:
			store.review(bid, approve={1}, reject=set())
			store.claim(bid, 1)
			with pytest.raises(ValueError, match="发送流程"):
				store.review(bid, approve=set(), reject={1})
			store.finish(bid, 1, "uncertain", "Fixture")
			with pytest.raises(ValueError, match="已更新"):
				store.review(bid, approve=set(), reject={1}, expected_updates={1: 0})
			assert store.get(bid)["items"][0]["status"] == "uncertain"
	finally:
		controller.close()
