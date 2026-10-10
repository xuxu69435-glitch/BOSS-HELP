"""Verified client delivery through the existing BOSS chat editor."""

from __future__ import annotations

import time
from typing import Any

from boss_agent_cli.api import endpoints
from boss_agent_cli.api.zhipin_errors import EnvironmentRiskError
from boss_agent_cli.bridge.client import BROWSER_FAILURE_MESSAGES, BridgeClient, BridgeRequestError
from boss_agent_cli.delivery_verification import history_confirms_absence, history_receipt
from boss_agent_cli.platforms.zhipin import BossPlatform


class ChatDeliveryError(RuntimeError):
	code = "CHAT_DELIVERY_UNVERIFIED"

	def __init__(self, message: str, *, stage: str = "", submitted: bool | None = None, reason: str = "") -> None:
		self.delivery_stage = stage
		self.delivery_submitted = submitted
		self.reason = reason
		super().__init__(message)


class VerifiedChatPlatform(BossPlatform):
	"""Used by the durable approval queue, not the legacy direct-greet command."""

	verified_greeting_delivery = True

	def preflight_greeting(self) -> None:
		if self._client._browser_source != "existing-browser":
			raise ChatDeliveryError("正文发送仅支持现有Chrome聊天页，不会切换浏览器或导出登录凭证。")
		self.bridge = BridgeClient(timeout=30, max_retries=1)
		status = self.bridge.status()
		version = str((status or {}).get("extensionVersion") or "")
		try:
			supported = tuple(int(part) for part in version.split(".")) >= (1, 2, 3)
		except ValueError:
			supported = False
		if not status or not status.get("extensionConnected") or not supported:
			raise ChatDeliveryError("发送前请刷新BOSS Agent Bridge扩展至1.2.3或更高版本。")
		result = self.bridge.send_command("inspect", workspace="boss")
		if not result.ok and getattr(result, "code", "") in BROWSER_FAILURE_MESSAGES:
			raise BridgeRequestError(result)
		if not result.ok or not isinstance(result.data, dict) or result.data.get("path") != "/web/geek/chat":
			raise ChatDeliveryError("发送前请在现有Chrome打开BOSS的消息页；不会自动关闭或跳转你的页面。")
		if result.data.get("hostPermissionGranted") is False:
			raise ChatDeliveryError("扩展缺少持续的BOSS站点访问权限，未开始发送；请在Chrome扩展详情中允许访问zhipin.com。")
		if result.data.get("verificationVisible"):
			raise EnvironmentRiskError.from_page(result.data)

	def _read(self, url: str, **params: Any) -> dict[str, Any]:
		response = self._client._browser_request("GET", url, params=params)
		if response.get("code") in {9, 36}:
			raise EnvironmentRiskError.from_response(response, endpoint=url)
		if response.get("code") != 0 or not isinstance(response.get("zpData"), dict):
			raise ChatDeliveryError("会话读取未成功，已停止；不会盲目重试正文。")
		return response["zpData"]

	def _friend(self, job_id: str) -> dict[str, Any] | None:
		data = self._read(endpoints.FRIEND_LIST_URL, page=1)
		friends = data.get("result", data.get("friendList"))
		if not isinstance(friends, list):
			raise ChatDeliveryError("联系人记录结构不完整，不能确认收件人。")
		matches = [friend for friend in friends if isinstance(friend, dict) and friend.get("encryptJobId") == job_id]
		if len(matches) > 1:
			raise ChatDeliveryError("岗位对应多个会话，需人工确认收件人。")
		if not matches:
			return None
		friend = matches[0]
		if not all(friend.get(key) for key in ("uid", "securityId", "brandName", "name")):
			raise ChatDeliveryError("收件人身份不完整，未提交正文。")
		return friend

	def _history(self, friend: dict[str, Any]) -> dict[str, Any]:
		return self._read(
			endpoints.CHAT_HISTORY_URL, gid=str(friend["uid"]), securityId=friend["securityId"], page=1, c=20, src=0
		)

	def _action(self, action: str, target: dict[str, Any]) -> dict[str, Any]:
		result = self.bridge.send_command(action, workspace="boss", data=target)
		if not result.ok or not isinstance(result.data, dict):
			if not result.ok and getattr(result, "code", "") in BROWSER_FAILURE_MESSAGES:
				raise BridgeRequestError(result)
			raise ChatDeliveryError(
				f"浏览器未返回可确认结果（{action}），已停止。",
				stage=action,
				submitted=None if action == "send-chat" else False,
			)
		if result.data.get("code") == "ENVIRONMENT_RISK":
			raise EnvironmentRiskError.from_page(result.data)
		if result.data.get("ok") is not True:
			reason = str(result.data.get("error") or "页面未通过校验")
			messages = {
				"The target contact cannot be identified uniquely": "无法在联系人列表唯一定位招聘者，可能尚未同步或存在同名项",
				"The target contact is not loaded and contact search is unavailable": "新会话尚未载入，页面联系人搜索也不可用",
				"Another conversation contains an unsent draft; it will not be overwritten": "当前会话有未发送草稿，已保留，不会覆盖",
				"An existing draft will not be overwritten": "当前草稿与批准原文不同，已保留，不会覆盖",
				"An existing contact search will not be overwritten": "联系人搜索框有正在使用的内容，已保留",
				"Selected conversation identity did not match the approved job": "所选会话的招聘者或岗位与审批目标不一致",
				"Active recipient or job does not match": "当前招聘者或岗位与审批目标不一致",
				"The page did not enable sending after normal input": "正常输入后发送按钮仍未启用",
				"The normal chat editor is not available": "页面聊天输入框不可用",
				"Recipient, draft or page permissions changed before sending": "提交前收件人、正文或页面权限发生变化",
			}
			raise ChatDeliveryError(
				messages.get(reason, reason[:200]),
				stage=action,
				submitted=False
				if result.data.get("submitted") is False and result.data.get("stage") == "before_submit"
				else None,
				reason=str(result.data.get("code") or ""),
			)
		return result.data

	def _select_chat(self, target: dict[str, Any]) -> None:
		try:
			self._action("select-chat", target)
		except ChatDeliveryError as exc:
			if exc.reason != "CONTACT_NOT_LOADED" or exc.delivery_submitted is not False:
				raise
			# A known server conversation may not yet be in the page's cached contact list.
			self._stage = "refresh-chat"
			refresh = self._action("refresh-chat", {"confirm": True})
			previous_document = refresh.get("previousDocumentStamp")
			if type(previous_document) not in {int, float}:
				raise ChatDeliveryError("未能确认刷新前的页面版本，正文未提交。")
			deadline = time.monotonic() + 20
			while time.monotonic() < deadline:
				result = self.bridge.send_command("inspect", workspace="boss")
				if result.ok and isinstance(result.data, dict):
					if result.data.get("verificationVisible"):
						raise EnvironmentRiskError.from_page(result.data)
					if (
						result.data.get("path") == "/web/geek/chat" and result.data.get("chatReady")
						and type(result.data.get("documentStamp")) in {int, float}
						and result.data["documentStamp"] != previous_document
					):
						break
				time.sleep(0.5)
			else:
				raise ChatDeliveryError("消息页尚未载入联系人，未提交正文。")
			self._stage = "select-chat"
			self._action("select-chat", target)

	def greet(self, security_id: str, job_id: str, message: str = "") -> dict[str, Any]:
		self._stage = "preflight"
		self._submitted: bool | None = False
		try:
			return self._greet(security_id, job_id, message)
		except BaseException as exc:
			if not getattr(exc, "delivery_stage", ""):
				setattr(exc, "delivery_stage", self._stage)
			if not hasattr(exc, "delivery_submitted"):
				setattr(exc, "delivery_submitted", self._submitted)
			raise

	def _greet(self, security_id: str, job_id: str, message: str) -> dict[str, Any]:
		if not security_id or not job_id or not isinstance(message, str) or not 1 <= len(message.strip()) <= 300:
			raise ChatDeliveryError("发送需要明确岗位及1至300字的已批准正文。")
		self.preflight_greeting()
		self._stage = "read_contacts"
		friend = self._friend(job_id)
		if friend is None:
			# Create the conversation only. This acknowledgement never counts as delivery.
			self._stage, self._submitted = "create_contact", None
			response = self._client._browser_request(
				"POST", endpoints.GREET_URL, data={"securityId": security_id, "jobId": job_id}
			)
			if response.get("code") != 0:
				raise ChatDeliveryError("未能确认建立会话，已停止，不会重试或标记已发送。")
			self._stage, self._submitted = "read_created_contact", False
			friend = self._friend(job_id)
			if friend is None:
				raise ChatDeliveryError("建立会话后尚未确认联系人，正文未发送；请在BOSS消息页核实。")
		job = {"job_id": job_id, "greeting": message}
		self._stage = "check_history"
		history = self._history(friend)
		receipt = history_receipt(job, friend, history)
		if receipt:
			return {"code": 0, "zpData": {"greeting_receipt": receipt, "already_present": True}}
		if not history_confirms_absence(job, friend, history):
			raise ChatDeliveryError("历史消息未能确认正文缺失，禁止盲目重发。")
		target = {
			"confirm": True,
			"peer_id": str(friend["uid"]),
			"job_id": job_id,
			"company": friend["brandName"],
			"name": friend["name"],
			"text": message,
		}
		self._stage = "select-chat"
		self._select_chat(target)
		self._stage = "prepare-chat"
		if self._action("prepare-chat", target).get("submitted") is not False:
			raise ChatDeliveryError("聊天准备结果不明，已停止。")
		self._stage = "recheck_history"
		history = self._history(friend)
		receipt = history_receipt(job, friend, history)
		if receipt:
			return {"code": 0, "zpData": {"greeting_receipt": receipt, "already_present": True}}
		if not history_confirms_absence(job, friend, history):
			raise ChatDeliveryError("提交前消息状态变化，已停止。")
		self._stage, self._submitted = "send-chat", None
		if self._action("send-chat", target).get("submitted") is not True:
			raise ChatDeliveryError("页面未确认提交，禁止自动重发。")
		self._stage, self._submitted = "verify_history", True
		receipt = history_receipt(job, friend, self._history(friend))
		if not receipt:
			raise ChatDeliveryError("已尝试提交，但服务器记录中未找到原文，需核实，禁止自动重发。")
		return {"code": 0, "zpData": {"greeting_receipt": receipt}}
