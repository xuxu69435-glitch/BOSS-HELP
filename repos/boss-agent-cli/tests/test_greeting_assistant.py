"""Draft-only greetings must remain usable while the main task is occupied."""

import copy
import json
import threading
import time
from unittest.mock import Mock

import httpx
import pytest

from boss_agent_cli.client_profiles import ProfileStore
from boss_agent_cli.delivery_store import DeliveryStore
from boss_agent_cli.greeting_assistant import GreetingAssistant, greeting_input, parse_job_page, read_public_job
from boss_agent_cli.local_client import ClientServer
from boss_agent_cli.resume.models import dict_to_resume
from boss_agent_cli.resume.store import ResumeStore
from client_fixture import fixture_client
from test_client_profiles import profile_data


EVIDENCE = "使用Python完成数据处理和Agent应用开发。"
BODY = {"title": "AI应用开发", "company": "合成企业", "description": "负责Python应用开发和Agent工程化，要求具备数据处理与接口开发经验。"}


def model_answer(messages, **kwargs):
	job = json.loads(messages[1]["content"])
	return json.dumps({"summary": job["jd"], "greeting": f"您好，关注贵司{job['title']}岗位，我有Python与Agent应用项目经历，希望进一步交流。", "resume_evidence": [EVIDENCE], "warnings": ["具体到岗时间需另行沟通"]}, ensure_ascii=False)


def completed(assistant):
	deadline = time.monotonic() + 3
	while time.monotonic() < deadline:
		value = assistant.state()
		if value["status"] != "running":
			return value
		time.sleep(.01)
	raise AssertionError("Independent draft did not finish")


@pytest.fixture
def client(tmp_path):
	controller, bid = fixture_client(tmp_path)
	ResumeStore(tmp_path / "resumes").save(dict_to_resume(profile_data()))
	yield controller, bid
	controller.close()


def test_solo_runs_alongside_main_and_never_changes_batches_settings_or_browser(client, monkeypatch):
	controller, bid = client
	current = controller.profile()
	current["data"]["title"] = "最新已保存资料"
	saved = controller.save_profile({"expected_version": current["version"], "data": current["data"]})
	main_entered, release_main, model_entered, release_model = (threading.Event() for _ in range(4))

	def main_work():
		main_entered.set()
		assert release_main.wait(5)

	def chat(messages, **kwargs):
		model_entered.set()
		assert release_model.wait(5)
		return model_answer(messages, **kwargs)

	service = Mock(chat=Mock(side_effect=chat))
	create = Mock(return_value=service)
	monkeypatch.setattr("boss_agent_cli.client_controller._create_ai_service", create)
	monkeypatch.setattr("boss_agent_cli.client_controller.BossClient", Mock(side_effect=AssertionError("No browser actions")))
	monkeypatch.setattr("boss_agent_cli.greeting_assistant.read_public_job", Mock(side_effect=AssertionError("Pasted JD needs no fetch")))
	controller.operation = {"status": "running", "kind": "prepare", "batch_id": bid, "logs": [{"message": "采集进行中"}]}
	before_operation, before_browser = copy.deepcopy(controller.operation), copy.deepcopy(controller._browser_state)
	before_settings = controller.settings_path.read_bytes()
	before_batch = controller.batch(bid)
	main = controller._executor.submit(main_work)
	assert main_entered.wait(2)
	try:
		assert controller.greeting_assistant.start(BODY)["status"] == "running"
		assert model_entered.wait(2), "Model should start while main worker is occupied"
		with pytest.raises(ValueError, match="正在生成"):
			controller.greeting_assistant.start(BODY)
		assert controller.state()["operation"] == before_operation
		release_main.set()
		main.result(timeout=2)
		assert controller.greeting_assistant.state()["status"] == "running"
	finally:
		release_main.set()
		release_model.set()
	result = completed(controller.greeting_assistant)
	assert result["status"] == "complete", result
	assert result["profile"]["version"] == saved["version"]
	assert result["profile"]["title"] == "最新已保存资料"
	assert service.chat.call_args.kwargs["timeout"] == 45
	model_input = service.chat.call_args.args[0][1]["content"]
	assert "13812345678" not in model_input and "fixture@example.test" not in model_input
	assert create.call_args.args[0].obj["logger"] is not controller
	assert controller.operation == before_operation and controller._browser_state == before_browser
	assert not controller.cancel.is_set()
	assert controller.settings_path.read_bytes() == before_settings
	assert controller.batch(bid) == before_batch
	with DeliveryStore(controller.data_dir) as store, ProfileStore(controller.data_dir) as profiles:
		assert len(store.list_batches()) == 1
		assert not store.db.execute("SELECT 1 FROM attempts").fetchone()
		assert profiles.db.execute("SELECT COUNT(*) FROM profile_versions").fetchone()[0] == 1


