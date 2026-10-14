"""Durable, explicitly reviewed batches of candidate outreach."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any


DEDUPE_REASONS = frozenset({"已打招呼的岗位", "重复岗位或已有沟通记录", "历史批次已入选同一岗位", "历史批次或本批已入选同一公司"})


def snapshot_hash(payload: dict[str, Any]) -> str:
	return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def company_key(name: str) -> str:
	return "".join(unicodedata.normalize("NFKC", name).casefold().split())


class DeliveryStore:
	def __init__(self, data_dir: Path) -> None:
		self.directory = data_dir / "delivery"
		self.directory.mkdir(parents=True, exist_ok=True)
		self.db = sqlite3.connect(self.directory / "batches.db", timeout=10)
		self.db.row_factory = sqlite3.Row
		self.db.execute("PRAGMA journal_mode=WAL")
		self.db.executescript("""
			CREATE TABLE IF NOT EXISTS batches (
				id TEXT PRIMARY KEY, created_at REAL NOT NULL, params TEXT NOT NULL,
				stats TEXT NOT NULL DEFAULT '{}', stop_reason TEXT NOT NULL DEFAULT ''
			);
			CREATE TABLE IF NOT EXISTS items (
				batch_id TEXT NOT NULL, number INTEGER NOT NULL, job_id TEXT NOT NULL,
				payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
				approved_hash TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL,
				error TEXT NOT NULL DEFAULT '', PRIMARY KEY (batch_id, number),
				UNIQUE (batch_id, job_id)
			);
			CREATE TABLE IF NOT EXISTS collection_checkpoints (
				batch_id TEXT PRIMARY KEY, state TEXT NOT NULL, updated_at REAL NOT NULL
			);
			CREATE TABLE IF NOT EXISTS attempts (
				job_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, number INTEGER NOT NULL,
				status TEXT NOT NULL, updated_at REAL NOT NULL
			);
			CREATE TABLE IF NOT EXISTS delivery_events (
				id INTEGER PRIMARY KEY, batch_id TEXT NOT NULL, number INTEGER NOT NULL,
				stage TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL, created_at REAL NOT NULL
			);
			CREATE TABLE IF NOT EXISTS message_receipts (
				batch_id TEXT NOT NULL, number INTEGER NOT NULL, data TEXT NOT NULL,
				verified_at REAL NOT NULL, PRIMARY KEY(batch_id, number)
			);
			CREATE TABLE IF NOT EXISTS status_corrections (
				id INTEGER PRIMARY KEY, batch_id TEXT NOT NULL, number INTEGER NOT NULL,
				previous_status TEXT NOT NULL, new_status TEXT NOT NULL,
				reason TEXT NOT NULL, created_at REAL NOT NULL
			);
			CREATE TABLE IF NOT EXISTS retry_authorizations (
				id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, scope TEXT NOT NULL,
				reason TEXT NOT NULL, created_at REAL NOT NULL
			);
			CREATE TABLE IF NOT EXISTS retry_attempts (
				authorization_id TEXT NOT NULL, number INTEGER NOT NULL,
				job_id TEXT NOT NULL, phase TEXT NOT NULL,
				prepare_count INTEGER NOT NULL, detail TEXT NOT NULL DEFAULT '',
				updated_at REAL NOT NULL, PRIMARY KEY(authorization_id, number)
			);
		""")

	def __enter__(self) -> DeliveryStore:
		return self

	def __exit__(self, *args: Any) -> None:
		self.db.close()

	def create(self, params: dict[str, Any]) -> str:
		batch_id = uuid.uuid4().hex[:16]
		with self.db:
			self.db.execute(
				"INSERT INTO batches (id, created_at, params) VALUES (?, ?, ?)",
				(batch_id, time.time(), json.dumps(params, ensure_ascii=False)),
			)
		return batch_id

	def get(self, batch_id: str) -> dict[str, Any]:
		row = self.db.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
		if row is None:
			raise ValueError("批次不存在")
		batch = dict(row)
		batch["params"] = json.loads(row["params"])
		batch["stats"] = json.loads(row["stats"])
		checkpoint = self.db.execute("SELECT state FROM collection_checkpoints WHERE batch_id = ?", (batch_id,)).fetchone()
		batch["checkpoint"] = json.loads(checkpoint["state"]) if checkpoint else None
		batch["items"] = [
			{**dict(item), "payload": json.loads(item["payload"])}
			for item in self.db.execute("SELECT * FROM items WHERE batch_id = ? ORDER BY number", (batch_id,))
		]
		return batch

	def list_batches(self) -> list[dict[str, Any]]:
		return [
			dict(row)
			for row in self.db.execute(
				"SELECT id, created_at, stop_reason FROM batches ORDER BY created_at DESC LIMIT 100"
			)
		]

	def is_greeted_job(self, job_id: str) -> bool:
		return self.db.execute(
			"SELECT 1 FROM items WHERE job_id=? AND status='sent' LIMIT 1", (job_id,)
		).fetchone() is not None

	def clear_dedupe_history(self) -> int:
		"""Clear exclusion details while preserving jobs, approvals and delivery records."""
		removed = 0
		with self.db:
			self.db.execute("BEGIN IMMEDIATE")
			for row in self.db.execute("SELECT id, stats FROM batches").fetchall():
				stats = json.loads(row["stats"])
				remaining = []
				changed = False
				for record in stats.get("excluded", []):
					reasons = record.get("reasons", [])
					if not any(reason in DEDUPE_REASONS for reason in reasons):
						remaining.append(record)
						continue
					changed = True
					removed += 1
					if other := [reason for reason in reasons if reason not in DEDUPE_REASONS]:
						remaining.append({**record, "reasons": other})
				if changed:
					stats["excluded"] = remaining
					self.db.execute(
						"UPDATE batches SET stats=? WHERE id=?", (json.dumps(stats, ensure_ascii=False), row["id"])
					)
		return removed

	def progress(self, batch_id: str, stats: dict[str, Any], reason: str = "") -> None:
		with self.db:
			self.db.execute(
				"UPDATE batches SET stats = ?, stop_reason = ? WHERE id = ?",
				(json.dumps(stats, ensure_ascii=False), reason, batch_id),
			)

	def set_source_batches(self, batch_id: str, source_ids: list[str]) -> None:
		batch = self.get(batch_id)
		if any(item["status"] not in {"draft", "pending"} for item in batch["items"]):
			raise ValueError("已进入审批或发送的批次不能更改来源")
		params = {**batch["params"], "source_batches": sorted(set(source_ids))}
		with self.db:
			self.db.execute(
				"UPDATE batches SET params=? WHERE id=?", (json.dumps(params, ensure_ascii=False), batch_id)
			)

	def collection_progress(self, batch_id: str, checkpoint: dict[str, Any], stats: dict[str, Any], reason: str) -> None:
		"""Persist the read cursor and counters together, separate from approval snapshots."""
		with self.db:
			self.db.execute(
				"INSERT INTO collection_checkpoints VALUES (?, ?, ?) ON CONFLICT(batch_id) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at",
				(batch_id, json.dumps(checkpoint, ensure_ascii=False), time.time()),
			)
			self.db.execute(
				"UPDATE batches SET stats=?, stop_reason=? WHERE id=?",
				(json.dumps(stats, ensure_ascii=False), reason, batch_id),
			)

	def add(self, batch_id: str, number: int, payload: dict[str, Any]) -> None:
		with self.db:
			self.db.execute(
				"INSERT INTO items (batch_id, number, job_id, payload, updated_at) VALUES (?, ?, ?, ?, ?)",
				(batch_id, number, payload["job_id"], json.dumps(payload, ensure_ascii=False), time.time()),
			)

	def save_draft(self, batch_id: str, number: int, payload: dict[str, Any]) -> None:
		with self.db:
			cursor = self.db.execute(
				"UPDATE items SET payload = ?, status = 'pending', updated_at = MAX(?, updated_at + 0.000001) WHERE batch_id = ? AND number = ? AND status = 'draft'",
				(json.dumps(payload, ensure_ascii=False), time.time(), batch_id, number),
			)
			if cursor.rowcount != 1:
				raise ValueError("该岗位状态已变更，不能覆盖已审批内容")

	def review(
		self,
		batch_id: str,
		*,
		approve: set[int],
		reject: set[int],
		approve_all: bool = False,
		expected_updates: dict[int, float] | None = None,
	) -> None:
		with self.db:
			self.db.execute("BEGIN IMMEDIATE")
			items = {item["number"]: item for item in self.get(batch_id)["items"]}
			if (approve | reject) - items.keys():
				raise ValueError("包含不存在的岗位编号")
			if approve_all:
				approve = {
					number
					for number, item in items.items()
					if item["status"] in {"draft", "pending", "approved", "not_sent"}
				} - reject
			if approve & reject:
				raise ValueError("不能同时批准和拒绝同一岗位")
			if expected_updates is not None and any(
				expected_updates.get(number) != items[number]["updated_at"] for number in approve | reject
			):
				raise ValueError("岗位内容或状态已更新，请刷新后重新审批")
			for number in approve:
				item = items[number]
				if item["status"] not in {"pending", "approved", "not_sent"} or not item["payload"].get("greeting"):
					raise ValueError(f"岗位 {number} 尚未生成招呼或不允许批准")
			for number in reject:
				if items[number]["status"] not in {"draft", "pending", "approved", "rejected", "not_sent", "uncertain"}:
					raise ValueError(f"岗位 {number} 已进入发送流程，不能再拒绝")
			for number in approve | reject:
				accepted = number in approve
				if not accepted and items[number]["status"] == "uncertain":
					detail = "已拒绝后续发送；之前的发送结果仍待核实，原发送尝试和防重记录保留。"
					self.db.execute(
						"INSERT INTO delivery_events(batch_id,number,stage,status,detail,created_at) VALUES (?,?,'user_reject','rejected',?,?)",
						(batch_id, number, detail, time.time()),
					)
					self.db.execute(
						"UPDATE items SET error=? WHERE batch_id=? AND number=?",
						(detail + " 原错误：" + items[number]["error"], batch_id, number),
					)
				self.db.execute(
					"UPDATE items SET status = ?, approved_hash = ?, updated_at = MAX(?, updated_at + 0.000001) WHERE batch_id = ? AND number = ?",
					(
						"approved" if accepted else "rejected",
						snapshot_hash(items[number]["payload"]) if accepted else "",
						time.time(),
						batch_id,
						number,
					),
				)

	def edit_greeting(self, batch_id: str, number: int, greeting: str, expected_update: float) -> None:
		if not isinstance(greeting, str) or not 1 <= len(greeting.strip()) <= 300:
			raise ValueError("招呼必须为1至300字")
		with self.db:
			self.db.execute("BEGIN IMMEDIATE")
			item = next((row for row in self.get(batch_id)["items"] if row["number"] == number), None)
			if item is None or item["status"] not in {"pending", "approved", "not_sent"}:
				raise ValueError("仅可修改待审批或尚未发送的已批准招呼")
			if item["updated_at"] != expected_update:
				raise ValueError("岗位已更新，请刷新后再编辑")
			payload = item["payload"]
			payload.setdefault("draft_edits", []).append({"before": payload["greeting"], "at": time.time()})
			payload["greeting"] = greeting.strip()
			self.db.execute(
				"UPDATE items SET payload=?, status='pending', approved_hash='', updated_at=MAX(?, updated_at + 0.000001) WHERE batch_id=? AND number=?",
				(json.dumps(payload, ensure_ascii=False), time.time(), batch_id, number),
			)

	def attempted(self, job_id: str) -> bool:
		return self.db.execute("SELECT 1 FROM attempts WHERE job_id = ?", (job_id,)).fetchone() is not None

	def skip_contacted(self, batch_id: str, number: int) -> None:
		with self.db:
			self.db.execute(
				"UPDATE items SET status = 'skipped', error = '已存在沟通记录', updated_at = ? "
				"WHERE batch_id = ? AND number = ? AND status IN ('approved','not_sent')",
				(time.time(), batch_id, number),
			)

	def claim(self, batch_id: str, number: int) -> dict[str, Any] | None:
		# Commit the attempt BEFORE the network write. A crash must not cause a resend.
		with self.db:
			self.db.execute("BEGIN IMMEDIATE")
			row = self.db.execute(
				"SELECT * FROM items WHERE batch_id = ? AND number = ?", (batch_id, number)
			).fetchone()
			if row is None or row["status"] not in {"approved", "not_sent"}:
				return None
			payload = json.loads(row["payload"])
			if row["approved_hash"] != snapshot_hash(payload):
				raise ValueError("岗位快照或话术在审批后发生变化，禁止发送")
			previous = self.db.execute("SELECT * FROM attempts WHERE job_id=?", (row["job_id"],)).fetchone()
			if previous and not (
				previous["status"] == "not_sent" and previous["batch_id"] == batch_id and previous["number"] == number
			):
				self.db.execute(
					"UPDATE items SET status = 'skipped', error = '已有发送尝试，需人工核实' WHERE batch_id = ? AND number = ?",
					(batch_id, number),
				)
				return None
			self.db.execute(
				"INSERT INTO attempts VALUES (?, ?, ?, 'sending', ?) ON CONFLICT(job_id) DO UPDATE SET status='sending', updated_at=excluded.updated_at",
				(row["job_id"], batch_id, number, time.time()),
			)
			self.db.execute(
				"INSERT INTO delivery_events(batch_id,number,stage,status,detail,created_at) VALUES (?,?,'claim','sending','已保留本次发送尝试',?)",
				(batch_id, number, time.time()),
			)
			self.db.execute(
				"UPDATE items SET status = 'sending', updated_at = ? WHERE batch_id = ? AND number = ?",
				(time.time(), batch_id, number),
			)
		return payload

	def finish(
		self, batch_id: str, number: int, status: str, error: str = "", *, receipt: Any = None, stage: str = ""
	) -> None:
		with self.db:
			if status == "sent":
				row = self.db.execute(
					"SELECT * FROM items WHERE batch_id=? AND number=?", (batch_id, number)
				).fetchone()
				payload = json.loads(row["payload"]) if row else {}
				expected = hashlib.sha256(payload.get("greeting", "").encode("utf-8")).hexdigest()
				if not (
					row
					and row["status"] in {"sending", "uncertain"}
					and bool(payload.get("greeting"))
					and row["approved_hash"] == snapshot_hash(payload)
					and self.attempted(row["job_id"])
					and isinstance(receipt, dict)
					and receipt.get("verification") == "history_match"
					and isinstance(receipt.get("message_id"), str)
					and receipt["message_id"].strip()
					and isinstance(receipt.get("recipient_id"), str)
					and receipt["recipient_id"].strip()
					and receipt.get("job_id") == row["job_id"]
					and receipt.get("text_sha256") == expected
				):
					raise ValueError("缺少与岗位及招呼原文匹配的消息回执，不能标记已发送")
				self.db.execute(
					"INSERT OR REPLACE INTO message_receipts VALUES (?, ?, ?, ?)",
					(batch_id, number, json.dumps(receipt), time.time()),
				)
			self.db.execute(
				"UPDATE items SET status = ?, error = ?, updated_at = ? WHERE batch_id = ? AND number = ?",
				(status, error, time.time(), batch_id, number),
			)
			self.db.execute(
				"UPDATE attempts SET status = ?, updated_at = ? WHERE batch_id = ? AND number = ?",
				(status, time.time(), batch_id, number),
			)
			self.db.execute(
				"INSERT INTO delivery_events(batch_id,number,stage,status,detail,created_at) VALUES (?,?,?,?,?,?)",
				(
					batch_id,
					number,
					stage or ("history_match" if status == "sent" else "finish"),
					status,
					error,
					time.time(),
				),
			)

	def correct_unverified_sent(self, batch_id: str, numbers: set[int], reason: str) -> int:
		"""Correct misleading statuses without deleting attempts or approved content."""
		with self.db:
			self.db.execute("BEGIN IMMEDIATE")
			items = {item["number"]: item for item in self.get(batch_id)["items"]}
			if not numbers or any(number not in items or items[number]["status"] != "sent" for number in numbers):
				raise ValueError("仅可更正已发送记录，请重新核对状态")
			if any(
				self.db.execute(
					"SELECT 1 FROM message_receipts WHERE batch_id=? AND number=?", (batch_id, number)
				).fetchone()
				for number in numbers
			):
				raise ValueError("含有已核实消息回执的记录，不能批量更正")
			for number in sorted(numbers):
				at = time.time()
				self.db.execute(
					"INSERT INTO status_corrections (batch_id,number,previous_status,new_status,reason,created_at) VALUES (?,?,'sent','uncertain',?,?)",
					(batch_id, number, reason, at),
				)
				self.db.execute(
					"UPDATE items SET status='uncertain', error=?, updated_at=? WHERE batch_id=? AND number=?",
					(reason, at, batch_id, number),
				)
				self.db.execute(
					"UPDATE attempts SET status='uncertain', updated_at=? WHERE batch_id=? AND number=?",
					(at, batch_id, number),
				)
		return len(numbers)

	def authorize_retry(
		self, authorization_id: str, batch_id: str, numbers: set[int], *, excluded_companies: set[str], reason: str
	) -> None:
		"""Save an explicit retry scope; reusing its id never clears consumed attempts."""
		with self.db:
			self.db.execute("BEGIN IMMEDIATE")
			items = {item["number"]: item for item in self.get(batch_id)["items"]}
			if not authorization_id or not reason or not numbers or not numbers <= items.keys():
				raise ValueError("补发授权范围不完整")
			exclusions = {company_key(name) for name in excluded_companies if company_key(name)}
			scope = {}
			for number in sorted(numbers):
				item = items[number]
				payload = item["payload"]
				company = company_key(str(payload.get("company") or ""))
				if any(excluded in company for excluded in exclusions):
					raise ValueError("授权范围包含明确排除的公司")
				if item["status"] not in {"uncertain", "sent"} or not self.attempted(item["job_id"]):
					raise ValueError("补发仅适用于已有尝试的岗位")
				if not payload.get("greeting") or item["approved_hash"] != snapshot_hash(payload):
					raise ValueError("原批准文案或岗位快照发生变化")
				scope[str(number)] = {"job_id": item["job_id"], "approved_hash": item["approved_hash"]}
			serialized = json.dumps(scope, sort_keys=True)
			existing = self.db.execute(
				"SELECT batch_id,scope FROM retry_authorizations WHERE id=?", (authorization_id,)
			).fetchone()
			if existing:
				if existing["batch_id"] != batch_id or existing["scope"] != serialized:
					raise ValueError("不能扩大或覆盖已有补发授权")
				return
			self.db.execute(
				"INSERT INTO retry_authorizations VALUES (?,?,?,?,?)",
				(authorization_id, batch_id, serialized, reason, time.time()),
			)

	def claim_retry(self, authorization_id: str, number: int) -> dict[str, Any] | None:
		with self.db:
			self.db.execute("BEGIN IMMEDIATE")
			authorization = self.db.execute(
				"SELECT * FROM retry_authorizations WHERE id=?", (authorization_id,)
			).fetchone()
			scope = json.loads(authorization["scope"]) if authorization else {}
			if str(number) not in scope:
				raise ValueError("岗位不在本次明确授权的补发范围")
			item = self.db.execute(
				"SELECT * FROM items WHERE batch_id=? AND number=?", (authorization["batch_id"], number)
			).fetchone()
			if item is None or item["status"] != "uncertain":
				return None
			payload = json.loads(item["payload"])
			if (
				item["job_id"] != scope[str(number)]["job_id"]
				or item["approved_hash"] != scope[str(number)]["approved_hash"]
				or snapshot_hash(payload) != scope[str(number)]["approved_hash"]
			):
				raise ValueError("补发文案或岗位快照发生变化")
			previous = self.db.execute(
				"SELECT * FROM retry_attempts WHERE authorization_id=? AND number=?", (authorization_id, number)
			).fetchone()
			if previous and (previous["phase"] != "not_submitted" or previous["prepare_count"] >= 3):
				return None
			count = previous["prepare_count"] + 1 if previous else 1
			self.db.execute(
				"INSERT INTO retry_attempts VALUES (?,?,?,'reserved',?,'',?) ON CONFLICT(authorization_id,number) DO UPDATE SET phase='reserved',prepare_count=excluded.prepare_count,detail='',updated_at=excluded.updated_at",
				(authorization_id, number, item["job_id"], count, time.time()),
			)
		return payload

	def finish_retry(self, authorization_id: str, number: int, phase: str, detail: str = "") -> None:
		if phase not in {"not_submitted", "submitted", "uncertain", "verified"}:
			raise ValueError("无效的补发阶段")
		with self.db:
			self.db.execute("BEGIN IMMEDIATE")
			previous = self.db.execute(
				"SELECT * FROM retry_attempts WHERE authorization_id=? AND number=?", (authorization_id, number)
			).fetchone()
			if previous is None or previous["phase"] == "verified":
				raise ValueError("没有可更新的补发尝试")
			if phase == "not_submitted" and previous["phase"] != "reserved":
				raise ValueError("不能把已提交或结果不明的尝试改回未提交")
			if phase == "verified":
				authorization = self.db.execute(
					"SELECT batch_id FROM retry_authorizations WHERE id=?", (authorization_id,)
				).fetchone()
				if not self.db.execute(
					"SELECT 1 FROM message_receipts WHERE batch_id=? AND number=?", (authorization["batch_id"], number)
				).fetchone():
					raise ValueError("缺少已核实的消息回执")
			self.db.execute(
				"UPDATE retry_attempts SET phase=?,detail=?,updated_at=? WHERE authorization_id=? AND number=?",
				(phase, detail, time.time(), authorization_id, number),
			)
