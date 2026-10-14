"""Offline batch review and outreach safety tests; no live platform writes."""

import hashlib
import json
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from boss_agent_cli.cache.store import CacheStore
from boss_agent_cli.delivery import DeliveryError, filter_job, generate_draft, send_batch, write_preview
from boss_agent_cli.delivery_store import DeliveryStore
from boss_agent_cli.main import cli


def job(number):
	return {
		"encryptJobId": f"job-{number}",
		"securityId": f"sec-{number}",
		"jobName": f"Python engineer {number}",
		"brandName": "Example",
		"salaryDesc": "20-30K",
		"cityName": "北京",
		"jobExperience": "1-3年",
		"jobDegree": "本科",
	}


class FakePlatform:
	verified_greeting_delivery = True

	def __init__(self, total=60):
		self.total = total
		self.searches = []
		self.details = []
		self.sent = []
		self.failed_number = None
		self.raise_on_send = False

	def __enter__(self):
		return self

	def __exit__(self, *args):
		pass

	def search_jobs(self, query, *, city, page):
		self.searches.append(page)
		return {
			"code": 0,
			"zpData": {
				"jobList": [job(i) for i in range((page - 1) * 30 + 1, min(page * 30, self.total) + 1)],
				"hasMore": page * 30 < self.total,
			},
		}

	def job_detail(self, job_id):
		self.details.append(job_id)
		return {
			"code": 0,
			"zpData": {
				"jobInfo": {"encryptJobId": job_id},
				"jobDetail": f"Full JD for {job_id}: Python and SQL required.",
			},
		}

	def job_card(self, security_id, lid=""):
		response = self.job_detail(security_id.replace("sec-", "job-"))
		if not self.is_success(response):
			return response
		data = response["zpData"]
		info = data.get("jobInfo") or {}
		return {
			"code": 0,
			"zpData": {
				"jobCard": {
					"encryptJobId": info.get("encryptJobId", ""),
					"postDescription": data.get("jobDetail") or info.get("postDescription", ""),
				}
			},
		}

	def job_card_browser(self, security_id, lid=""):
		return self.job_card(security_id, lid)

	def greet(self, security_id, job_id, message):
		self.sent.append((security_id, job_id, message))
		if self.raise_on_send:
			raise TimeoutError("unknown result")
		if len(self.sent) == self.failed_number:
			return {"code": 36, "message": "stop"}
		return {
			"code": 0,
			"zpData": {
				"greeting_receipt": {
					"message_id": f"fixture-message-{len(self.sent)}",
					"recipient_id": "fixture-recipient",
					"job_id": job_id,
					"text_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
					"verification": "history_match",
				}
			},
		}

	def is_success(self, response):
		return response.get("code") == 0

	def parse_error(self, response):
		return "ACCOUNT_RISK", response.get("message", "stop")

	def unwrap_data(self, response):
		return response.get("zpData", {})


class FakeAI:
	def __init__(self):
		self.calls = []
		self.options = []

	def chat(self, messages, **kwargs):
		self.calls.append(messages)
		self.options.append(kwargs)
		data = json.loads(messages[1]["content"])
		return json.dumps(
			{
				"summary": data["jd"],
				"greeting": f"您好，对 {data['title']} 感兴趣，我有Python项目经历。",
				"resume_evidence": ["Python项目经历"],
				"warnings": [],
			}
		)


@pytest.fixture
def environment(tmp_path, monkeypatch):
	platform = FakePlatform()
	service = FakeAI()
	factory = Mock(return_value=platform)
	ai_factory = Mock(return_value=service)
	monkeypatch.setattr("boss_agent_cli.commands.delivery.get_platform_instance", factory)
	monkeypatch.setattr("boss_agent_cli.commands.delivery._require_ai_service", ai_factory)
	monkeypatch.setattr("boss_agent_cli.commands.delivery._load_resume_text", lambda *args: "本人有Python项目经历")
	monkeypatch.setattr("boss_agent_cli.commands.delivery.require_existing_boss_tab", lambda: None)
	return platform, service, factory, ai_factory


def invoke(tmp_path, *args):
	return CliRunner().invoke(cli, ["--data-dir", str(tmp_path), "--json", "--log-level", "error", "delivery", *args])