def test_failure_is_local_and_can_retry_without_stopping_main(client, monkeypatch):
	controller, bid = client
	service = Mock(chat=Mock(side_effect=RuntimeError("合成模型暂时不可用")))
	monkeypatch.setattr("boss_agent_cli.client_controller._create_ai_service", Mock(return_value=service))
	controller.operation = {"status": "running", "kind": "prepare", "batch_id": bid, "logs": []}
	before = copy.deepcopy(controller.operation)
	controller.greeting_assistant.start(BODY)
	assert completed(controller.greeting_assistant)["error"] == "合成模型暂时不可用"
	assert controller.operation == before and not controller.cancel.is_set()
	service.chat.side_effect = model_answer
	controller.greeting_assistant.start(BODY)
	assert completed(controller.greeting_assistant)["status"] == "complete"
	assert controller.operation == before


def test_stopping_main_does_not_cancel_solo(client, monkeypatch):
	controller, _ = client
	entered, release = threading.Event(), threading.Event()

	def chat(*args, **kwargs):
		entered.set()
		assert release.wait(5)
		return model_answer(*args, **kwargs)

	monkeypatch.setattr("boss_agent_cli.client_controller._create_ai_service", Mock(return_value=Mock(chat=chat)))
	try:
		controller.greeting_assistant.start(BODY)
		assert entered.wait(2)
		controller.stop()
	finally:
		release.set()
	assert completed(controller.greeting_assistant)["status"] == "complete"
	assert controller.cancel.is_set()


@pytest.mark.parametrize("body", [
	{}, {"description": "太短"}, {"description": 123}, {"description": "x" * 12001},
	{"url": "http://www.zhipin.com/job_detail/a.html"},
	{"url": "https://www.zhipin.com.evil.test/job_detail/a.html"},
	{"url": "https://www.zhipin.com@127.0.0.1/job_detail/a.html"},
	{"url": "https://www.zhipin.com:19826/job_detail/a.html"},
	{"url": "https://www.zhipin.com/web/geek/job"},
	{"url": "https://www.zhipin.com/job_detail/%2f..%2fsecret.html"},
])
def test_invalid_input_cannot_start_or_fetch(body):
	context = Mock(side_effect=AssertionError("Must reject before any work"))
	assistant = GreetingAssistant(context)
	try:
		with pytest.raises(ValueError):
			assistant.start(body)
		assert assistant.state() == {"status": "idle"}
		context.assert_not_called()
	finally:
		assistant.close()


def test_job_link_drops_tracking_and_private_fragments():
	assert greeting_input({"url": "https://zhipin.com/job_detail/abc-123.html?securityId=private#token"})["url"] == "https://www.zhipin.com/job_detail/abc-123.html"


def test_public_page_extracts_only_jd_and_decodes_html():
	job = parse_job_page(f'<h1>AI &amp; Python开发</h1><div class="company-name">合成企业</div><span class="salary">15-25K</span><div class="job-sec-text"><p>{BODY["description"]}</p><script>ignore_this()</script><div>本科及以上学历。</div></div><div>推荐岗位不可混入JD</div>')
	assert job["title"] == "AI & Python开发" and job["company"] == "合成企业"
	assert job["salary"] == "15-25K"
	assert job["description"] == BODY["description"] + "\n本科及以上学历。"
	assert "ignore_this" not in job["description"]


