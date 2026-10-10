"""Editable local profiles, immutable batch versions, and configurable filter rules."""

import copy
from unittest.mock import Mock

import pytest

from boss_agent_cli.client_profiles import ProfileStore, profile_model_text, validate_profile
from boss_agent_cli.client_controller import search_plan
from boss_agent_cli.delivery import filter_job
from boss_agent_cli.delivery_store import DeliveryStore
from boss_agent_cli.resume.models import dict_to_resume
from boss_agent_cli.resume.store import ResumeStore
from client_fixture import fixture_client


def profile_data(name="test"):
	return {
		"name": name, "title": "测试同学", "center_title": False,
		"personal_info": {"items": [
			{"label": "学校", "value": "测试大学", "icon": "", "link": ""},
			{"label": "手机", "value": "13812345678", "icon": "", "link": ""},
			{"label": "邮箱", "value": "fixture@example.test", "icon": "", "link": ""},
			{"label": "作品", "value": "个人项目", "icon": "", "link": "https://example.test/work"},
		], "layout": "inline"},
		"job_intention": {"title": "实习安排", "icon": "", "show_background": True, "items": [{"label": "每周可实习天数", "value": "4"}]},
		"modules": [{"id": "project", "title": "项目经历", "icon": "", "rows": [{"type": "richtext", "content": ["使用Python完成数据处理和Agent应用开发。"]}]}],
		"avatar": "", "created_at": "2026-10-06", "updated_at": "2026-10-06",
	}


@pytest.fixture
def profile_client(tmp_path):
	controller, bid = fixture_client(tmp_path)
	ResumeStore(tmp_path / "resumes").save(dict_to_resume(profile_data()))
	yield controller, bid
	controller.close()


def test_profile_load_and_save_are_local_and_leave_source_and_approvals_unchanged(profile_client, monkeypatch):
	controller, bid = profile_client
	monkeypatch.setattr("boss_agent_cli.client_controller.BossClient", Mock(side_effect=AssertionError("No BOSS call")))
	monkeypatch.setattr("boss_agent_cli.client_controller._create_ai_service", Mock(side_effect=AssertionError("No model call")))
	source = controller.data_dir / "resumes/test.json"
	original_file = source.read_bytes()
	with DeliveryStore(controller.data_dir) as store:
		store.review(bid, approve={1}, reject=set())
		old_items = store.get(bid)["items"]
	current = controller.profile()
	with ProfileStore(controller.data_dir) as store:
		assert store.db.execute("SELECT COUNT(*) FROM profile_versions").fetchone()[0] == 0
	current["data"]["modules"].append({"id": "availability", "title": "补充经历", "icon": "", "rows": [{"type": "richtext", "content": ["可以连续实习六个月。"]}]})
	saved = controller.save_profile({"expected_version": current["version"], "data": current["data"]})
	assert saved["version"] != current["version"]
	assert "可以连续实习六个月" in saved["model_text"]
	assert "13812345678" not in saved["model_text"] and "fixture@example.test" not in saved["model_text"]
	assert "https://example.test/work" in saved["model_text"]
	assert source.read_bytes() == original_file
	with DeliveryStore(controller.data_dir) as store:
		assert store.get(bid)["items"] == old_items
		assert not store.db.execute("SELECT 1 FROM attempts").fetchone()
	assert controller.profile()["data"] == saved["data"]


def test_stale_profile_cannot_overwrite_newer_save(profile_client):
	controller, _ = profile_client
	old = controller.profile()
	controller.save_profile({"expected_version": old["version"], "data": old["data"]})
	with pytest.raises(ValueError, match="已有更新"):
		controller.save_profile({"expected_version": old["version"], "data": old["data"]})
	with ProfileStore(controller.data_dir) as store:
		assert store.db.execute("SELECT COUNT(*) FROM profile_versions").fetchone()[0] == 1


def test_batch_profile_snapshot_is_immutable_and_legacy_batches_use_original(profile_client, monkeypatch):
	controller, _ = profile_client
	monkeypatch.setattr("boss_agent_cli.client_controller._create_ai_service", Mock(return_value=Mock()))
	legacy = controller.settings()
	snapshot = copy.deepcopy(legacy)
	controller._snapshot_profile(snapshot)
	old_version = snapshot["profile_version"]
	current = controller.profile()
	current["data"]["modules"][0]["rows"][0]["content"] = ["新增机器人控制项目经历。"]
	new = controller.save_profile({"expected_version": current["version"], "data": current["data"]})
	_, old_text = controller._service_and_resume(snapshot)
	_, legacy_text = controller._service_and_resume(legacy)
	future = copy.deepcopy(legacy)
	controller._snapshot_profile(future)
	_, new_text = controller._service_and_resume(future)
	assert old_version != new["version"] == future["profile_version"]
	assert "机器人控制" not in old_text and "机器人控制" not in legacy_text
	assert "机器人控制" in new_text
	with ProfileStore(controller.data_dir) as store:
		with pytest.raises(ValueError, match="不存在"):
			store.get("another-resume", old_version)


