"""Explicit role evidence, independent of a search engine's keyword ranking."""

from __future__ import annotations

import re
from typing import Any


_ROLES = {
	"ai_development": (
		"AI开发",
		r"(?:AI|人工智能|大模型|LLM).{0,12}(?:开发|研发|工程师)",
		r"(?:开发|研发|构建|搭建).{0,20}(?:AI应用|大模型应用|LLM应用|RAG|智能体)|(?:AI应用|大模型应用|LLM应用).{0,12}(?:开发|研发|工程化)",
	),
	"ai_infra": (
		"AI Infra",
		r"(?:AI|ML)[\s_-]*Infra|(?:AI|机器学习|大模型).{0,8}(?:基础设施|基础架构)|推理引擎",
		r"(?:训练|推理).{0,8}(?:框架|平台|引擎).{0,12}(?:开发|研发|优化)|(?:分布式训练|推理加速|算子开发)",
	),
	"foundation_model": (
		"基础模型",
		r"基础模型|基座模型|Foundation\s*Model|大模型.{0,8}(?:算法|研究)",
		r"(?:基础模型|基座模型|预训练模型).{0,15}(?:开发|研发|研究|训练)|(?:大模型预训练)",
	),
	"model_development": (
		"模型开发",
		r"模型.{0,8}(?:开发|研发|算法|工程师)|(?:开发|研发).{0,8}模型",
		r"(?:开发|研发|构建|设计).{0,12}(?:机器学习|深度学习|神经网络|大语言模型)|(?:模型|LLM|VLM).{0,8}(?:微调|训练|评估|评测)|(?:Pre-training|Post-training|SFT|PPO|GRPO|LoRA).{0,15}(?:训练|微调)",
	),
	"agent_development": (
		"Agent开发",
		r"(?:Agent|智能体).{0,12}(?:开发|研发|工程师)|(?:开发|研发).{0,12}(?:Agent|智能体)",
		r"(?:开发|研发|构建|搭建).{0,20}(?:Agent|智能体)|(?:Agent|智能体).{0,12}(?:开发|研发|工程化)",
	),
	"fullstack": (
		"全栈",
		r"全栈.{0,8}(?:开发|研发|工程师)|(?:开发|研发|工程师).{0,8}全栈|full[\s-]*stack",
		r"(?:负责|参与|完成).{0,16}前后端.{0,8}(?:开发|研发)|端到端.{0,20}(?:Web|前后端)",
	),
	"product_manager": ("产品经理", r"产品经理|Product\s*Manager", ""),
	"ai_for_science": (
		"AI for Science",
		r"AI\s*(?:for\s*)?Science|科学智能",
		r"AI\s*(?:for\s*)?Science|科学智能|(?:机器学习|深度学习|人工智能|AI).{0,25}(?:分子|蛋白质|科学计算|材料发现)|(?:分子|蛋白质|科学计算|材料发现).{0,25}(?:机器学习|深度学习|人工智能|AI)",
	),
	"model_training": (
		"模型训练/基础算法",
		r"模型训练|机器学习|深度学习|算法工程师",
		r"(?:负责|参与|完成).{0,20}(?:模型训练|算法开发|算法研发)",
	),
}

ROLE_NAMES = {key: spec[0] for key, spec in _ROLES.items()}
ROLE_SEARCH_QUERIES = {**ROLE_NAMES, "agent_development": "Agent", "model_training": "模型训练"}


def role_check(job: dict[str, Any], targets: list[str], custom_keywords: list[str] | None = None) -> tuple[list[str], list[str]]:
	if not targets and not custom_keywords:
		return [], []
	unknown = set(targets) - _ROLES.keys()
	if unknown:
		raise ValueError(f"Unknown target roles: {sorted(unknown)}")
	title = str(job.get("title") or "")
	description = str(job.get("description") or "")
	if re.search(r"多岗位|全品类岗位|岗位汇总|招聘合集", title):
		return ["综合招聘信息，缺少单一岗位JD"], []
	# Do not let a reference to AI tools turn an unrelated occupation into a developer role.
	technical_title = bool(re.search(r"开发|研发|工程师|算法|研究|科学家|软件|技术", title, re.I))
	if not technical_title and not re.search(r"销售|运营|客服|招聘|市场|数据标注", title):
		technical_title = bool(re.search(r"(?:AI|Agent|智能体|大模型|LLM|模型工程|模型训练|模型评测).{0,12}实习", title, re.I))
	evidence = []
	for target in targets:
		label, title_pattern, description_pattern = _ROLES[target]
		match = re.search(title_pattern, title, re.I)
		if (
			not match
			and target == "product_manager"
			and re.search(r"产品.{0,5}(?:实习生|助理)", title)
			and not re.search(r"销售|运营|客服|市场推广", title)
		):
			match = re.search(r"需求分析|产品设计|产品规划|方案设计|原型设计|研究用户需求|应用策略设计", description)
		if not match and target == "model_training" and technical_title:
			match = re.search(r"(?:训练|微调).{0,8}(?:机器学习|深度学习|神经网络)模型", description)
		if not match and description_pattern and technical_title:
			match = re.search(description_pattern, description, re.I)
		if match:
			evidence.append(f"岗位方向:{label}（{match.group(0)}）")
	text = (title + "\n" + description).casefold()
	for keyword in custom_keywords or []:
		if keyword.casefold() in text:
			evidence.append(f"自定义方向关键词:{keyword}")
	return ([] if evidence else ["岗位方向不符或待确认"]), evidence