def prepare(tmp_path, count=3, no_ai=False):
	result = invoke(
		tmp_path, "prepare", "Python", "--count", str(count), *(["--no-ai"] if no_ai else ["--resume", "mine"])
	)
	assert result.exit_code == 0, result.output
	return json.loads(result.stdout)["data"]


def test_prepare_collects_fifty_full_jds_without_any_api_or_sends(tmp_path, environment):
	platform, service, _, ai_factory = environment
	data = prepare(tmp_path, count=50, no_ai=True)
	assert len(data["items"]) == 50
	assert platform.searches == [1, 2]
	assert len(platform.details) == 50
	assert platform.sent == []
	ai_factory.assert_not_called()
	assert service.calls == []
	assert data["counts"] == {"draft": 50}
	assert data["stop_reason"] == "target_reached"
	assert all(item["description"].startswith("Full JD") for item in data["items"])
	assert "security_id" not in data["items"][0]
	preview = (tmp_path / "delivery" / f"{data['batch_id']}.md").read_text(encoding="utf-8")
	assert "50." in preview and "Full JD for job-50" in preview


def test_prepare_generates_one_distinct_draft_per_job_but_never_sends(tmp_path, environment):
	platform, service, _, _ = environment
	data = prepare(tmp_path)
	assert data["counts"] == {"pending": 3}
	assert len(service.calls) == 3
	assert len({item["greeting"] for item in data["items"]}) == 3
	assert "本人有Python项目经历" in service.calls[0][1]["content"]
	assert platform.sent == []


def test_preview_explains_partial_batches_and_wraps_untrusted_text(tmp_path, environment):
	batch_id = prepare(tmp_path, count=1, no_ai=True)["batch_id"]
	with DeliveryStore(tmp_path) as store:
		batch = store.get(batch_id)
		payload = batch["items"][0]["payload"]
		payload.update(
			summary="Summary <script>bad</script>",
			greeting="A long greeting",
			warnings=["Check employer"],
			screening_notes=["Check employer", "Possible duplicate"],
		)
		store.save_draft(batch_id, 1, payload)
		store.progress(batch_id, batch["stats"], "source_environment_risk")
		write_preview(store, batch_id)
	preview = (tmp_path / "delivery" / f"{batch_id}.md").read_text(encoding="utf-8")
	assert "尚未达到目标数量" in preview
	assert "source_environment_risk" not in preview
	assert "\nA long greeting" in preview
	assert "<script>" not in preview
	assert preview.count("Check employer") == 1
	assert "Possible duplicate" in preview
	assert "sec-1" not in preview


def test_preview_shows_human_readable_saved_conditions(tmp_path):
	with DeliveryStore(tmp_path) as store:
		batch_id = store.create(
			{
				"count": 50,
				"rules": {
					"city": ["北京", "上海"],
					"min_salary": 15,
					"min_daily_salary": 400,
					"graduation_year": 2027,
					"bachelor_eligible": True,
					"max_internship_ratio": 0.2,
					"target_roles": ["ai_development", "product_manager"],
				},
			}
		)
		write_preview(store, batch_id)
	preview = (tmp_path / "delivery" / f"{batch_id}.md").read_text(encoding="utf-8")
	assert "城市：北京、上海" in preview
	assert "方向：AI开发、产品经理" in preview
	assert "实习日薪下限：400元/天" in preview
	assert "实习比例不超过实际入选数量的20%" in preview
	assert "max_internship_ratio" not in preview


def test_whole_batch_approval_except_rejections_and_limited_exact_send(tmp_path, environment, monkeypatch):
	platform, service, factory, _ = environment
	monkeypatch.setattr("boss_agent_cli.delivery.time.sleep", lambda seconds: None)
	data = prepare(tmp_path)
	batch_id = data["batch_id"]
	result = invoke(tmp_path, "review", batch_id, "--approve-all", "--reject", "2")
	assert result.exit_code == 0, result.output
	assert json.loads(result.stdout)["data"]["counts"] == {"approved": 2, "rejected": 1}
	assert platform.sent == []
	factory.reset_mock()
	assert invoke(tmp_path, "send", batch_id).exit_code == 0
	factory.assert_not_called()
	assert invoke(tmp_path, "send", batch_id, "--execute", "--limit", "1").exit_code == 0
	assert platform.sent == [("sec-1", "job-1", data["items"][0]["greeting"])]
	assert invoke(tmp_path, "send", batch_id, "--execute").exit_code == 0
	assert [call[1] for call in platform.sent] == ["job-1", "job-3"]
	assert invoke(tmp_path, "send", batch_id, "--execute").exit_code == 0
	assert len(platform.sent) == 2
	assert len(service.calls) == 3
	with CacheStore(tmp_path / "cache" / "boss_agent.db") as cache:
		assert cache.is_contacted_job("job-1")
		assert not cache.is_contacted_job("job-2")