@pytest.mark.parametrize("mutation", [
	lambda data: data.update(name="../other"),
	lambda data: data.update(title=""),
	lambda data: data.update(modules="wrong"),
	lambda data: data.update(unknown="bad"),
	lambda data: data["personal_info"]["items"][0].update(link="javascript:alert(1)"),
	lambda data: data["personal_info"]["items"][0].update(link="https://user:pass@example.test"),
	lambda data: data["modules"].append(copy.deepcopy(data["modules"][0])),
	lambda data: data["modules"][0]["rows"][0].update(content=["a" * 6001]),
	lambda data: data["modules"][0]["rows"][0].update(type="script"),
])
def test_profile_validation_rejects_invalid_or_unsafe_structure(mutation):
	data = profile_data()
	mutation(data)
	with pytest.raises(ValueError):
		validate_profile(data, "test")


def test_plain_text_profiles_do_not_execute_markup_and_model_text_hides_inline_contacts():
	data = profile_data()
	data["modules"][0]["rows"][0]["content"] = ['<script>window.profileXss=1</script>', '联系我：13812345678 fixture@example.test']
	valid = validate_profile(data, "test")
	text = profile_model_text(valid)
	assert '<script>window.profileXss=1</script>' in text
	assert "13812345678" not in text and "fixture@example.test" not in text
	assert data["personal_info"]["items"][1]["value"] == "13812345678"


def test_storage_name_does_not_override_edited_identity_in_model_input():
	data = profile_data("old-person-file-name")
	data["title"] = "新的姓名"
	assert "old-person-file-name" not in profile_model_text(data)
	assert profile_model_text(data).startswith("新的姓名")


def settings_body(controller):
	r = controller.settings()["rules"]
	return {"campus_count": 5, "internship_count": 5, "min_salary": 15, "min_daily_salary": 300,
		"cities": r["city"], "roles": r["target_roles"], "history_dedupe": "job", "expected_revision": controller.state()["settings_revision"]}


def test_custom_city_direction_year_and_exclusions_save_without_touching_old_batch(profile_client):
	controller, bid = profile_client
	old = controller.batch(bid)
	body = settings_body(controller)
	body.update(cities=["成都"], roles=[], custom_role_keywords=["数据工程师", "机器人软件"], graduation_year=2028, bachelor_eligible=True, require=["Python", "SQL"], exclude=["外包"], exclude_company=["培训公司"])
	controller.save_settings(body)
	p = controller.settings()
	assert p["rules"]["city"] == ["成都"] and p["rules"]["graduation_year"] == 2028
	assert {s["query"] for s in search_plan(p)} == {"数据工程师", "机器人软件"}
	assert {s["city"] for s in search_plan(p)} == {"成都"}
	assert controller.batch(bid) == old
	with pytest.raises(ValueError, match="已有更新"):
		controller.save_settings(body)


def test_custom_directions_are_or_but_salary_and_required_terms_remain_mandatory():
	rules = {"target_roles": ["ai_development"], "custom_role_keywords": ["数据工程师"], "city": ["成都", "北京"], "graduation_year": 2028, "bachelor_eligible": True, "min_salary": 15, "min_daily_salary": 300, "require": ["Python", "SQL"], "exclude": ["外包"]}
	job = {"title": "2028届校招数据工程师", "description": "本科以上，Python和SQL开发", "education": "本科", "city": "成都", "salary": "15-25K"}
	assert filter_job(dict(job), rules, full=True)[0] == []
	assert filter_job({**job, "title": "2028届校招AI开发工程师"}, rules, full=True)[0] == []
	assert filter_job({**job, "salary": "14-25K"}, rules, full=True)[0]
	assert filter_job({**job, "description": "Python开发"}, rules, full=True)[0]
	assert filter_job({**job, "description": "Python和SQL外包开发"}, rules, full=True)[0]
	assert filter_job({**job, "education": "硕士"}, rules, full=True)[0]


def test_selected_roles_determine_new_search_queries_and_custom_matching_is_literal(profile_client):
	controller, _ = profile_client
	body = settings_body(controller)
	body.update(roles=["fullstack"], custom_role_keywords=["C++"])
	controller.save_settings(body)
	assert {s["query"] for s in search_plan(controller.settings())} == {"全栈", "C++"}
	job = {"title": "C++工程师", "description": "SQL"}
	assert filter_job(job, {"custom_role_keywords": ["C++"]}, full=True)[0] == []
	assert filter_job(job, {"custom_role_keywords": [".*"]}, full=True)[0]


@pytest.mark.parametrize("field,value", [("graduation_year", "2028"), ("bachelor_eligible", "false"), ("custom_role_keywords", ["x\ny"]), ("cities", ["not-a-city"]), ("require", [""])] )
def test_invalid_settings_preserve_saved_file(profile_client, field, value):
	controller, _ = profile_client
	body = settings_body(controller)
	body[field] = value
	before = controller.settings_path.read_bytes()
	with pytest.raises(ValueError):
		controller.save_settings(body)
	assert controller.settings_path.read_bytes() == before
