import hashlib

import pytest

from boss_agent_cli.delivery_verification import MessageVerificationError, history_confirms_absence, history_receipt


JOB = {"job_id": "fixture-job", "greeting": "Approved exact text"}
FRIEND = {"uid": 22, "encryptJobId": "fixture-job"}


def message(**updates):
	return {
		"mid": "9007199254740993",
		"from": {"uid": 11},
		"to": {"uid": 22},
		"body": {"type": 1, "text": JOB["greeting"]},
		**updates,
	}


def test_exact_outgoing_text_with_server_mid_is_verified():
	receipt = history_receipt(JOB, FRIEND, {"messages": [message()], "hasMore": False})
	assert receipt == {
		"verification": "history_match",
		"message_id": "9007199254740993",
		"recipient_id": "22",
		"job_id": JOB["job_id"],
		"text_sha256": hashlib.sha256(JOB["greeting"].encode()).hexdigest(),
	}


@pytest.mark.parametrize(
	"updates",
	[
		{"from": {"uid": 22}, "to": {"uid": 11}},
		{"to": {"uid": 99}},
		{"from": {}},
		{"body": {"type": 1, "text": "Other text"}},
		{"body": {"type": 8, "jobDesc": {"content": JOB["greeting"]}}},
	],
)
def test_incoming_other_recipient_or_system_card_is_not_a_receipt(updates):
	assert history_receipt(JOB, FRIEND, {"messages": [message(**updates)]}) is None


@pytest.mark.parametrize("value", [0, None, True, 9007199254740992, "not-an-id"])
def test_missing_or_rounded_mid_is_not_accepted(value):
	with pytest.raises(MessageVerificationError, match="消息编号"):
		history_receipt(JOB, FRIEND, {"messages": [message(mid=value)]})


def test_other_job_or_unknown_schema_cannot_be_verified():
	with pytest.raises(MessageVerificationError):
		history_receipt(JOB, {**FRIEND, "encryptJobId": "wrong-job"}, {"messages": []})
	with pytest.raises(MessageVerificationError):
		history_receipt(JOB, FRIEND, {})


def test_absence_requires_a_complete_history_page():
	assert not history_confirms_absence(JOB, FRIEND, {"messages": [], "hasMore": True})
	assert not history_confirms_absence(JOB, FRIEND, {"messages": []})
	assert history_confirms_absence(JOB, FRIEND, {"messages": [], "hasMore": False})
	assert not history_confirms_absence(JOB, FRIEND, {"messages": [message()], "hasMore": False})
	assert not history_confirms_absence(JOB, FRIEND, {"messages": [message(**{"from": {}})], "hasMore": False})
	assert not history_confirms_absence(JOB, FRIEND, {"messages": [{"body": {"type": 999}}], "hasMore": False})
	assert history_confirms_absence(
		JOB,
		FRIEND,
		{"messages": [{"body": {"type": 8, "jobDesc": {}}}, {"body": {"type": 16, "articles": []}}], "hasMore": False},
	)


def test_observed_incoming_system_hyperlink_is_not_an_unparsed_outgoing_message():
	tip = message(
		type=4,
		**{
			"from": {"uid": 22},
			"to": {"uid": 11},
			"body": {"type": 12, "hyperLink": {"text": "快捷设置自我介绍", "hyperLinkType": 1}},
		},
	)
	assert history_confirms_absence(JOB, FRIEND, {"messages": [tip], "hasMore": False})
	assert not history_confirms_absence(
		JOB, FRIEND, {"messages": [{**tip, "from": {"uid": 11}, "to": {"uid": 22}}], "hasMore": False}
	)
	tip["body"]["hyperLink"]["text"] = JOB["greeting"]
	assert not history_confirms_absence(JOB, FRIEND, {"messages": [tip], "hasMore": False})
