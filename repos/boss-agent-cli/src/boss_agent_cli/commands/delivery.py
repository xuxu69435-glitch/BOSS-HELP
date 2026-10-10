"""Prepare up to fifty jobs, review once, then send approved greetings."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

import click
from rich.console import Console
from rich.table import Table

from boss_agent_cli.ai.service import AIServiceError
from boss_agent_cli.api.browser_source import require_existing_boss_tab
from boss_agent_cli.auth.manager import AuthManager
from boss_agent_cli.cache.store import CacheStore
from boss_agent_cli.commands._platform import get_platform_instance
from boss_agent_cli.commands.ai_cmd import _load_resume_text, _require_ai_service
from boss_agent_cli.delivery import DeliveryError, collect_batch, draft_batch, public_batch, send_batch, write_preview
from boss_agent_cli.delivery_store import DeliveryStore
from boss_agent_cli.display import handle_auth_errors, handle_error_output, handle_output


@contextmanager
def _store(ctx: click.Context) -> Iterator[DeliveryStore]:
	try:
		with DeliveryStore(ctx.obj["data_dir"]) as store:
			yield store
	except (ValueError, DeliveryError, AIServiceError) as exc:
		code = (
			exc.code
			if isinstance(exc, DeliveryError)
			else ("AI_API_ERROR" if isinstance(exc, AIServiceError) else "INVALID_PARAM")
		)
		handle_error_output(
			ctx,
			"delivery",
			code=code,
			message=str(exc),
			recovery_action="查看 delivery list / preview；核实后再继续，不自动重试发送",
		)


def _render(data: dict[str, Any]) -> None:
	table = Table("编号", "状态", "岗位", "公司", "城市", "薪资")
	for item in data["items"]:
		table.add_row(str(item["number"]), item["status"], item["title"], item["company"], item["city"], item["salary"])
	console = Console(stderr=True)
	console.print(table)
	console.print(
		f"批次: {data['batch_id']} | 目标: {data['params']['count']} | 实际: {len(data['items'])}", markup=False
	)
	console.print(f"完整预览: {data['preview_path']}", markup=False)


def _output(ctx: click.Context, store: DeliveryStore, batch_id: str) -> None:
	data = public_batch(store.get(batch_id))
	data["preview_path"] = write_preview(store, batch_id)
	handle_output(
		ctx,
		"delivery",
		data,
		render=_render,
		hints={
			"operator_actions": [
				"先阅读完整预览中的摘要、JD和拟发送招呼，再批准或拒绝编号。审批本身不发送消息。",
			],
			"next_actions": [
				f"boss delivery preview {batch_id}",
				f"boss delivery review {batch_id} --approve-all --reject <编号,编号>",
				f"boss delivery send {batch_id} --limit 10 --execute",
			],
		},
	)


def _numbers(value: str) -> set[int]:
	try:
		numbers = {int(part.strip()) for part in value.split(",") if part.strip()}
	except ValueError as exc:
		raise ValueError("岗位编号必须是逗号分隔的正整数") from exc
	if any(number < 1 for number in numbers):
		raise ValueError("岗位编号必须大于零")
	return numbers


@click.group("delivery")
@click.pass_context
def delivery_group(ctx: click.Context) -> None:
	"""岗位批次：规则筛选、专属草稿、整批审批、限量发送。"""
	ctx.obj["browser_source"] = "existing-browser"
	if ctx.obj.get("platform") != "zhipin" or ctx.obj.get("role") != "candidate":
		handle_error_output(
			ctx,
			"delivery",
			code="NOT_SUPPORTED",
			message="岗位批次当前只支持 BOSS 求职者，请使用 --platform zhipin --role candidate",
		)


@delivery_group.command("prepare")
@click.argument("query")
@click.option("--resume", "resume_name", help="本地简历名；生成招呼时必填")
@click.option("--no-ai", is_flag=True, help="仅规则筛选和获取完整JD，不调用模型，不可审批发送")
@click.option("--count", default=50, show_default=True, type=click.IntRange(1, 50))
@click.option("--city", default="", help="允许的城市，逗号分隔")
@click.option("--min-salary", type=click.IntRange(1), help="岗位月薪区间下限至少为多少K；面议/日薪视为信息不足")
@click.option("--extra-query", multiple=True, help="额外搜索词，可重复；与各城市轮流搜索，共用数量和页数预算")
@click.option("--graduation-year", type=click.IntRange(2020, 2100), help="仅保留面向该届的校招或实习岗位")
@click.option("--min-daily-salary", type=click.IntRange(1), help="实习日薪下限（元），与 --graduation-year 同用")
@click.option("--bachelor-eligible", is_flag=True, help="用学历标签和完整JD复核本科可申请资格")
@click.option(
	"--max-internship-ratio",
	type=click.FloatRange(0, 1, max_open=True),
	help="实习占实际预览数量的比例上限，例如0.2；与毕业届别同用",
)
@click.option("--experience", default="", help="允许的岗位经验要求，逗号分隔，精确匹配（不是个人年限）")
@click.option("--education", default="", help="允许的岗位学历要求，逗号分隔，精确匹配")
@click.option("--industry", default="", help="允许的行业，逗号分隔，精确匹配")
@click.option("--scale", default="", help="允许的公司规模，逗号分隔，精确匹配")
@click.option("--stage", default="", help="允许的融资阶段，逗号分隔，精确匹配")
@click.option("--require", "required_words", multiple=True, help="岗位名称/JD必须包含的关键词，可重复，全部满足")
@click.option("--exclude", "excluded_words", multiple=True, help="岗位名称/JD排除关键词，可重复，任一命中即排除")
@click.option("--exclude-company", multiple=True, help="公司名称排除关键词，可重复")
@click.option(
	"--max-pages", default=10, show_default=True, type=click.IntRange(1, 200), help="所有城市和关键词合计的搜索页数预算"
)
@click.option("--max-details", default=150, show_default=True, type=click.IntRange(1, 300))
@click.option("--max-seconds", default=900, show_default=True, type=click.IntRange(1, 3600), help="采集时间预算（秒）")
@click.pass_context
@handle_auth_errors("delivery.prepare")
def prepare_cmd(
	ctx: click.Context,
	query: str,
	resume_name: str | None,
	no_ai: bool,
	count: int,
	required_words: tuple[str, ...],
	excluded_words: tuple[str, ...],
	exclude_company: tuple[str, ...],
	min_salary: int | None,
	extra_query: tuple[str, ...],
	graduation_year: int | None,
	min_daily_salary: int | None,
	bachelor_eligible: bool,
	max_internship_ratio: float | None,
	max_pages: int,
	max_details: int,
	max_seconds: int,
	**filters: str,
) -> None:
	"""逐页筛选，最多收集50个完整JD，生成摘要与专属招呼；绝不发送。"""
	with _store(ctx) as store:
		if not query.strip():
			raise ValueError("搜索词不能为空")
		if any(not value.strip() for value in extra_query):
			raise ValueError("额外搜索词不能为空")
		if min_daily_salary and not graduation_year:
			raise ValueError("日薪规则需要同时指定 --graduation-year")
		if max_internship_ratio is not None and not graduation_year:
			raise ValueError("实习比例需要同时指定 --graduation-year")
		service, resume_text = None, None
		if not no_ai:
			if not resume_name:
				raise ValueError("生成招呼需要 --resume；暂未接API时使用 --no-ai")
			service = _require_ai_service(ctx)
			resume_text = _load_resume_text(ctx, resume_name)
		rules: dict[str, Any] = {
			key: [part.strip() for part in value.split(",") if part.strip()] for key, value in filters.items()
		}
		for words in (required_words, excluded_words, exclude_company):
			if any(not word.strip() for word in words):
				raise ValueError("关键词不能为空")
		rules.update(
			min_salary=min_salary,
			min_daily_salary=min_daily_salary,
			graduation_year=graduation_year,
			bachelor_eligible=bachelor_eligible,
			max_internship_ratio=max_internship_ratio,
			require=list(required_words),
			exclude=list(excluded_words),
			exclude_company=list(exclude_company),
		)
		require_existing_boss_tab()
		batch_id = store.create(
			{
				"query": query.strip(),
				"extra_queries": [value.strip() for value in extra_query],
				"count": count,
				"rules": rules,
				"max_pages": max_pages,
				"max_details": max_details,
				"max_seconds": max_seconds,
				"resume": resume_name,
				"platform": "zhipin",
			}
		)
		ctx.obj["logger"].info(f"已创建岗位批次 {batch_id}，可用 delivery preview 查看已保存进度")
		try:
			auth = AuthManager(ctx.obj["data_dir"], logger=ctx.obj["logger"], platform="zhipin")
			with (
				get_platform_instance(ctx, auth) as platform,
				CacheStore(ctx.obj["data_dir"] / "cache" / "boss_agent.db") as cache,
			):
				collect_batch(store, batch_id, platform, cache, ctx.obj["logger"])
			if service is not None and resume_text:
				draft_batch(store, batch_id, service, resume_text, ctx.obj["logger"])
		finally:
			write_preview(store, batch_id)
		_output(ctx, store, batch_id)


@delivery_group.command("draft")
@click.argument("batch_id")
@click.option("--resume", "resume_name", required=True)
@click.pass_context
@handle_auth_errors("delivery.draft")
def draft_cmd(ctx: click.Context, batch_id: str, resume_name: str) -> None:
	"""为已有JD补齐API草稿；只处理待生成项，不覆盖审批内容。"""
	with _store(ctx) as store:
		store.get(batch_id)
		service = _require_ai_service(ctx)
		resume_text = _load_resume_text(ctx, resume_name)
		try:
			if service is not None and resume_text:
				draft_batch(store, batch_id, service, resume_text, ctx.obj["logger"])
		finally:
			write_preview(store, batch_id)
		_output(ctx, store, batch_id)


@delivery_group.command("list")
@click.pass_context
@handle_auth_errors("delivery.list")
def list_cmd(ctx: click.Context) -> None:
	"""列出本地批次，不访问招聘平台。"""
	with _store(ctx) as store:
		handle_output(ctx, "delivery.list", store.list_batches())


@delivery_group.command("preview")
@click.argument("batch_id")
@click.pass_context
@handle_auth_errors("delivery.preview")
def preview_cmd(ctx: click.Context, batch_id: str) -> None:
	"""查看全部岗位、完整JD、招呼与审批/发送记录。"""
	with _store(ctx) as store:
		_output(ctx, store, batch_id)


@delivery_group.command("review")
@click.argument("batch_id")
@click.option("--approve-all", is_flag=True, help="批准全部待审批项（保留此前拒绝项）")
@click.option("--approve", default="", help="只批准这些编号，如 1,5,8")
@click.option("--reject", default="", help="拒绝这些编号；可与 --approve-all 同用")
@click.pass_context
@handle_auth_errors("delivery.review")
def review_cmd(ctx: click.Context, batch_id: str, approve_all: bool, approve: str, reject: str) -> None:
	"""一次性审批；不会发送任何消息。"""
	with _store(ctx) as store:
		if (approve_all and approve) or not (approve_all or approve or reject):
			raise ValueError("指定 --approve-all 或 --approve，可同时指定 --reject")
		store.review(batch_id, approve=_numbers(approve), reject=_numbers(reject), approve_all=approve_all)
		_output(ctx, store, batch_id)


@delivery_group.command("send")
@click.argument("batch_id")
@click.option("--limit", default=10, show_default=True, type=click.IntRange(1, 50), help="本次最多发送数量")
@click.option(
	"--interval", default=10.0, show_default=True, type=click.FloatRange(2, 60), help="两条消息之间的最小间隔秒数"
)
@click.option("--execute", is_flag=True, help="明确执行平台发送；省略时只预览")
@click.pass_context
@handle_auth_errors("delivery.send")
def send_cmd(ctx: click.Context, batch_id: str, limit: int, interval: float, execute: bool) -> None:
	"""只发送已批准的原文；默认预览，失败停批且不自动重试。"""
	with _store(ctx) as store:
		batch = store.get(batch_id)
		approved = [item for item in batch["items"] if item["status"] == "approved"]
		if execute and approved:
			require_existing_boss_tab()
			try:
				auth = AuthManager(ctx.obj["data_dir"], logger=ctx.obj["logger"], platform="zhipin")
				with (
					get_platform_instance(ctx, auth) as platform,
					CacheStore(ctx.obj["data_dir"] / "cache" / "boss_agent.db") as cache,
				):
					send_batch(
						store, batch_id, platform, cache, limit=limit, interval=interval, hooks=ctx.obj.get("hooks")
					)
			finally:
				write_preview(store, batch_id)
		_output(ctx, store, batch_id)
