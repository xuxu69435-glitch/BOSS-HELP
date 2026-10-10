"""Conservative, evidence-based checks for campus and internship batches."""

from __future__ import annotations

import re
from typing import Any


_PAY = re.compile(
	r"(?P<low>\d+(?:\.\d+)?)\s*(?:[-~～至–—]\s*(?P<high>\d+(?:\.\d+)?))?\s*(?P<scale>[kK千])?\s*(?:元)?\s*(?:[/／]\s*(?P<unit>小时|时|天|日|月|年))?"
)
_CAMPUS = re.compile(r"秋招|校招|校园招聘|校园招募|应届生招聘")
_COHORT = re.compile(r"(?<!\d)(20\d{2}|2\d)\s*(?:届|级毕业生|校招)")


def graduate_offer(text: str) -> bool:
	clauses = re.split(r"[。\n；;]", text)
	if any(re.search(r"不(?:接受|招收|招).{0,6}应届|应届.{0,6}勿投|仅社招", clause) for clause in clauses):
		return False
	return any(
		re.search(r"(?:接受|欢迎|招聘|招收|面向|可以是).{0,20}应届|应届.{0,8}(?:可投|可申请|可接受)", clause)
		for clause in clauses
	)


def listing_recruitment_signal(job: dict[str, Any]) -> str | None:
	"""A discovery hint, never a replacement for full-JD eligibility checks."""
	if str(job.get("raw_job_type")) == "4" or job.get("employment_type") == "实习":
		return "internship"
	title = str(job.get("title") or "")
	if str(job.get("raw_job_type")) == "5" or _CAMPUS.search(title) or graduate_offer(title):
		return "campus"
	if "实习" in title:
		return "internship"
	return None


def salary_lower(value: str, unit: str) -> float | None:
	"""Read an advertised lower bound, without converting between pay periods."""
	match = _PAY.match(value.strip())
	if not match:
		return None
	actual_unit = match.group("unit")
	if actual_unit in {"日", "天"}:
		actual_unit = "day"
	elif actual_unit == "月" or (actual_unit is None and match.group("scale")):
		actual_unit = "month"
	if actual_unit != unit or "以下" in value:
		return None
	low = float(match.group("low"))
	high = float(match.group("high")) if match.group("high") else low
	if low > high:
		return None
	return low * (1000 if match.group("scale") else 1)


def bachelor_check(job: dict[str, Any], *, full: bool) -> tuple[list[str], list[str]]:
	reasons: list[str] = []
	evidence: list[str] = []
	degree = str(job.get("education") or "").strip()
	if "本科" in degree or degree in {"不限", "学历不限", "大专", "大专及以上", "高中", "中专/中技", "初中及以下"}:
		evidence.append(f"学历标签:{degree}")
	elif degree:
		reasons.append(f"本科资格不符合或待确认:{degree}")
	if not full:
		return reasons, evidence
	text = f"{job.get('title', '')}\n{job.get('description', '')}"
	for clause in re.split(r"[\n。；;，,]", text):
		if re.search(r"(?:不(?:接受|招收|招)|拒绝).{0,4}本科|本科.{0,4}(?:勿投|不接受|不招)", clause):
			reasons.append("JD明确不接受本科")
		if "本科" in clause:
			if re.search(
				r"本科.{0,10}(?:学历|学位|在读|毕业|以上)|(?:学历|学位|要求).{0,8}本科|本科[、/／或及和]", clause
			):
				evidence.append(f"JD学历:{clause.strip()}")
			if re.search(r"(?:必须|仅限|只招).{0,5}(?:硕士|博士|研究生)", clause):
				reasons.append("JD要求研究生学历")
			continue
		if not re.search(r"硕士|博士|研究生", clause):
			continue
		if re.search(r"(?:硕士|博士|研究生).{0,8}(?:优先|优选|加分|更佳)", clause):
			evidence.append(f"学历偏好（非硬门槛）:{clause.strip()}")
			continue
		if re.search(
			r"(?:硕士|博士|研究生).{0,10}(?:及以上|以上|学历|学位|在读|毕业生)|(?:学历|要求|必须|仅限|只招).{0,8}(?:硕士|博士|研究生)|博士(?:专项|招聘|校招)",
			clause,
		):
			reasons.append(f"JD学历限制:{clause.strip()}")
	if not any(item.startswith(("学历标签:", "JD学历:")) for item in evidence):
		reasons.append("信息缺失:本科可申请依据")
	return reasons, evidence


