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
from typing import Any, Dict, List, Optional, Tuple

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
        paths, summary_path = self._persist(items, source="hypothesis_agent")
        print(f"[hypothesis] {len(items)} 个候选假设（"
              f"校验问题: {self._validation_of(items) or '无'}）")
        return {"hypotheses": str(summary_path), "proposals": paths}

    def refine(self, peer_items: List[Dict[str, Any]],
               gate_feedback: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """Lead 辩论精炼（§5.1）：按同行质询 + Gate 反馈修订当前候选。

        无 LLM（离线）时恒等返回；修订产出不足 MIN_HYPOTHESES 条时保留原稿。
        """
        data = json.loads((self.proposals_dir / "hypotheses.json")
                          .read_text(encoding="utf-8"))
        items = [d for d in data.get("hypotheses", []) if isinstance(d, dict)]
        if self.llm is None:
            print("[hypothesis] refine：离线模式跳过修订（恒等）")
            return items
        revised = self._refine_generate(items, peer_items, gate_feedback or {})
        revised = [it for it in revised if not _missing_fields(it)][:MAX_HYPOTHESES]
        if len(revised) < MIN_HYPOTHESES:
            print(f"[hypothesis] refine：修订产出 {len(revised)} 条不足 "
                  f"{MIN_HYPOTHESES}，保留原稿")
            return items
        self._persist(revised, source="hypothesis_agent_refine")
        print(f"[hypothesis] refine：{len(items)} -> {len(revised)} 条候选")
        return revised

    # ------------------------------------------------------------ internals
    def _persist(self, items: List[Dict[str, Any]],
                 source: str) -> Tuple[List[str], Path]:
        """重写 P0xx.md + hypotheses.json（refine 后数量可能缩水，先清旧 P*.md）。"""
        for old in self.proposals_dir.glob("P*.md"):
            old.unlink()
        paths: List[str] = []
        for idx, item in enumerate(items, start=1):
            pid = f"P{idx:03d}"
            meta = proposal_front_matter(
                pid, title=item.get("title", ""),
                status="candidate",
                extra={"sub_direction": item.get("sub_direction", ""),
                       "source": source})
            paths.append(str(write_proposal(self.proposals_dir, meta,
                                            self._body_md(item))))

        summary = {
            "schema": "v0",
            "topic": self.topic.get("name"),
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "source": source,
            "count": len(items),
            "validation": validate_hypotheses(items),
            "hypotheses": items,
        }
        summary_path = self.proposals_dir / "hypotheses.json"
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        return paths, summary_path

    @staticmethod
    def _validation_of(items: List[Dict[str, Any]]) -> List[str]:
        return validate_hypotheses(items)

    def _refine_generate(self, items: List[Dict[str, Any]],
                         peer_items: List[Dict[str, Any]],
                         feedback: Dict[str, Any]) -> List[Dict[str, Any]]:
        weighted = feedback.get("weighted", {})
        hypo_lines: List[str] = []
        for it in items:
            hid = str(it.get("id", ""))
            w = weighted.get(hid)
            hypo_lines.append(
                f"- id={hid}（gate 加权分 {f'{w:.2f}' if w is not None else '未评'}）\n"
                f"  标题：{it.get('title', '')}\n"
                f"  预测：{str(it.get('testable_prediction', ''))[:GAPS_INPUT_LIMIT // 8]}\n"
                f"  实验：{str(it.get('minimal_experiment', ''))[:GAPS_INPUT_LIMIT // 8]}")
        peer_lines = [
            f"- {r.get('id', '')}：新颖性担忧={r.get('novelty_concern', '')}；"
            f"主要弱点={r.get('major_weakness', '')}；建议={r.get('suggested_fix', '')}"
            for r in peer_items]
        prompt = (
            f"研究主题：{self.topic.get('title') or self.topic.get('name')}\n"
            f"算力预算（硬约束）：{json.dumps(self.topic.get('budget') or {}, ensure_ascii=False)}\n\n"
            "当前候选假设（含 GateAgent 加权分）：\n" + "\n".join(hypo_lines) + "\n\n"
            "同行质询意见（PeerAgent）：\n" + "\n".join(peer_lines) + "\n\n"
            f"GateAgent 判定：无候选达到阈值（{feedback.get('reason', '')}）。\n\n"
            "请修订候选假设：逐条解决质询指出的弱点与低分维度（新颖性重叠、"
            "预测不可量化、实验超预算等），可合并/替换/新增。保持 "
            f"{MIN_HYPOTHESES}~{MAX_HYPOTHESES} 个，字段 schema 不变：\n"
            '{"id": "H1", "title": "一句话标题", "problem_statement": "问题陈述", '
            '"testable_prediction": "可量化验证的预测", '
            '"minimal_experiment": "所需最小实验", "sub_direction": "所属子方向"}\n'
            "只输出 JSON 数组，不要其他文字。"
        )
        cli = self.llm.bind("ideation", "hypothesis_lead")
        resp = cli.chat(prompt, system=HYPOTHESIS_SYSTEM, max_tokens=6144)
        return extract_json_array(LLMClient.text_of(resp))

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