def test_no_approval_no_outreach_even_with_execute(tmp_path, environment):
	platform, _, factory, _ = environment
	data = prepare(tmp_path)
	factory.reset_mock()
	assert invoke(tmp_path, "send", data["batch_id"], "--execute").exit_code == 0
	factory.assert_not_called()
	assert not platform.sent


def test_missing_selected_tab_stops_before_claiming_send(tmp_path, environment, monkeypatch):
	from boss_agent_cli.api.browser_source import BrowserSessionNotFound

	batch_id = prepare(tmp_path, count=1)["batch_id"]
	assert invoke(tmp_path, "review", batch_id, "--approve-all").exit_code == 0
	check = Mock(side_effect=BrowserSessionNotFound("selected tab closed"))
	monkeypatch.setattr("boss_agent_cli.commands.delivery.require_existing_boss_tab", check)
	environment[2].reset_mock()
	result = invoke(tmp_path, "send", batch_id, "--execute")
	assert result.exit_code == 1
	assert json.loads(result.stdout)["error"]["code"] == "BROWSER_SESSION_NOT_FOUND"
	environment[2].assert_not_called()
	with DeliveryStore(tmp_path) as store:
		assert not store.attempted("job-1")
		assert store.get(batch_id)["items"][0]["status"] == "approved"
	assert not environment[0].sent


def test_rule_only_batch_cannot_be_approved_until_drafted(tmp_path, environment):
	data = prepare(tmp_path, no_ai=True)
	result = invoke(tmp_path, "review", data["batch_id"], "--approve-all", "--reject", "2")
	assert result.exit_code == 1
	with DeliveryStore(tmp_path) as store:
		assert {item["status"] for item in store.get(data["batch_id"])["items"]} == {"draft"}
	assert invoke(tmp_path, "draft", data["batch_id"], "--resume", "mine").exit_code == 0
	assert invoke(tmp_path, "review", data["batch_id"], "--approve-all").exit_code == 0


def test_missing_ai_configuration_fails_before_search(tmp_path, environment, monkeypatch):
	from boss_agent_cli.commands.ai_cmd import _require_ai_service

	monkeypatch.setattr("boss_agent_cli.commands.delivery._require_ai_service", _require_ai_service)
	monkeypatch.setattr("boss_agent_cli.commands.ai_cmd._create_ai_service", lambda ctx: None)
	result = invoke(tmp_path, "prepare", "Python", "--resume", "mine")
	assert result.exit_code == 1
	assert json.loads(result.stdout)["error"]["code"] == "AI_NOT_CONFIGURED"
	environment[2].assert_not_called()


def test_insufficient_results_are_not_padded(tmp_path, environment):
	environment[0].total = 2
	data = prepare(tmp_path, count=50, no_ai=True)
	assert len(data["items"]) == 2
	assert data["stop_reason"] == "results_exhausted"


def test_exclusions_and_missing_jd_are_replaced_by_later_jobs(tmp_path, environment, monkeypatch):
	platform = environment[0]
	original = platform.job_detail

	def detail(job_id):
		response = original(job_id)
		if job_id == "job-1":
			response["zpData"]["jobDetail"] = ""
		if job_id == "job-2":
			response["zpData"]["jobDetail"] += " 外包"
		return response

	monkeypatch.setattr(platform, "job_detail", detail)
	result = invoke(
		tmp_path, "prepare", "Python", "--no-ai", "--count", "3", "--require", "Python", "--exclude", "外包"
	)
	assert result.exit_code == 0, result.output
	data = json.loads(result.stdout)["data"]
	assert [item["job_id"] for item in data["items"]] == ["job-3", "job-4", "job-5"]
	assert len(data["stats"]["excluded"]) == 2


