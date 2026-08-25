# -*- coding: utf-8 -*-
"""GateAgent（设计文档 §5.1）：Ideation 阶段出口质量门。

直接消费 jiuwenswarm.common.quality_gate.QualityGate（feature/quality-gate
分支的 PR1 组件，经 workswarm 可编辑安装引入）——这是框架贡献回上游
仓库后在自家流水线里的"实岗"验证。

职责：
1. strong 档 LLM 给每条候选假设打四维 rubric 分（novelty /
   verifiability / compute_feasibility / boundary_clarity，0~1）；
2. 域约束硬检查（§5.1 硬性检查项）：实验可全自动化验证（automation_ok）、
   不超算力预算（budget_ok）——硬检查失败者无论分多高不得过门；
3. 调 QualityGate 组件做加权评分、阈值门与熔断：连续 max_rounds 轮
   无候选过门 → 组件强制放行最高分草案（防止精炼死循环）；
4. 评审记录双落盘：gate_scores_r{n}.json（LLM 打分快照 + 组件判定）+
   gate_reviews/quality_gate_reviews.jsonl（组件审计流水）；
5. accept() 产出 accepted_proposal.md（中心假设/可量化预测/最小实验/
   评分记录/淘汰存档），并把 P0xx.md front-matter 更新为终态。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from jiuwenswarm.common.quality_gate import Candidate, GateConfig, QualityGate

from .artifacts import (load_hypotheses, parse_proposal, proposal_front_matter,
                        write_proposal)
from .hypothesis import extract_json_array
from .llm import LLMClient

#: rubric 四维（与 GateConfig.ideation_default 保持一致）
SCORE_DIMENSIONS = ("novelty", "verifiability", "compute_feasibility",
                    "boundary_clarity")
#: 每假设字段进 prompt 的截断（上下文压缩红线）
FIELD_LIMIT = 200

GATE_SYSTEM = (
    "You are a rigorous evaluation-gate agent for research hypotheses. "
    "Score each hypothesis on the rubric dimensions in [0, 1]. Be honest; "
    "do not inflate scores. Output ONLY a JSON array."
)


def _clamp01(value: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


class GateAgent:
    """Ideation 阶段出口质量门（PR1 QualityGate 组件的科研场景实岗）。"""

    def __init__(self, topic: Dict[str, Any], llm_strong: Optional[LLMClient],
                 proposals_dir: Path, threshold: float = 0.6,
                 max_rounds: int = 3):
        self.topic = topic
        self.llm = llm_strong
        self.proposals_dir = Path(proposals_dir)
        config = GateConfig.ideation_default(threshold=threshold,
                                             max_iterations=max_rounds)
        # 域约束硬检查（§5.1）：可全自动化验证 + 不超算力预算
        config.hard_checks = {
            "automation_ok": lambda c: bool(c.payload.get("automation_ok")),
            "budget_ok": lambda c: bool(c.payload.get("budget_ok")),
        }
        self.gate = QualityGate(config, records_dir=self.proposals_dir / "gate_reviews")
        self.max_rounds = max_rounds
        # 最近一轮 LLM 打分（假设 id -> {scores, rationale, ...}），accept 用
        self._last_scores: Dict[str, Dict[str, Any]] = {}

    @property
    def threshold(self) -> float:
        return self.gate.config.threshold

    # ------------------------------------------------------------- review
    def review(self, hypotheses_path: Path | str,
               round_no: int = 1) -> Tuple[Any, str]:
        """给当前候选打分并过门，返回 (组件 GateDecision, 快照路径)。"""
        items = load_hypotheses(hypotheses_path)
        if self.llm is None:
            scored = self._placeholder_scores(items)
        else:
            scored = self._score(items)

        candidates: List[Candidate] = []
        self._last_scores = {}
        for it, s in zip(items, scored):
            hid = str(it.get("id", ""))
            scores = {d: _clamp01(s.get("scores", {}).get(d)) for d in SCORE_DIMENSIONS}
            candidates.append(Candidate(id=hid, scores=scores,
                                        payload={"automation_ok": bool(s.get("automation_ok")),
                                                 "budget_ok": bool(s.get("budget_ok"))}))
            self._last_scores[hid] = {
                "scores": scores,
                "automation_ok": bool(s.get("automation_ok")),
                "budget_ok": bool(s.get("budget_ok")),
                "rationale": str(s.get("rationale", "")),
            }

        decision = self.gate.review(candidates, iteration=round_no)

        snapshot = {
            "schema": "v0",
            "round": round_no,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "threshold": self.threshold,
            "candidates": [
                {"id": hid, **info, "weighted": decision.weighted.get(hid)}
                for hid, info in self._last_scores.items()
            ],
            "decision": {
                "passed": decision.passed,
                "candidate_id": decision.candidate_id,
                "forced": decision.forced,
                "reason": decision.reason,
                "hard_check_failures": decision.hard_check_failures,
            },
        }
        self.proposals_dir.mkdir(parents=True, exist_ok=True)
        path = self.proposals_dir / f"gate_scores_r{round_no}.json"
        path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        print(f"[gate] round {round_no} passed={decision.passed} "
              f"winner={decision.candidate_id} forced={decision.forced}")
        return decision, str(path)

    # ------------------------------------------------------------- accept
    def accept(self, decision: Any, hypotheses_path: Path | str) -> Path:
        """写出 accepted_proposal.md 并把 P0xx.md 更新为终态（§5.1 出口制品）。"""
        items = load_hypotheses(hypotheses_path)
        hid_to_pid = {str(it.get("id", "")): f"P{i:03d}"
                      for i, it in enumerate(items, start=1)}
        winner = next((it for it in items
                       if str(it.get("id")) == decision.candidate_id), None)
        if winner is None:
            raise ValueError(
                f"gate decision 引用的候选 {decision.candidate_id!r} "
                f"不在 {hypotheses_path}")

        forced = bool(decision.forced)
        status = "forced_accept" if forced else "accepted"
        weighted = decision.weighted.get(decision.candidate_id) or 0.0
        info = self._last_scores.get(decision.candidate_id, {})
        scores = info.get("scores", {})

        rubric_rows = "\n".join(
            f"| {d} | {scores.get(d, 0.0):.2f} | 0.25 |" for d in SCORE_DIMENSIONS)
        eliminated_rows = []
        for it in items:
            hid = str(it.get("id", ""))
            if hid == decision.candidate_id:
                continue
            fails = decision.hard_check_failures.get(hid) or []
            reason = (f"硬检查未过: {', '.join(fails)}" if fails else "低于阈值")
            eliminated_rows.append(
                f"| {hid}（{hid_to_pid[hid]}.md） | "
                f"{decision.weighted.get(hid) or 0.0:.2f} | {reason} |")
        eliminated = "\n".join(eliminated_rows) or "（无）"

        body = (
            f"# {winner.get('title', '')}\n\n"
            f"> 由 GateAgent 在第 {decision.iteration} 轮评审放行"
            f"（{'熔断强制放行' if forced else '过门'}）。本文件是 Ideation 阶段\n"
            f"> 的唯一出口制品，下游 Planning 阶段从此翻译实验契约。\n\n"
            f"## 中心假设\n\n{winner.get('problem_statement', '')}\n\n"
            f"## 可量化预测\n\n{winner.get('testable_prediction', '')}\n\n"
            f"## 所需最小实验\n\n{winner.get('minimal_experiment', '')}\n\n"
            f"## 子方向\n\n{winner.get('sub_direction', '')}\n\n"
            f"## GateAgent 评分记录\n\n"
            f"| 维度 | 得分 | 权重 |\n|---|---|---|\n{rubric_rows}\n\n"
            f"- 加权总分：{weighted:.3f}（阈值 {self.threshold}）\n"
            f"- 评审轮次：{decision.iteration}（熔断预算 {self.max_rounds} 轮）\n"
            f"- 熔断放行：{'是' if forced else '否'}\n"
            f"- 评分理由：{info.get('rationale', '') or '（无）'}\n"
            f"- 审计流水：proposals/gate_reviews/quality_gate_reviews.jsonl\n\n"
            f"## 淘汰候选存档（供 innovation.md 与资源报告引用）\n\n"
            f"| id | 加权分 | 淘汰原因 |\n|---|---|---|\n{eliminated}\n"
        )
        meta = proposal_front_matter(
            hid_to_pid[decision.candidate_id], title=winner.get("title", ""),
            status=status, gate_score=weighted,
            extra={"rounds": decision.iteration, "forced": forced,
                   "source": "gate_agent",
                   "sub_direction": winner.get("sub_direction", "")})
        path = write_proposal(self.proposals_dir, meta, body,
                              filename="accepted_proposal.md")

        self._finalize_pfiles(items, decision, hid_to_pid)
        print(f"[gate] accepted -> {path.name}（status={status}）")
        return path

    # ------------------------------------------------------------ internals
    def _score(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """LLM 批量打分；按 items 顺序对齐，缺评候选给全 0（低分淘汰）。"""
        lines: List[str] = []
        for it in items:
            lines.append(
                f"- id={it.get('id', '')}\n"
                f"  标题：{it.get('title', '')}\n"
                f"  问题：{str(it.get('problem_statement', ''))[:FIELD_LIMIT]}\n"
                f"  预测：{str(it.get('testable_prediction', ''))[:FIELD_LIMIT]}\n"
                f"  实验：{str(it.get('minimal_experiment', ''))[:FIELD_LIMIT]}")
        prompt = (
            f"研究主题：{self.topic.get('title') or self.topic.get('name')}\n"
            f"算力预算（硬约束）：{json.dumps(self.topic.get('budget') or {}, ensure_ascii=False)}\n\n"
            "候选假设：\n" + "\n".join(lines) + "\n\n"
            "对每条假设按以下 rubric 打分（0.0~1.0，诚实评分，不得虚高）：\n"
            "- novelty：与已有工作的差异度\n"
            "- verifiability：预测是否可量化、可由代码自动验证\n"
            "- compute_feasibility：最小实验是否在上述预算内可完成\n"
            "- boundary_clarity：问题边界是否清晰\n\n"
            "同时判定两个域约束（布尔）：\n"
            "- automation_ok：实验能否全自动执行（无需人工标注/人工评估）\n"
            "- budget_ok：是否不超上述算力预算\n\n"
            "输出 JSON 数组，每个元素：\n"
            '{"id": "H1", "scores": {"novelty": 0.0, "verifiability": 0.0, '
            '"compute_feasibility": 0.0, "boundary_clarity": 0.0}, '
            '"automation_ok": true, "budget_ok": true, "rationale": "一句评分理由"}\n'
            "只输出 JSON 数组，不要其他文字。"
        )
        cli = self.llm.bind("ideation", "gate_agent")
        resp = cli.chat(prompt, system=GATE_SYSTEM, max_tokens=4096)
        raw = extract_json_array(LLMClient.text_of(resp))
        by_id = {str(r.get("id")): r for r in raw if r.get("id")}
        return [by_id.get(str(it.get("id", "")),
                          {"id": it.get("id", ""), "scores": {},
                           "rationale": "未评（按 0 分处理）"})
                for it in items]

    @staticmethod
    def _placeholder_scores(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """无 LLM 占位：四维 0.5（诚实表达"未知"，不过 0.6 阈值，
        离线全流程将走熔断路径），域约束默认通过以验证打分逻辑本身。"""
        return [{
            "id": str(it.get("id", "")),
            "scores": {d: 0.5 for d in SCORE_DIMENSIONS},
            "automation_ok": True,
            "budget_ok": True,
            "rationale": "（离线占位：未接 LLM，按 0.5 中性分）",
        } for it in items]

    def _finalize_pfiles(self, items: List[Dict[str, Any]], decision: Any,
                         hid_to_pid: Dict[str, str]) -> None:
        """P0xx.md front-matter 更新为终态；被淘汰文件保留（供审计）。"""
        for it in items:
            hid = str(it.get("id", ""))
            pid = hid_to_pid[hid]
            p = self.proposals_dir / f"{pid}.md"
            if not p.exists():
                continue
            parsed = parse_proposal(p)
            meta = parsed["meta"]
            if hid == decision.candidate_id:
                meta["status"] = "forced_accept" if decision.forced else "accepted"
                meta["gate_score"] = decision.weighted.get(hid) or 0.0
            else:
                meta["status"] = "rejected"
            write_proposal(self.proposals_dir, meta, parsed["body"])
