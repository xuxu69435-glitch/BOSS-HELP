"""Strict checks against server-side BOSS message history."""

from __future__ import annotations

import hashlib
from typing import Any


class MessageVerificationError(ValueError):
	pass


def _identifier(value: Any) -> str:
	if isinstance(value, bool):
		return ""
	if isinstance(value, int):
		if not 0 < value <= 2**53 - 1:
			return ""
		return str(value)
	if isinstance(value, str) and value.isascii() and value.isdigit() and int(value) > 0:
		return str(int(value))
	return ""


def history_receipt(job: dict[str, Any], friend: dict[str, Any], history: dict[str, Any]) -> dict[str, str] | None:
	"""Require exact job, recipient, outgoing text, and a lossless server message id."""
	if str(friend.get("encryptJobId") or "") != job.get("job_id"):
		raise MessageVerificationError("会话岗位与已批准岗位不一致")
	peer = _identifier(friend.get("uid"))
	text = job.get("greeting")
	if not peer or not isinstance(text, str) or not text.strip():
		raise MessageVerificationError("缺少收件人标识或已批准正文")
	messages = history.get("messages", history.get("historyMsgList"))
	if not isinstance(messages, list):
		raise MessageVerificationError("消息记录结构不完整，不能判断是否已发送")
	for message in messages:
		if not isinstance(message, dict):
			continue
		body = message.get("body")
		sender = message.get("from")
		receiver = message.get("to")
		if not all(isinstance(value, dict) for value in (body, sender, receiver)):
			continue
		assert isinstance(body, dict) and isinstance(sender, dict) and isinstance(receiver, dict)
		if (body.get("text") or message.get("text")) != text:
			continue
		sender_id = _identifier(sender.get("uid"))
		if not sender_id or sender_id == peer or _identifier(receiver.get("uid")) != peer:
			continue
		message_id = _identifier(message.get("mid", message.get("messageId", message.get("msgId", message.get("id")))))
		if not message_id:
			raise MessageVerificationError("正文匹配但消息编号缺失或精度不可靠，不能确认发送")
		return {
			"verification": "history_match",
			"message_id": message_id,
			"recipient_id": peer,
			"job_id": str(job["job_id"]),
			"text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
		}
	return None


def history_confirms_absence(job: dict[str, Any], friend: dict[str, Any], history: dict[str, Any]) -> bool:
	if history_receipt(job, friend, history) is not None or history.get("hasMore") is not False:
		return False
	for message in history.get("messages", history.get("historyMsgList", [])):
		if not isinstance(message, dict) or not isinstance(message.get("body"), dict):
			return False
		body = message["body"]
		text = body.get("text", message.get("text"))
		if text == job["greeting"]:
			return False
		if not isinstance(text, str) and body.get("type") not in {8, 16}:
			link = body.get("hyperLink")
			if (
				body.get("type") == 12
				and message.get("type") == 4
				and isinstance(link, dict)
				and isinstance(link.get("text"), str)
				and link["text"] != job["greeting"]
				and isinstance(message.get("from"), dict)
				and _identifier(message["from"].get("uid")) == _identifier(friend.get("uid"))
			):
				# Observed incoming system hyperlink (e.g. the profile-introduction tip), not outgoing text.
				continue
			return False
	return True