@pytest.mark.parametrize(
	"rules, job_data, expected",
	[
		({"city": ["北京"]}, {"city": "上海"}, True),
		({"city": ["北京"]}, {}, True),
		({"city": ["北京"]}, {"city": "北京"}, False),
		({"min_salary": 20}, {"salary": "15-30K"}, True),
		({"min_salary": 20}, {"salary": "面议"}, True),
		({"min_salary": 20}, {"salary": "20-30K"}, False),
		({"min_salary": 20}, {"salary": "20K/天"}, True),
		({"experience": ["经验不限", "1-3年"]}, {"experience": "5-10年"}, True),
		({"education": ["本科"]}, {"education": "硕士"}, True),
		({"exclude_company": ["中介"]}, {"company": "某中介公司"}, True),
		({"require": ["Python", "SQL"]}, {"description": "Python only"}, True),
		({"require": ["Python"]}, {"description": "python developer"}, False),
	],
)
def test_deterministic_rules(rules, job_data, expected):
	assert bool(filter_job(job_data, rules, full=True)[0]) == expected


def test_greeted_job_is_excluded_across_rotating_security_ids_but_apply_only_is_kept(tmp_path, environment):
	with CacheStore(tmp_path / "cache" / "boss_agent.db") as cache:
		cache.record_greet("old-security", "job-1")
		cache.record_apply("old-security-2", "job-2")
	data = prepare(tmp_path, no_ai=True)
	assert [item["job_id"] for item in data["items"]] == ["job-2", "job-3", "job-4"]


def test_budget_stops_detail_requests(tmp_path, environment):
	result = invoke(tmp_path, "prepare", "Python", "--no-ai", "--max-details", "2")
	assert result.exit_code == 0
	data = json.loads(result.stdout)["data"]
	assert len(data["items"]) == 2
	assert data["stop_reason"] == "detail_limit"
	assert len(environment[0].details) == 2


def test_risk_response_stops_collection_without_fallback(tmp_path, environment, monkeypatch):
	platform = environment[0]
	monkeypatch.setattr(platform, "job_detail", lambda job_id: {"code": 36, "message": "stop"})
	result = invoke(tmp_path, "prepare", "Python", "--no-ai")
	assert result.exit_code == 1
	assert json.loads(result.stdout)["error"]["code"] == "ACCOUNT_RISK"
	assert platform.searches == [1]
	with DeliveryStore(tmp_path) as store:
		batch = store.get(store.list_batches()[0]["id"])
		assert batch["stop_reason"] == "interrupted_or_error"
		assert not batch["items"]


@pytest.mark.parametrize("approve,reject", [("1,99", ""), ("1", "1"), ("1", "-1"), ("x", "")])
def test_invalid_review_is_atomic(tmp_path, environment, approve, reject):
	data = prepare(tmp_path)
	assert invoke(tmp_path, "review", data["batch_id"], "--approve", approve, "--reject", reject).exit_code == 1
	with DeliveryStore(tmp_path) as store:
		assert {item["status"] for item in store.get(data["batch_id"])["items"]} == {"pending"}


def test_approve_all_preserves_previous_rejections(tmp_path, environment):
	batch_id = prepare(tmp_path)["batch_id"]
	assert invoke(tmp_path, "review", batch_id, "--reject", "2").exit_code == 0
	assert invoke(tmp_path, "review", batch_id, "--approve-all").exit_code == 0
	with DeliveryStore(tmp_path) as store:
		assert store.get(batch_id)["items"][1]["status"] == "rejected"


def test_platform_write_failure_stops_batch_and_records_partial_success(tmp_path, environment, monkeypatch):
	platform = environment[0]
	platform.failed_number = 2
	monkeypatch.setattr("boss_agent_cli.delivery.time.sleep", lambda seconds: None)
	batch_id = prepare(tmp_path)["batch_id"]
	assert invoke(tmp_path, "review", batch_id, "--approve-all").exit_code == 0
	result = invoke(tmp_path, "send", batch_id, "--execute")
	assert result.exit_code == 1
	assert len(platform.sent) == 2
	with DeliveryStore(tmp_path) as store:
		assert [item["status"] for item in store.get(batch_id)["items"]] == ["sent", "failed", "approved"]


