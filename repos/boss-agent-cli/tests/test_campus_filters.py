"""Regression tests for the user's undergraduate campus/internship constraints."""

import json

import pytest

from boss_agent_cli.campus_filters import salary_lower
from boss_agent_cli.delivery import filter_job
from test_delivery import environment as environment, invoke, job


RULES = {
	"city": ["北京", "上海"],
	"min_salary": 15,
	"min_daily_salary": 400,
	"graduation_year": 2027,
	"bachelor_eligible": True,
}


def vacancy(**updates):
	return {
		"title": "AI开发工程师-2027秋招",
		"city": "北京",
		"salary": "15-25K·15薪",
		"education": "本科",
		"description": "面向2027届毕业生，本科及以上学历。负责AI应用开发。",
		**updates,
	}


@pytest.mark.parametrize(
	"salary,unit,expected",
	[
		("15-25K·15薪", "month", 15000),
		("15000-20000元/月", "month", 15000),
		("400-500元/天", "day", 400),
		("400元/日", "day", 400),
		("400~600元/天", "day", 400),
		("8-10K", "day", None),
		("400元/天", "month", None),
		("400元/小时", "day", None),
		("40-60K/年", "month", None),
		("面议", "month", None),
		("500-400元/天", "day", None),
		("400元/天以下", "day", None),
	],
)
def test_explicit_salary_units_and_lower_bound(salary, unit, expected):
	assert salary_lower(salary, unit) == expected


def test_campus_pass_records_evidence():
	item = vacancy()
	assert not filter_job(item, RULES, full=True)[0]
	assert item["recruitment_kind"] == "campus"
	assert any("本科" in entry for entry in item["eligibility_evidence"])


def test_internship_uses_daily_not_monthly_floor():
	item = vacancy(
		title="Agent开发实习生", salary="400-600元/天", raw_job_type=4, description="本科及以上在校生，负责Agent开发。"
	)
	assert not filter_job(item, RULES, full=True)[0]
	assert item["recruitment_kind"] == "internship"


@pytest.mark.parametrize(
	"updates,reason",
	[
		({"salary": "12-25K"}, "薪资不符"),
		({"title": "AI开发实习生", "salary": "300-500元/天"}, "薪资不符"),
		({"title": "AI开发实习生", "salary": "15-20K"}, "日薪明确金额"),
		({"education": "硕士"}, "本科资格"),
		({"description": "2027校招，要求硕士及以上学历"}, "JD学历限制"),
		({"description": "2027届本科及以上。必须硕士学历。"}, "JD学历限制"),
		({"description": "2027届，本科不接受"}, "不接受本科"),
		({"title": "AI开发工程师", "description": "经验不限，本科及以上学历。"}, "秋招或校园招聘"),
		({"title": "AI开发校招", "description": "本科及以上学历。"}, "面向2027届"),
		({"title": "AI实习生", "salary": "400元/天", "description": "只招2028届本科生"}, "毕业届别"),
		({"city": "成都"}, "city"),
		({"education": "", "description": "2027届校招，无学历说明"}, "本科可申请依据"),
	],
)
def test_ineligible_or_unknown_jobs_do_not_pass(updates, reason):
	reasons, _ = filter_job(vacancy(**updates), RULES, full=True)
	assert any(reason in item for item in reasons), reasons


def test_masters_preference_is_not_a_mandatory_degree():
	item = vacancy(description="2027届，本科及以上学历，硕士优先。")
	assert not filter_job(item, RULES, full=True)[0]


def test_missing_list_fields_can_be_verified_in_full_jd():
	item = vacancy(education="", title="AI开发工程师", description="")
	assert not filter_job(item, RULES, full=False)[0]
	item["description"] = "2027届校招，本科及以上学历"
	assert not filter_job(item, RULES, full=True)[0]


def test_explicit_campus_with_unspecified_year_is_flagged_not_silently_rejected():
	item = vacancy(title="【校招】AI开发工程师", description="本科及以上学历，开发AI应用。")
	assert not filter_job(item, {**RULES, "allow_unspecified_cohort": True}, full=True)[0]
	assert item["cohort_confirmed"] is False
	assert any("届别" in note for note in item["screening_notes"])


@pytest.mark.parametrize("title", ["27校招-AI SRE工程师", "27秋招-AI开发工程师"])
def test_common_short_cohort_labels_are_recognized(title):
	item = vacancy(title=title, description="本科及以上学历。开发Agent平台。")
	assert not filter_job(item, RULES, full=True)[0]
	assert item["cohort_confirmed"] is True


@pytest.mark.parametrize("title", ["28届校招-AI开发", "28秋招-AI开发", "2026校招-AI开发"])
def test_unspecified_cohort_option_never_accepts_a_conflicting_cohort(title):
	item = vacancy(title=title, description="本科及以上学历。开发Agent平台。")
	assert filter_job(item, {**RULES, "allow_unspecified_cohort": True}, full=True)[0]


def test_negotiable_salary_is_rejected_before_fetching_jd():
	assert filter_job(vacancy(salary="面议"), RULES, full=False)[0]


