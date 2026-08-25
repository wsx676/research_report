# -*- coding: utf-8 -*-
"""HypothesisAgent / Lead（设计文档 §5.1）：基于研究空白清单生成候选假设。

职责：
1. 读入 SurveyAgent 的 research_gaps.md 与 survey_cards.json（只取路径引用
   + 截断输入，遵守上下文压缩红线）；
2. strong 档 LLM 生成 5~8 个候选假设，每个含四要素：
   问题陈述 / 可验证预测 / 所需最小实验 / 子方向归属；
3. 域约束写入 prompt（可全自动化验证、不超算力预算——§5.1 硬性检查项，
   由 D2 下午的 GateAgent 复核）；
4. 每个假设落盘 proposals/P0xx.md（front-matter schema v0，status=candidate），
   并汇总 hypotheses.json 供 GateAgent 打分。

输出 JSON 解析容错：接受裸 JSON / ```json 围栏 / 前后噪声文本。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .artifacts import proposal_front_matter, write_proposal
from .llm import LLMClient

#: 假设数量目标区间（§5.1：5~8 个）
MIN_HYPOTHESES = 5
MAX_HYPOTHESES = 8
#: gaps 文件读入截断（上下文压缩）
GAPS_INPUT_LIMIT = 4000

HYPOTHESIS_SYSTEM = (
    "You are a research lead generating candidate hypotheses for an "
    "automated science pipeline. Each hypothesis MUST be verifiable by "
    "code-only experiments (no human annotation), fit the compute budget, "
    "and map to exactly one sub-direction. Output ONLY a JSON array."
)


def extract_json_array(text: str) -> List[Dict[str, Any]]:
    """从 LLM 输出中提取 JSON 数组（容错：围栏/前后噪声）。"""
    fenced = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, flags=re.S)
    if fenced:
        return _load(fenced.group(1))
    bracket = re.search(r"\[.*\]", text, flags=re.S)
    if bracket:
        return _load(bracket.group(0))
    return []


def _load(raw: str) -> List[Dict[str, Any]]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


REQUIRED_HYPO_FIELDS = ("id", "title", "problem_statement",
                        "testable_prediction", "minimal_experiment")


def validate_hypotheses(items: List[Dict[str, Any]]) -> List[str]:
    """返回问题清单（空 = 通过）；缺字段的条目在 Agent.run 中被丢弃。"""
    problems: List[str] = []
    if not MIN_HYPOTHESES <= len(items) <= MAX_HYPOTHESES:
        problems.append(f"count={len(items)} 不在 [{MIN_HYPOTHESES}, {MAX_HYPOTHESES}]")
    for i, item in enumerate(items):
        missing = [k for k in REQUIRED_HYPO_FIELDS if not str(item.get(k) or "").strip()]
        if missing:
            problems.append(f"item[{i}] 缺字段 {missing}")
    return problems


class HypothesisAgent:
    """空白清单 → 5~8 个候选假设（§5.1 HypothesisAgent/Lead）。"""

    def __init__(self, topic: Dict[str, Any], llm_strong: Optional[LLMClient],
                 proposals_dir: Path):
        self.topic = topic
        self.llm = llm_strong
        self.proposals_dir = Path(proposals_dir)

    def run(self, gaps_path: Path | str) -> Dict[str, Any]:
        gaps = Path(gaps_path).read_text(encoding="utf-8")[:GAPS_INPUT_LIMIT]
        if self.llm is None:
            items = self._placeholder_items()
        else:
            items = self._generate(gaps)
        items = [it for it in items if not _missing_fields(it)][:MAX_HYPOTHESES]

        self.proposals_dir.mkdir(parents=True, exist_ok=True)
        paths: List[str] = []
        for idx, item in enumerate(items, start=1):
            pid = f"P{idx:03d}"
            body = self._body_md(item)
            meta = proposal_front_matter(
                pid, title=item.get("title", ""),
                status="candidate",
                extra={"sub_direction": item.get("sub_direction", ""),
                       "source": "hypothesis_agent"})
            path = write_proposal(self.proposals_dir, meta, body)
            paths.append(str(path))

        summary = {
            "schema": "v0",
            "topic": self.topic.get("name"),
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "count": len(items),
            "validation": validate_hypotheses(items),
            "hypotheses": items,
        }
        summary_path = self.proposals_dir / "hypotheses.json"
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        print(f"[hypothesis] {len(items)} 个候选假设（"
              f"校验问题: {summary['validation'] or '无'}）")
        return {"hypotheses": str(summary_path), "proposals": paths}

    # ------------------------------------------------------------ internals
    def _generate(self, gaps: str) -> List[Dict[str, Any]]:
        budget = self.topic.get("budget") or {}
        prompt = (
            f"研究主题：{self.topic.get('title') or self.topic.get('name')}\n"
            f"子方向：{', '.join(self.topic.get('sub_directions', []))}\n"
            f"算力预算（硬约束）：{json.dumps(budget, ensure_ascii=False)}\n\n"
            f"研究空白清单：\n{gaps}\n\n"
            "请生成 5~8 个候选假设，输出 JSON 数组，每个元素字段：\n"
            '{"id": "H1", "title": "一句话标题", '
            '"problem_statement": "问题陈述", '
            '"testable_prediction": "可量化验证的预测", '
            '"minimal_experiment": "所需最小实验（数据/基准/指标/预算量级）", '
            '"sub_direction": "所属子方向"}\n'
            "要求：预测必须可由代码自动验证；实验必须不超上述算力预算；"
            "只输出 JSON 数组，不要其他文字。"
        )
        cli = self.llm.bind("ideation", "hypothesis_lead")
        resp = cli.chat(prompt, system=HYPOTHESIS_SYSTEM, max_tokens=6144)
        return extract_json_array(LLMClient.text_of(resp))

    def _placeholder_items(self) -> List[Dict[str, Any]]:
        directions = self.topic.get("sub_directions") or [self.topic.get("name", "")]
        return [{
            "id": f"H{i}",
            "title": f"[placeholder] {d} 的可验证假设",
            "problem_statement": "（未接 LLM 的离线占位：待 hypothesis_lead 生成）",
            "testable_prediction": "占位预测",
            "minimal_experiment": "占位实验",
            "sub_direction": d,
        } for i, d in enumerate(directions[:MIN_HYPOTHESES], start=1)]

    @staticmethod
    def _body_md(item: Dict[str, Any]) -> str:
        return (
            f"# {item.get('title', '')}\n\n"
            f"## 问题陈述\n\n{item.get('problem_statement', '')}\n\n"
            f"## 可验证预测\n\n{item.get('testable_prediction', '')}\n\n"
            f"## 所需最小实验\n\n{item.get('minimal_experiment', '')}\n\n"
            f"## 子方向\n\n{item.get('sub_direction', '')}\n"
        )


def _missing_fields(item: Dict[str, Any]) -> List[str]:
    return [k for k in REQUIRED_HYPO_FIELDS if not str(item.get(k) or "").strip()]