def test_uncertain_attempt_is_never_retried(tmp_path, environment):
	platform = environment[0]
	platform.raise_on_send = True
	batch_id = prepare(tmp_path, count=1)["batch_id"]
	assert invoke(tmp_path, "review", batch_id, "--approve-all").exit_code == 0
	assert invoke(tmp_path, "send", batch_id, "--execute").exit_code == 1
	assert invoke(tmp_path, "send", batch_id, "--execute").exit_code == 0
	assert len(platform.sent) == 1
	with DeliveryStore(tmp_path) as store:
		assert store.get(batch_id)["items"][0]["status"] == "uncertain"


def test_crash_after_claim_and_concurrent_claim_do_not_resend(tmp_path, environment):
	batch_id = prepare(tmp_path, count=1)["batch_id"]
	assert invoke(tmp_path, "review", batch_id, "--approve-all").exit_code == 0
	with DeliveryStore(tmp_path) as first, DeliveryStore(tmp_path) as second:
		assert first.claim(batch_id, 1) is not None
		assert second.claim(batch_id, 1) is None
	assert invoke(tmp_path, "send", batch_id, "--execute").exit_code == 0
	assert environment[0].sent == []


def test_approved_snapshot_cannot_be_changed_before_sending(tmp_path, environment):
	batch_id = prepare(tmp_path, count=1)["batch_id"]
	assert invoke(tmp_path, "review", batch_id, "--approve-all").exit_code == 0
	with DeliveryStore(tmp_path) as store:
		payload = store.get(batch_id)["items"][0]["payload"]
		payload["greeting"] = "changed"
		with store.db:
			store.db.execute("UPDATE items SET payload = ? WHERE batch_id = ?", (json.dumps(payload), batch_id))
	assert invoke(tmp_path, "send", batch_id, "--execute").exit_code == 1
	assert not environment[0].sent


@pytest.mark.parametrize(
	"payload",
	[
		[],
		{"summary": "", "greeting": "hi"},
		{"summary": "summary", "greeting": "hi", "warnings": [], "resume_evidence": ["invented"]},
	],
)
def test_invalid_ai_output_never_enters_approval(payload):
	service = Mock()
	service.chat.return_value = json.dumps(payload)
	with pytest.raises(DeliveryError):
		generate_draft(service, {"title": "engineer", "company": "Example", "description": "Python"}, "Python项目经历")


def test_draft_receives_verified_job_facts_without_browser_credentials():
	service = FakeAI()
	job_data = {
		"title": "AI engineer",
		"company": "Example",
		"description": "Python",
		"city": "北京",
		"salary": "20-30K",
		"education": "本科",
		"experience": "在校/应届",
		"recruitment_kind": "campus",
		"rule_matches": ["city=北京", "月薪下限:20000元"],
		"security_id": "private-security-id",
		"lid": "private-request-context",
	}
	generate_draft(service, job_data, "Python项目经历")
	prompt = service.calls[0][1]["content"]
	assert json.loads(prompt)["job_facts"] == {
		key: job_data[key] for key in ("city", "salary", "education", "experience", "recruitment_kind", "rule_matches")
	}
	assert "private-security-id" not in prompt
	assert "private-request-context" not in prompt
	assert service.options == [{"response_format": {"type": "json_object"}, "temperature": 0.2}]


@pytest.mark.parametrize("raw", [None, "", "  ", 42])
def test_empty_or_non_text_ai_output_stays_out_of_approval(raw):
	service = Mock()
	service.chat.return_value = raw
	with pytest.raises(DeliveryError, match="空内容或非文本"):
		generate_draft(service, {"title": "AI engineer", "company": "Example", "description": "Python"}, "Python")


def test_draft_retries_invalid_json_once_without_platform_access(tmp_path, environment, monkeypatch):
	batch_id = prepare(tmp_path, count=1, no_ai=True)["batch_id"]
	platform, service, factory, _ = environment
	original = service.chat
	attempts = []

	def unstable(messages, **kwargs):
		attempts.append(messages)
		return "not JSON" if len(attempts) == 1 else original(messages, **kwargs)

	monkeypatch.setattr(service, "chat", unstable)
	factory.reset_mock()
	result = invoke(tmp_path, "draft", batch_id, "--resume", "mine")
	assert result.exit_code == 0, result.output
	assert len(attempts) == 2
	factory.assert_not_called()
	assert not platform.sent
	with DeliveryStore(tmp_path) as store:
		assert store.get(batch_id)["items"][0]["status"] == "pending"


