"""Synthetic client data. Never reads a user's real job or credential directory."""

import json
from pathlib import Path

from boss_agent_cli.client_controller import ClientController
from boss_agent_cli.delivery_store import DeliveryStore


def fixture_client(root: Path):
	root.mkdir(parents=True, exist_ok=True)
	settings = root / "preferences.json"
	params = {
		"query": "AI开发 校招",
		"extra_queries": ["AI开发 实习"],
		"count": 10,
		"max_pages": 20,
		"max_details": 60,
		"max_seconds": 900,
		"resume": "test",
		"platform": "zhipin",
		"rules": {
			"city": ["北京", "上海"],
			"graduation_year": 2027,
			"bachelor_eligible": True,
			"min_salary": 15,
			"min_daily_salary": 300,
			"recruitment_quotas": {"campus": 5, "internship": 5},
			"history_dedupe": "job",
			"target_roles": ["ai_development"],
			"require": [],
			"exclude": [],
			"exclude_company": [],
		},
	}
	settings.write_text(json.dumps(params, ensure_ascii=False), encoding="utf-8")
	with DeliveryStore(root) as store:
		batch_id = store.create(params)
		for n in (1, 2):
			job = {
				"job_id": f"fixture-job-{n}",
				"security_id": "test-private-security",
				"lid": "test-private-context",
				"title": "AI应用开发工程师" if n == 1 else "<script>window.fixtureXss=true</script>",
				"company": f"测试企业 {n}",
				"city": "北京",
				"salary": "20-30K",
				"education": "本科",
				"recruitment_kind": "campus",
				"description": "2027届，本科及以上。开发Agent应用。",
				"summary": "参与Agent应用开发与工程化。",
				"greeting": "您好，我有Python与Agent项目经历，希望进一步交流。",
				"resume_evidence": ["Python与Agent项目经历"],
				"warnings": ["测试用提示"],
				"rule_matches": ["本科可申请"],
			}
			store.add(batch_id, n, job)
			store.save_draft(batch_id, n, job)
	return ClientController(root, settings), batch_id