def campus_check(job: dict[str, Any], rules: dict[str, Any], *, full: bool) -> tuple[list[str], list[str]]:
	reasons: list[str] = []
	evidence: list[str] = []
	if rules.get("bachelor_eligible"):
		degree_reasons, degree_evidence = bachelor_check(job, full=full)
		reasons.extend(degree_reasons)
		evidence.extend(degree_evidence)
	year = rules.get("graduation_year")
	if not year:
		return reasons, evidence
	title = str(job.get("title") or "")
	description = str(job.get("description") or "")
	text = title + "\n" + description
	intern = listing_recruitment_signal(job) == "internship"
	if full and not intern and str(job.get("raw_job_type")) != "5":
		intern = bool(
			re.search(
				r"(?:岗位性质|职位类型|招聘类型)\s*[:：]\s*实习|实习(?:时间|时长|期限|天数|要求)\s*[:：]", description
			)
		)
	cohorts = {int(value) if len(value) == 4 else 2000 + int(value) for value in _COHORT.findall(text)}
	cohorts.update(2000 + int(value) for value in re.findall(r"(?<!\d)(2\d)\s*秋招", text))
	year_evidence = re.search(
		rf"(?<!\d)(?:{year}|{str(year)[-2:]})\s*(?:届|校招)|(?<!\d){str(year)[-2:]}\s*秋招|{year}\s*年[^。\n；;]{{0,30}}毕业|毕业[^。\n；;]{{0,30}}{year}",
		text,
	)
	if full and cohorts and int(year) not in cohorts and not year_evidence:
		reasons.append(f"毕业届别不符或待确认:{sorted(cohorts)}")
	if intern:
		kind = "internship"
		minimum = rules.get("min_daily_salary")
		unit = "day"
	else:
		kind = "campus"
		minimum = (rules.get("min_salary") or 0) * 1000
		unit = "month"
		if full:
			accepts_graduates = rules.get("allow_explicit_graduate_roles") and graduate_offer(text)
			campus_offer = bool(_CAMPUS.search(text) or year_evidence)
			if not campus_offer and not accepts_graduates:
				reasons.append("信息缺失:秋招或校园招聘依据")
			if accepts_graduates and not campus_offer:
				job["recruitment_label"] = "应届全职（批次待核实）"
				job["screening_notes"] = list(
					dict.fromkeys(
						[
							*job.get("screening_notes", []),
							"岗位标题或JD明确接受应届生，但未明示秋招批次；按应届全职候选收录，需核实招聘批次及毕业届别。",
						]
					)
				)
			if not year_evidence:
				if rules.get("allow_unspecified_cohort") and (campus_offer or accepts_graduates) and not cohorts:
					if campus_offer:
						job["screening_notes"] = list(
							dict.fromkeys(
								[
									*job.get("screening_notes", []),
									f"明确为校招，但未注明毕业届别，需确认是否接收{year}届。",
								]
							)
						)
				else:
					reasons.append(f"信息缺失:面向{year}届")
	if minimum:
		lower = salary_lower(str(job.get("salary") or ""), unit)
		if lower is None:
			if full or re.search(r"面议|保密|待定", str(job.get("salary") or "")):
				reasons.append(f"信息缺失:{'日薪' if intern else '月薪'}明确金额")
		elif lower < minimum:
			reasons.append(f"薪资不符:{lower:g} < {minimum:g}元/{'天' if intern else '月'}")
		else:
			evidence.append(f"{'日薪' if intern else '月薪'}下限:{lower:g}元")
	if full and not reasons:
		job["recruitment_kind"] = kind
		job["cohort_confirmed"] = bool(year_evidence) if kind == "campus" else None
		job["eligibility_evidence"] = evidence + ([year_evidence.group(0)] if year_evidence else [])
	return reasons, evidence