def test_invalid_json_retry_is_bounded_and_does_not_save_draft(tmp_path, environment, monkeypatch):
	batch_id = prepare(tmp_path, count=1, no_ai=True)["batch_id"]
	service = environment[1]
	chat = Mock(return_value="not JSON")
	monkeypatch.setattr(service, "chat", chat)
	assert invoke(tmp_path, "draft", batch_id, "--resume", "mine").exit_code == 1
	assert chat.call_count == 2
	with DeliveryStore(tmp_path) as store:
		assert store.get(batch_id)["items"][0]["status"] == "draft"
	assert not environment[0].sent


def test_retry_draft_does_not_overwrite_approved_message(tmp_path, environment):
	batch_id = prepare(tmp_path, count=1)["batch_id"]
	assert invoke(tmp_path, "review", batch_id, "--approve-all").exit_code == 0
	assert invoke(tmp_path, "draft", batch_id, "--resume", "mine").exit_code == 0
	assert len(environment[1].calls) == 1


def test_hook_can_veto_before_claim_or_send(tmp_path, environment):
	batch_id = prepare(tmp_path, count=1)["batch_id"]
	assert invoke(tmp_path, "review", batch_id, "--approve-all").exit_code == 0
	hooks = Mock()
	hooks.greet_before.call.return_value = "blocked"
	with DeliveryStore(tmp_path) as store, CacheStore(tmp_path / "cache" / "boss_agent.db") as cache:
		with pytest.raises(DeliveryError):
			send_batch(store, batch_id, environment[0], cache, limit=1, interval=2, hooks=hooks)
		assert not store.attempted("job-1")
	assert not environment[0].sent


def test_two_prepared_batches_cannot_send_the_same_job_twice(tmp_path, environment):
	first = prepare(tmp_path, count=1)["batch_id"]
	second = prepare(tmp_path, count=1)["batch_id"]
	for batch_id in (first, second):
		assert invoke(tmp_path, "review", batch_id, "--approve-all").exit_code == 0
	with DeliveryStore(tmp_path) as store:
		assert store.claim(first, 1) is not None
		assert store.claim(second, 1) is None
		assert store.get(second)["items"][0]["status"] == "skipped"
	assert not environment[0].sent


def test_contacted_skip_does_not_overwrite_a_concurrent_success(tmp_path, environment):
	batch_id = prepare(tmp_path, count=1)["batch_id"]
	assert invoke(tmp_path, "review", batch_id, "--approve-all").exit_code == 0
	with DeliveryStore(tmp_path) as store:
		payload = store.claim(batch_id, 1)
		receipt = FakePlatform().greet(payload["security_id"], payload["job_id"], payload["greeting"])["zpData"]["greeting_receipt"]
		store.finish(batch_id, 1, "sent", receipt=receipt)
		store.skip_contacted(batch_id, 1)
		assert store.get(batch_id)["items"][0]["status"] == "sent"


def test_preview_and_review_do_not_access_platform_or_api(tmp_path, environment):
	batch_id = prepare(tmp_path)["batch_id"]
	_, _, platform_factory, ai_factory = environment
	platform_factory.reset_mock()
	ai_factory.reset_mock()
	assert invoke(tmp_path, "preview", batch_id).exit_code == 0
	assert invoke(tmp_path, "review", batch_id, "--approve-all").exit_code == 0
	platform_factory.assert_not_called()
	ai_factory.assert_not_called()


def test_privacy_clean_includes_batch_snapshots_and_drafts(tmp_path, environment):
	prepare(tmp_path, count=1)
	args = ["--data-dir", str(tmp_path), "--json", "clean", "--privacy"]
	result = CliRunner().invoke(cli, [*args, "--dry-run"])
	assert result.exit_code == 0, result.output
	assert (tmp_path / "delivery" / "batches.db").exists()
	result = CliRunner().invoke(cli, args)
	assert result.exit_code == 0, result.output
	assert not (tmp_path / "delivery").exists()


def test_invalid_platform_is_blocked_before_any_remote_call(tmp_path, environment):
	result = CliRunner().invoke(
		cli,
		["--data-dir", str(tmp_path), "--json", "--platform", "zhilian", "delivery", "prepare", "Python", "--no-ai"],
	)
	assert result.exit_code == 1
	assert json.loads(result.stdout)["error"]["code"] == "NOT_SUPPORTED"
	environment[2].assert_not_called()
