from boss_agent_cli.client_controller import search_plan
from test_delivery_quotas_history import params


def test_city_and_direction_are_rotated_and_server_filters_are_explicit():
	p = params()
	p.update(query="AI开发 校招", extra_queries=["Agent 校招", "AI开发 实习", "Agent 实习", "全栈 校招"])
	p["rules"]["city"] = ["北京", "上海", "杭州"]
	searches = search_plan(p)
	assert [(r["query"], r["city"]) for r in searches[:6]] == [
		("AI开发", "北京"),
		("AI开发", "北京"),
		("Agent", "上海"),
		("Agent", "上海"),
		("全栈", "杭州"),
		("全栈", "杭州"),
	]
	assert len(searches) == 18
	assert len({(r["query"], r["city"], r["kind"]) for r in searches}) == 18
	assert all(
		r["filters"] == (
			{"experience": "应届生", "experience_code": "102"} if r["kind"] == "campus"
			else {"experience": "在校生", "experience_code": "108"}
		) for r in searches
	)


def test_model_training_uses_only_selected_cities():
	p = params()
	p.update(query="AI开发 校招", extra_queries=[])
	p["rules"].update(city=["北京"], target_roles=["model_training"])
	searches = search_plan(p)
	assert searches
	assert all(r["query"] == "模型训练" and r["city"] == "北京" for r in searches)
