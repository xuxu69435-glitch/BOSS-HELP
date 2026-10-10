"""Explicit, bounded retries using the normal chat editor and server-side receipts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from boss_agent_cli.api import endpoints
from boss_agent_cli.api.client import BossClient
from boss_agent_cli.api.zhipin_errors import EnvironmentRiskError
from boss_agent_cli.auth.manager import AuthManager
from boss_agent_cli.bridge.client import BROWSER_FAILURE_MESSAGES, BridgeClient, BridgeRequestError, is_supported_extension_version
from boss_agent_cli.chat_delivery import VerifiedChatPlatform
from boss_agent_cli.delivery import DeliveryError, write_preview
from boss_agent_cli.delivery_store import DeliveryStore, company_key, snapshot_hash
from boss_agent_cli.delivery_verification import history_confirms_absence, history_receipt
from boss_agent_cli.output import redact_sensitive_text


def _data(response: dict[str, Any]) -> dict[str, Any]:
	if response.get("code") != 0 or not isinstance(response.get("zpData"), dict):
		raise DeliveryError("MESSAGE_READ_FAILED", "消息读取未成功，已停止；不会提交或重试正文")
	return response["zpData"]


def _action(bridge: BridgeClient, action: str, target: dict[str, Any]) -> dict[str, Any]:
	result = bridge.send_command(action, workspace="boss", data=target)
	if not result.ok or not isinstance(result.data, dict):
		if not result.ok and getattr(result, "code", "") in BROWSER_FAILURE_MESSAGES:
			raise BridgeRequestError(result)
		raise DeliveryError("CHAT_RESULT_UNCERTAIN", "聊天操作未取得可确认结果；不会自动重试")
	if result.data.get("code") == "ENVIRONMENT_RISK":
		raise EnvironmentRiskError.from_page(result.data)
	return result.data


def retry_batch(
	data_dir: Path,
	batch_id: str,
	authorization_id: str,
	authorized_numbers: set[int],
	run_numbers: set[int],
	excluded_companies: set[str],
	*,
	mode: str,
	should_stop: Any = None,
	logger: Any = None,
) -> list[dict[str, Any]]:
	if mode not in {"check", "prepare", "send"} or not run_numbers or not run_numbers <= authorized_numbers:
		raise ValueError("无效的补发模式或范围")
	if mode == "prepare" and len(run_numbers) != 1:
		raise ValueError("预填检查一次只能选择一个会话")
	bridge = BridgeClient(timeout=30, max_retries=1)
	status = bridge.status()
	version = str((status or {}).get("extensionVersion") or "")
	if (
		not status
		or not status.get("extensionConnected")
		or not is_supported_extension_version(version)
		or tuple(map(int, version.split("."))) < (1, 2, 3)
	):
		raise ValueError("补发需要已连接的1.2.3或更高版本扩展")
	inspection = bridge.send_command("inspect", workspace="boss")
	if not inspection.ok and getattr(inspection, "code", "") in BROWSER_FAILURE_MESSAGES:
		raise BridgeRequestError(inspection)
	if not inspection.ok or not isinstance(inspection.data, dict) or inspection.data.get("path") != "/web/geek/chat":
		raise ValueError("请保留现有BOSS聊天页；不会自动导航或新建页面")
	if inspection.data.get("verificationVisible"):
		raise EnvironmentRiskError.from_page(inspection.data)
	if inspection.data.get("hostPermissionGranted") is False:
		raise ValueError("扩展缺少持续的BOSS站点访问权限，未开始核查或补发；请在Chrome扩展详情中允许访问zhipin.com。")
	results = []

	def check_stop() -> None:
		if should_stop and should_stop():
			raise DeliveryError("CANCELLED", "已停止核查与补发，未继续提交")

	with (
		DeliveryStore(data_dir) as store,
		BossClient(AuthManager(data_dir), browser_source="existing-browser", delay=(6, 6)) as client,
	):
		store.authorize_retry(
			authorization_id,
			batch_id,
			authorized_numbers,
			excluded_companies=excluded_companies,
			reason="用户明确授权：按原批准文案尝试补发，仅限指定岗位，排除指定公司；有相同正文则跳过。",
		)
		items = {item["number"]: item for item in store.get(batch_id)["items"]}
		check_stop()
		sender = VerifiedChatPlatform(client)
		sender.bridge = bridge
		response = _data(client._browser_request("GET", endpoints.FRIEND_LIST_URL, params={"page": 1}))
		friends = response.get("result", response.get("friendList"))
		if not isinstance(friends, list):
			raise ValueError("联系人结构不完整，不能选择收件人")
		for number in sorted(run_numbers):
			check_stop()
			item = items[number]
			job = item["payload"]
			if logger:
				logger.info(f"先核查待确认岗位 #{number}：{job.get('company', '')}；有相同正文则跳过")
			if item["approved_hash"] != snapshot_hash(job) or any(
				company_key(name) in company_key(str(job.get("company") or "")) for name in excluded_companies
			):
				raise ValueError("岗位内容变化或公司被排除，已停止")
			matches = [
				friend for friend in friends if isinstance(friend, dict) and friend.get("encryptJobId") == job["job_id"]
			]
			if not matches:
				raise DeliveryError(
					"CHAT_CONTACT_NOT_FOUND",
					f"岗位{number}未在已读取的会话列表中找到，不能据此判断之前是否发送；未补发、未新建会话。可在BOSS核实，或拒绝该岗位停止后续发送。",
				)
			if len(matches) != 1:
				raise DeliveryError("CHAT_CONTACT_AMBIGUOUS", f"岗位{number}对应多个既有会话，收件人不明确；不会补发或建立新会话")
			friend = matches[0]
			if (
				not friend.get("uid")
				or not friend.get("securityId")
				or not friend.get("brandName")
				or not friend.get("name")
			):
				raise ValueError(f"岗位{number}缺少完整收件人身份")

			def read_history() -> dict[str, Any]:
				check_stop()
				return _data(
					client._browser_request(
						"GET",
						endpoints.CHAT_HISTORY_URL,
						params={
							"gid": str(friend["uid"]),
							"securityId": friend["securityId"],
							"page": 1,
							"c": 20,
							"src": 0,
						},
					)
				)

			history = read_history()
			receipt = history_receipt(job, friend, history)
			if receipt:
				if item["status"] == "uncertain":
					store.finish(batch_id, number, "sent", receipt=receipt)
				attempt = store.db.execute(
					"SELECT phase FROM retry_attempts WHERE authorization_id=? AND number=?", (authorization_id, number)
				).fetchone()
				if attempt and attempt["phase"] != "verified":
					store.finish_retry(authorization_id, number, "verified", "复核服务器消息记录确认已发送；未重复提交")
				write_preview(store, batch_id)
				result = {"number": number, "company": job.get("company"), "verified": True, "resent": False}
				results.append(result)
				print(json.dumps(result, ensure_ascii=False), flush=True)
				continue
			if not history_confirms_absence(job, friend, history):
				raise ValueError(f"岗位{number}的记录未能确认正文缺失，禁止盲目重发")
			if mode == "check":
				result = {"number": number, "company": job.get("company"), "text_absent": True, "submitted": False}
				results.append(result)
				print(json.dumps(result, ensure_ascii=False), flush=True)
				continue
			attempt = store.db.execute(
				"SELECT phase,prepare_count FROM retry_attempts WHERE authorization_id=? AND number=?",
				(authorization_id, number),
			).fetchone()
			if item["status"] != "uncertain" or (
				attempt and (attempt["phase"] != "not_submitted" or attempt["prepare_count"] >= 3)
			):
				raise ValueError(f"岗位{number}已有未核实补发尝试，不会重新预填或提交")
			target = {
				"confirm": True,
				"peer_id": str(friend["uid"]),
				"job_id": job["job_id"],
				"company": friend["brandName"],
				"name": friend["name"],
				"text": job["greeting"],
			}
			for action in ("select-chat", "prepare-chat"):
				check_stop()
				if action == "select-chat":
					sender._select_chat(target)
					continue
				outcome = _action(bridge, action, target)
				if outcome.get("ok") is not True or outcome.get("submitted") is not False:
					raise ValueError(f"岗位{number}准备未通过：{outcome.get('error') or '状态不明'}")
			if mode == "prepare":
				result = {"number": number, "company": job.get("company"), "prepared": True, "submitted": False}
				results.append(result)
				print(json.dumps(result, ensure_ascii=False), flush=True)
				continue
			# Recheck after prefill in case the user sent manually while we prepared.
			history = read_history()
			receipt = history_receipt(job, friend, history)
			if receipt:
				store.finish(batch_id, number, "sent", receipt=receipt)
				write_preview(store, batch_id)
				result = {"number": number, "company": job.get("company"), "verified": True, "resent": False}
				results.append(result)
				print(json.dumps(result, ensure_ascii=False), flush=True)
				continue
			if not history_confirms_absence(job, friend, history):
				raise ValueError("提交前的消息状态变化，已停止")
			if store.claim_retry(authorization_id, number) is None:
				raise ValueError(f"岗位{number}已有未核实补发尝试，不会再次提交")
			try:
				outcome = _action(bridge, "send-chat", target)
				if outcome.get("submitted") is not True:
					if outcome.get("submitted") is False and outcome.get("stage") == "before_submit":
						store.finish_retry(
							authorization_id, number, "not_submitted", str(outcome.get("error") or "提交前停止")
						)
					else:
						store.finish_retry(authorization_id, number, "uncertain", "提交结果不明")
					raise ValueError(f"岗位{number}未确认提交：{outcome.get('error') or '状态不明'}")
				store.finish_retry(authorization_id, number, "submitted", "已触发页面发送，尚待服务器消息核验")
				receipt = history_receipt(job, friend, read_history())
				if not receipt:
					store.finish_retry(authorization_id, number, "uncertain", "服务器记录中未找到匹配原文")
					raise ValueError(f"岗位{number}尚无匹配消息回执，停止后续发送")
				store.finish(batch_id, number, "sent", receipt=receipt)
				store.finish_retry(authorization_id, number, "verified", "服务器消息记录匹配收件人、岗位和原批准正文")
			except BaseException as exc:
				phase = store.db.execute(
					"SELECT phase FROM retry_attempts WHERE authorization_id=? AND number=?", (authorization_id, number)
				).fetchone()
				if phase and phase["phase"] == "reserved" and getattr(exc, "delivery_submitted", None) is False:
					store.finish_retry(authorization_id, number, "not_submitted", "扩展确认本次补发操作未执行；原待核实记录保留")
				elif phase and phase["phase"] in {"reserved", "submitted"}:
					store.finish_retry(authorization_id, number, "uncertain", "执行中断或无法核实；禁止自动重发")
				raise
			finally:
				write_preview(store, batch_id)
			result = {"number": number, "company": job.get("company"), "verified": True, "resent": True}
			results.append(result)
			print(json.dumps(result, ensure_ascii=False), flush=True)
	return results


def main() -> None:
	parser = argparse.ArgumentParser(description="Explicit verified retries, never contact-only acknowledgements")
	parser.add_argument("--data-dir", required=True, type=Path)
	parser.add_argument("--batch-id", required=True)
	parser.add_argument("--authorization-id", required=True)
	parser.add_argument("--authorized-numbers", required=True)
	parser.add_argument("--only", required=True)
	parser.add_argument("--exclude-company", action="append", default=[])
	parser.add_argument("--mode", choices=["check", "prepare", "send"], default="check")
	args = parser.parse_args()
	try:
		retry_batch(
			args.data_dir,
			args.batch_id,
			args.authorization_id,
			{int(n) for n in args.authorized_numbers.split(",")},
			{int(n) for n in args.only.split(",")},
			set(args.exclude_company),
			mode=args.mode,
		)
	except Exception as exc:
		print(
			json.dumps(
				{
					"stopped": True,
					"code": getattr(exc, "code", type(exc).__name__),
					"message": redact_sensitive_text(str(exc)),
				},
				ensure_ascii=False,
			),
			flush=True,
		)
		raise SystemExit(1)


if __name__ == "__main__":
	main()