def test_explicit_graduate_fulltime_is_distinguished_from_confirmed_campus():
	item = vacancy(title="AI应用开发工程师", description="可以是在校硕本应届生。负责AI应用开发。")
	rules = {**RULES, "allow_unspecified_cohort": True, "allow_explicit_graduate_roles": True}
	assert not filter_job(item, rules, full=True)[0]
	assert item["recruitment_label"] == "应届全职（批次待核实）"
	assert item["cohort_confirmed"] is False
	assert not any(note.startswith("明确为校招") for note in item["screening_notes"])
	assert filter_job(vacancy(title="AI应用开发", description="经验不限。本科及以上学历。"), rules, full=True)[0]
	assert filter_job(vacancy(title="AI应用开发", description="不接受应届生，本科及以上学历。"), rules, full=True)[0]


@pytest.mark.parametrize("title", ["AI应用开发（应届可投）", "大模型开发（接受应届）"])
def test_graduate_eligibility_in_title_is_not_discarded(title):
	rules = {**RULES, "allow_unspecified_cohort": True, "allow_explicit_graduate_roles": True}
	item = vacancy(title=title, description="本科及以上学历，负责AI开发。")
	assert not filter_job(item, rules, full=True)[0]
	assert item["cohort_confirmed"] is False
	assert item["recruitment_label"] == "应届全职（批次待核实）"
	item["description"] = "不接受应届生。只招有工作经验的人。"
	assert filter_job(item, rules, full=True)[0]


def test_campus_response_type_is_not_misread_as_internship_from_mixed_title():
	item = vacancy(title="2027校招AI开发（可提前实习）", raw_job_type=5, salary="15-30K")
	assert not filter_job(item, RULES, full=True)[0]
	assert item["recruitment_kind"] == "campus"


@pytest.mark.parametrize("city", ["北京", "上海"])
def test_each_city_can_qualify_on_its_own(city):
	assert not filter_job(vacancy(city=city), RULES, full=True)[0]


def test_multiple_cities_are_searched_after_an_empty_city(tmp_path, environment, monkeypatch):
	platform = environment[0]
	calls = []

	def search(query, *, city, page):
		calls.append((query, city, page))
		rows = [] if city == "北京" else [{**job(1), "cityName": "上海"}]
		return {"code": 0, "zpData": {"jobList": rows, "hasMore": False}}

	monkeypatch.setattr(platform, "search_jobs", search)
	result = invoke(tmp_path, "prepare", "AI", "--city", "北京,上海", "--no-ai")
	assert result.exit_code == 0, result.output
	assert calls == [("AI", "北京", 1), ("AI", "上海", 1)]
	assert len(json.loads(result.stdout)["data"]["items"]) == 1


def test_extra_queries_share_the_global_page_budget(tmp_path, environment, monkeypatch):
	platform = environment[0]
	calls = []

	def search(query, *, city, page):
		calls.append((query, city, page))
		return {"code": 0, "zpData": {"jobList": [job(len(calls))], "hasMore": True}}

	monkeypatch.setattr(platform, "search_jobs", search)
	result = invoke(tmp_path, "prepare", "AI", "--extra-query", "全栈", "--max-pages", "2", "--no-ai")
	assert result.exit_code == 0, result.output
	assert calls == [("AI", None, 1), ("全栈", None, 1)]
	assert json.loads(result.stdout)["data"]["stop_reason"] == "page_limit"


def test_daily_salary_requires_graduation_mode(tmp_path, environment):
	result = invoke(tmp_path, "prepare", "AI", "--min-daily-salary", "400", "--no-ai")
	assert result.exit_code == 1
	environment[2].assert_not_called()


@pytest.mark.parametrize(
	"campus_count,intern_count,expected_total,expected_interns",
	[(40, 20, 50, 10), (8, 12, 10, 2), (3, 10, 3, 0), (0, 10, 0, 0), (5, 10, 6, 1)],
)
def test_internship_ratio_uses_actual_size_even_when_interns_arrive_first(
	tmp_path, environment, monkeypatch, campus_count, intern_count, expected_total, expected_interns
):
	platform = environment[0]
	rows = [
		{**job(index + 1), "jobName": "AI开发实习生", "jobType": 4, "salaryDesc": "400-500元/天"}
		for index in range(intern_count)
	] + [
		{**job(intern_count + index + 1), "jobName": "2027届秋招AI开发", "salaryDesc": "15-25K"}
		for index in range(campus_count)
	]
	monkeypatch.setattr(
		platform, "search_jobs", lambda *args, **kwargs: {"code": 0, "zpData": {"jobList": rows, "hasMore": False}}
	)
	monkeypatch.setattr(
		platform,
		"job_detail",
		lambda job_id: {
			"code": 0,
			"zpData": {"jobInfo": {"encryptJobId": job_id}, "jobDetail": "本科及以上学历。负责AI开发。"},
		},
	)
	result = invoke(
		tmp_path,
		"prepare",
		"AI",
		"--no-ai",
		"--graduation-year",
		"2027",
		"--bachelor-eligible",
		"--min-salary",
		"15",
		"--min-daily-salary",
		"400",
		"--max-internship-ratio",
		"0.2",
	)
	assert result.exit_code == 0, result.output
	data = json.loads(result.stdout)["data"]
	assert len(data["items"]) == expected_total
	assert sum(item["recruitment_kind"] == "internship" for item in data["items"]) == expected_interns
	assert expected_interns * 5 <= expected_total