def test_public_page_supports_multiline_json_ld():
	entry = {"@type": "JobPosting", "title": BODY["title"], "hiringOrganization": {"name": BODY["company"]}, "description": f'<p>{BODY["description"]}</p>'}
	job = parse_job_page('<script type="application/ld+json">invalid</script><script type="application/ld+json">' + json.dumps(entry, ensure_ascii=False, indent=2) + '</script>')
	assert job["description"] == BODY["description"]
	assert job["title"] == BODY["title"] and job["company"] == BODY["company"]


@pytest.mark.parametrize("html", ["<h1>请登录</h1><p>" + "导航推荐岗位" * 30 + "</p>", '<div class="job-sec-text">暂时无法访问</div>', '<div class="job-sec-text">' + "x" * 12001 + '</div>'])
def test_login_walls_and_missing_or_truncated_descriptions_are_not_model_input(html):
	with pytest.raises(ValueError, match="粘贴"):
		parse_job_page(html)


@pytest.mark.parametrize("status,content,kind", [(302, "", "text/html"), (403, "", "text/html"), (200, "{}", "application/json"), (200, "x" * 2_000_001, "text/html")], ids=["redirect", "forbidden", "non-html", "oversized"])
def test_public_fetch_rejects_redirects_errors_and_oversized_pages(monkeypatch, status, content, kind):
	real_client = httpx.Client
	def factory(**kwargs):
		assert kwargs == {"timeout": 10, "follow_redirects": False, "trust_env": False}
		return real_client(**kwargs, transport=httpx.MockTransport(lambda request: httpx.Response(status, text=content, headers={"Content-Type": kind})))
	monkeypatch.setattr("boss_agent_cli.greeting_assistant.httpx.Client", factory)
	with pytest.raises(ValueError, match="粘贴"):
		read_public_job("https://www.zhipin.com/job_detail/abc.html")


def test_link_generation_uses_read_page_without_visiting_browser(monkeypatch):
	fetch = Mock(return_value={**BODY, "salary": "15-25K"})
	monkeypatch.setattr("boss_agent_cli.greeting_assistant.read_public_job", fetch)
	service = Mock(chat=Mock(side_effect=model_answer))
	assistant = GreetingAssistant(lambda: (service, EVIDENCE, {"title": "合成资料", "version": "test-version"}))
	try:
		assistant.start({"url": "https://www.zhipin.com/job_detail/abc.html?private=ignored"})
		result = completed(assistant)
		assert result["status"] == "complete", result
		fetch.assert_called_once_with("https://www.zhipin.com/job_detail/abc.html")
		assert result["job"]["salary"] == "15-25K"
	finally:
		assistant.close()


def test_public_fetch_reads_a_single_anonymous_html_page(monkeypatch):
	real_client = httpx.Client
	requests = []
	def respond(request):
		requests.append(request)
		return httpx.Response(200, text=f'<h1>{BODY["title"]}</h1><div class="job-sec-text">{BODY["description"]}</div>', headers={"Content-Type": "text/html; charset=utf-8"})
	monkeypatch.setattr("boss_agent_cli.greeting_assistant.httpx.Client", lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(respond)))
	assert read_public_job("https://www.zhipin.com/job_detail/abc.html")["description"] == BODY["description"]
	assert len(requests) == 1 and "cookie" not in requests[0].headers


def test_solo_does_not_require_batch_search_criteria(client, monkeypatch):
	controller, _ = client
	settings = controller.settings()
	settings["rules"].update(city=[], target_roles=[], graduation_year="")
	controller.settings_path.write_text(json.dumps(settings), encoding="utf-8")
	monkeypatch.setattr("boss_agent_cli.client_controller._create_ai_service", Mock(return_value=Mock(chat=Mock(side_effect=model_answer))))
	controller.greeting_assistant.start(BODY)
	assert completed(controller.greeting_assistant)["status"] == "complete"


