"""Do not count adjacent search hits as requested roles without JD evidence."""

import pytest

from boss_agent_cli.delivery import filter_job
from boss_agent_cli.role_filters import role_check


TARGETS = [
	"ai_development",
	"ai_infra",
	"foundation_model",
	"model_development",
	"agent_development",
	"fullstack",
	"product_manager",
	"ai_for_science",
	"model_training",
]


@pytest.mark.parametrize(
	"title,description,city,accepted",
	[
		("AI游戏开发工程师", "开发Agent工具链", "北京", True),
		("【27校招】AI策划", "用AI制作休闲游戏Demo，设计玩法与关卡", "北京", False),
		("AI数据科学（算法）-27届校招", "广告数据监控，专项实验分析，业务发展规划", "上海", False),
		("软件开发实习生 AI 辅助开发 / Vibe Coding", "Python软件开发", "上海", True),
		("AI Agent开发工程师", "基于LangGraph开发企业智能体", "北京", True),
		("AI Infra工程师", "分布式训练", "杭州", True),
		("AI全栈开发工程师", "Web和服务端开发", "深圳", True),
		("全栈策划", "AI内容创作", "北京", False),
		("产品经理", "需求分析与产品规划", "广州", True),
		("AI产品实习生", "参与需求分析、产品方案设计和迭代", "北京", True),
		("AI应用策略产品实习生", "研究用户需求，参与AI应用策略设计和迭代", "北京", True),
		("AI产品运营实习生", "需求分析、增长运营", "北京", False),
		("AI产品销售实习生", "客户需求分析与产品销售", "上海", False),
		("量化研究员-AI方向", "训练机器学习模型", "上海", True),
		("科学计算研究员", "利用深度学习预测蛋白质结构", "苏州", True),
		("后端开发工程师", "负责大模型应用开发和RAG检索", "南京", True),
		("后端开发工程师", "Java开发，可以使用AI辅助编码", "南京", False),
		("模型研发工程师", "开发基础模型", "杭州", True),
		("算法工程师", "负责图像算法开发", "上海", True),
		("算法工程师", "负责图像算法开发", "北京", True),
		("AI销售", "销售AI应用、模型训练平台和Agent工具", "上海", False),
		("2027校招/策划美术算法等多岗位", "AI Agent工程师 游戏研发方向", "杭州", False),
		("后端开发工程师", "负责大模型应用开发", "北京", True),
		("AI Agent实习生", "参与设计并开发可接入线上系统的 AI 智能体，使用Python进行后端开发", "上海", True),
		("大模型工程实习生", "负责LLM微调和模型部署，构建RAG检索系统", "深圳", True),
		("大模型训练实习生", "参与多模态模型的 Pre-training / SFT / Post-training 训练", "深圳", True),
		("大模型评测实习生", "参与大模型评测框架的日常开发与维护", "深圳", True),
		("AI Agent实习生", "使用AI工具制作宣传物料，日常内容发布", "上海", False),
		("AI运营实习生", "搭建Agent工具辅助内容运营", "上海", False),
		("大模型招聘实习生", "为LLM微调团队筛选简历", "深圳", False),
		("AI数据标注实习生", "为大模型训练整理图片标签", "深圳", False),
	],
)
def test_requested_roles_require_explicit_evidence(title, description, city, accepted):
	reasons, evidence = role_check({"title": title, "description": description, "city": city}, TARGETS)
	assert bool(evidence) is accepted
	assert bool(reasons) is not accepted


def test_legacy_filters_without_target_roles_are_unchanged():
	assert filter_job({"title": "Other", "description": "Other"}, {}, full=True) == ([], [])


def test_only_full_jd_stage_rejects_an_unconfirmed_direction():
	job = {"title": "研发工程师"}
	rules = {"target_roles": TARGETS}
	assert filter_job(job, rules, full=False) == ([], [])
	assert filter_job(job, rules, full=True)[0] == ["岗位方向不符或待确认"]


def test_unknown_role_configuration_fails_explicitly():
	with pytest.raises(ValueError, match="Unknown target roles"):
		role_check({}, ["typo"])


@pytest.mark.parametrize("title", ["AI Infra工程师", "产品经理", "Agent开发工程师", "全栈工程师"])
def test_one_target_direction_is_sufficient(title):
	assert role_check({"title": title, "description": "", "city": "北京"}, TARGETS)[0] == []
