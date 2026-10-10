"""Empty first-run configuration and user-controlled setup, using temporary data."""

import importlib.util
import json
import shutil
from pathlib import Path
from unittest.mock import Mock

import pytest

from boss_agent_cli.client_controller import ClientController, search_plan


ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("initialize_config", ROOT / "scripts/initialize_config.py")
initializer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(initializer)


@pytest.fixture
def first_run(tmp_path):
	shutil.copytree(ROOT / "config", tmp_path / "config")
	initializer.initialize(tmp_path)
	controller = ClientController(tmp_path / "data/boss-agent", tmp_path / "data/boss-agent/preferences/settings.json")
	yield controller, tmp_path
	controller.close()


def test_first_run_contains_no_personal_preferences_and_opens_locally(first_run):
	controller, _ = first_run
	state = controller.state()
	rules = state["settings"]["rules"]
	assert rules["city"] == [] and rules["target_roles"] == [] and rules["custom_role_keywords"] == []
	assert rules["graduation_year"] is None
	assert rules["min_salary"] is None and rules["min_daily_salary"] is None
	assert state["batches"] == [] and state["operation"]["status"] == "idle"
	profile = controller.profile()["data"]
	assert profile["personal_info"]["items"] == [] and profile["modules"] == []
	assert search_plan(state["settings"]) == []


def test_reinitialization_never_overwrites_user_changes(first_run):
	controller, root = first_run
	settings = controller.settings_path
	profile = controller.data_dir / "resumes/user.json"
	settings.write_text('{"user_modified": true}', encoding="utf-8")
	profile.write_text('{"user_modified": true}', encoding="utf-8")
	initializer.initialize(root)
	assert json.loads(settings.read_text(encoding="utf-8")) == {"user_modified": True}
	assert json.loads(profile.read_text(encoding="utf-8")) == {"user_modified": True}


def test_blank_configuration_cannot_start_network_work(first_run, monkeypatch):
	controller, _ = first_run
	submit = Mock(side_effect=AssertionError("No task may start before user configuration"))
	monkeypatch.setattr(controller._executor, "submit", submit)
	with pytest.raises(ValueError, match="请先保存"):
		controller.start("prepare", {})
	submit.assert_not_called()
	assert controller.operation["status"] == "idle"


def test_user_can_save_their_own_city_direction_cohort_and_profile(first_run):
	controller, _ = first_run
	controller.save_settings({
		"expected_revision": controller.state()["settings_revision"],
		"cities": ["成都"], "roles": [], "custom_role_keywords": ["测试岗位方向"],
		"graduation_year": 2031, "bachelor_eligible": False,
		"min_salary": 8, "min_daily_salary": 100,
		"campus_count": 1, "internship_count": 1, "history_dedupe": "job",
	})
	params = controller.settings()
	assert params["rules"]["city"] == ["成都"] and params["rules"]["graduation_year"] == 2031
	assert {item["query"] for item in search_plan(params)} == {"测试岗位方向"}
	assert {item["city"] for item in search_plan(params)} == {"成都"}
	current = controller.profile()
	current["data"]["title"] = "合成测试资料"
	current["data"]["modules"] = [{"id": "test", "title": "测试经历", "rows": [{"type": "richtext", "content": ["合成经历文本。"]}]}]
	saved = controller.save_profile({"expected_version": current["version"], "data": current["data"]})
	assert "合成经历文本" in saved["model_text"]