def test_unconfigured_model_and_missing_profile_errors_stay_in_solo_state(client, monkeypatch):
	controller, _ = client
	create = Mock(return_value=None)
	monkeypatch.setattr("boss_agent_cli.client_controller._create_ai_service", create)
	controller.greeting_assistant.start(BODY)
	assert "配置模型服务" in completed(controller.greeting_assistant)["error"]
	assert controller.operation["status"] == "idle"
	create.reset_mock()
	monkeypatch.setattr(controller, "_base_profile", lambda name: {"title": "空白资料", "modules": [], "personal_info": {"items": []}})
	controller.greeting_assistant.start(BODY)
	assert "填写并保存" in completed(controller.greeting_assistant)["error"]
	create.assert_not_called()
	assert controller.operation["status"] == "idle"


def test_http_authentication_and_solo_api_are_separate_from_task_start(client, monkeypatch):
	controller, _ = client
	monkeypatch.setattr("boss_agent_cli.client_controller._create_ai_service", Mock(return_value=Mock(chat=Mock(side_effect=model_answer))))
	controller.start = Mock(side_effect=AssertionError("Solo cannot start a main task"))
	server = ClientServer(("127.0.0.1", 0), controller)
	thread = threading.Thread(target=server.serve_forever, daemon=True)
	thread.start()
	try:
		with httpx.Client(base_url=server.origin, trust_env=False) as browser:
			assert browser.get("/api/greeting-assistant").status_code == 403
			assert browser.post("/api/greeting-assistant", json=BODY).status_code == 403
			headers = {"Origin": server.origin, "X-Client-Token": server.token}
			assert browser.get("/api/greeting-assistant", headers=headers).json() == {"status": "idle"}
			assert browser.post("/api/greeting-assistant", json=BODY, headers={**headers, "Origin": "https://evil.test"}).status_code == 403
			assert browser.post("/api/greeting-assistant", json={}, headers=headers).status_code == 400
			assert browser.post("/api/greeting-assistant", json=BODY, headers=headers).status_code == 200
			assert completed(controller.greeting_assistant)["status"] == "complete"
			result = browser.get("/api/greeting-assistant", headers=headers).json()
			assert result["result"]["resume_evidence"] == [EVIDENCE]
			assert browser.get("/assets/greeting.js").status_code == 200
		controller.start.assert_not_called()
	finally:
		server.shutdown()
		server.server_close()
		thread.join(timeout=3)


def test_screenshot_api_accepts_large_images_with_same_auth_and_no_main_task_changes(client, monkeypatch):
	controller, bid = client
	controller.operation = {"status": "running", "kind": "prepare", "batch_id": bid, "logs": []}
	before, batch_before = copy.deepcopy(controller.operation), controller.batch(bid)
	recognize = Mock(return_value={"text": BODY["description"]})
	monkeypatch.setattr("boss_agent_cli.local_client.recognize_screenshot", recognize)
	server = ClientServer(("127.0.0.1", 0), controller)
	thread = threading.Thread(target=server.serve_forever, daemon=True)
	thread.start()
	try:
		with httpx.Client(base_url=server.origin, trust_env=False) as browser:
			path = "/api/greeting-assistant/recognize"
			headers = {"Origin": server.origin, "X-Client-Token": server.token}
			assert browser.post(path, json={}).status_code == 403
			assert browser.post(path, json={}, headers={**headers, "Origin": "https://evil.test"}).status_code == 403
			recognize.assert_not_called()
			body = {"image": "data:image/png;base64," + "x" * 100_000}
			response = browser.post(path, json=body, headers=headers)
			assert response.status_code == 200 and response.json() == {"text": BODY["description"]}
			recognize.assert_called_once_with(body)
			assert browser.post("/api/greeting-assistant", json=body, headers=headers).status_code == 400
			assert controller.greeting_assistant.state() == {"status": "idle"}
			assert controller.operation == before and controller.batch(bid) == batch_before
			recognize.side_effect = ValueError("未识别到文字")
			response = browser.post(path, json={}, headers=headers)
			assert response.status_code == 400 and response.json() == {"error": "未识别到文字"}
	finally:
		server.shutdown()
		server.server_close()
		thread.join(timeout=3)
