# -*- coding: utf-8 -*-
"""PlannerAgent（设计文档 §5.2）：accepted_proposal.md → experiment_contract.yaml。

职责：
1. 读入 GateAgent 过门的 accepted_proposal.md（截断输入，上下文压缩红线）；
2. strong 档 LLM 把中心假设/可量化预测翻译为机器可读契约——五类任务结构，
   强制 baselines ≥ 2、analysis ≥ 1（SAR "Experiments & Evaluation"
   是最高频 weakness，完备实验设计直接对冲评分）；
3. 预算字段从 topic.yaml 注入（与 MeteringMiddleware 联动的硬约束）；
4. 校验失败重试一次（把校验错误回填 prompt），仍失败抛 ContractError——
   契约是 D3 上午的实验设计冻结点，宁停勿带病放行。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from .artifacts import SCHEMA_VERSION
from .contract import (ContractError, contract_skeleton, extract_json_object,
                       save_contract, validate_contract)
from .llm import LLMClient
from .skills import METRIC_ALIASES, known_methods

#: accepted_proposal 读入截断（上下文压缩）
PROPOSAL_INPUT_LIMIT = 6000
#: 契约生成最大 token（M2 先产 thinking，预算需覆盖思考+正文）
PLANNER_MAX_TOKENS = 6144

PLANNER_SYSTEM = (
    "You are an experiment planner for an automated science pipeline. "
    "Translate an accepted research proposal into a machine-readable "
    "experiment contract. Experiments MUST be code-only verifiable, fit "
    "the compute budget, and complete on a single machine. "
    "Output ONLY a JSON object."
)

#: 契约 JSON 的字段说明（嵌入 prompt，保证 schema 一致）
CONTRACT_SPEC = """\
{
  "schema": "v0",
  "hypothesis": "中心假设（一句话，取自立项报告）",
  "predict": "可量化预测（含指标名与方向）",
  "tasks": {
    "env_setup": [{"id": "E1", "name": "...", "method": "...", "metric": "...", "seeds": [0]}],
    "baselines": [{"id": "B1", "name": "已发表方法复现一", "method": "...", "metric": "score", "seeds": [0, 1]}, ...至少 2 个],
    "main": [{"id": "M1", "name": "主实验：方法实现+对比", "method": "...", "metric": "score", "seeds": [0, 1]}],
    "effectiveness_gate": {"metric": "M1.score", "direction": "gt", "threshold": 0.0, "compare_to": "max_baseline.score", "on_fail": "early_stop_keep_negative"},
    "analysis": [{"id": "A1", "name": "消融/敏感性", "method": "...", "metric": "score", "seeds": [0]}, ...至少 1 个]
  },
  "budget": {"llm_tokens_total": <int>, "per_task_timeout_s": <int>, "seeds_per_task": <int>}
}
"""


class PlannerAgent:
    """立项报告 → 实验契约（§5.2 Planning Swarm）。"""

    def __init__(self, topic: Dict[str, Any], llm_strong: Optional[LLMClient],
                 plan_dir: Path | str):
        self.topic = topic
        self.llm = llm_strong
        self.plan_dir = Path(plan_dir)

    def run(self, accepted_path: Path | str) -> Dict[str, Any]:
        """生成并落盘 experiment_contract.yaml；返回 {"contract": 路径}。"""
        proposal = Path(accepted_path).read_text(encoding="utf-8")
        if self.llm is None:
            contract = contract_skeleton()
            contract["derived_from"] = str(accepted_path)
            print("[planner] 离线模式：使用契约骨架占位")
        else:
            contract = self._generate(proposal[:PROPOSAL_INPUT_LIMIT],
                                      str(accepted_path))
        self.plan_dir.mkdir(parents=True, exist_ok=True)
        path = save_contract(contract, self.plan_dir / "experiment_contract.yaml")
        print(f"[planner] 契约已冻结: {path}（baselines="
              f"{len(contract['tasks'].get('baselines', []))}, analysis="
              f"{len(contract['tasks'].get('analysis', []))}）")
        return {"contract": str(path)}

    # ------------------------------------------------------------ internals
    def _generate(self, proposal: str, derived_from: str) -> Dict[str, Any]:
        prompt = self._prompt(proposal)
        contract = self._call(prompt)
        problems = validate_contract(contract)
        if problems:
            # 校验错误回填重试一次：LLM 自纠比人工返工便宜两个数量级
            prompt += ("\n\n上一次输出未通过校验，问题如下，请修正后重新输出："
                       + json.dumps(problems, ensure_ascii=False))
            contract = self._call(prompt)
            problems = validate_contract(contract)
        if problems:
            raise ContractError(f"PlannerAgent 两次产出均未通过校验: {problems}")
        contract["schema"] = SCHEMA_VERSION
        contract["derived_from"] = derived_from
        return contract

    def _call(self, prompt: str) -> Dict[str, Any]:
        cli = self.llm.bind("planning", "planner")
        resp = cli.chat(prompt, system=PLANNER_SYSTEM,
                        max_tokens=PLANNER_MAX_TOKENS)
        return extract_json_object(LLMClient.text_of(resp))

    def _prompt(self, proposal: str) -> str:
        budget = self._budget()
        return (
            f"研究主题：{self.topic.get('title') or self.topic.get('name')}\n"
            f"算力预算（硬约束，直接写入契约 budget 字段）："
            f"{json.dumps(budget, ensure_ascii=False)}\n\n"
            f"已通过的立项报告（accepted_proposal）：\n{proposal}\n\n"
            "请把立项报告翻译为实验契约，严格按以下 JSON schema 输出：\n"
            f"{CONTRACT_SPEC}\n"
            "硬性要求：\n"
            f"1. baselines ≥ 2（复现已发表方法作对照）；analysis ≥ 1（消融/敏感性）；\n"
            f"2. 所有任务必须单机可在 {budget['experiment_wall_clock_min']} 分钟墙钟内跑完，"
            "只用代码可自动验证的指标（禁止人工标注）；\n"
            f"3. budget.llm_tokens_total ≤ {budget['llm_tokens_total']}；"
            f"seeds_per_task = {budget['seeds_per_task']}；"
            f"per_task_timeout_s 建议 ≤ {budget['per_task_timeout_s']}；\n"
            "4. effectiveness_gate.metric 指向 main 任务的指标，direction 取 gt/lt，"
            "on_fail 固定 early_stop_keep_negative；\n"
            f"5. 任务 method 字段只能从技能库已验证方法中选择：{known_methods()}；\n"
            f"6. 任务 metric 与 effectiveness_gate.metric 的指标名只能从已知指标表选择："
            f"{sorted(METRIC_ALIASES)}；\n"
            "只输出 JSON 对象，不要其他文字。"
        )

    def _budget(self) -> Dict[str, Any]:
        """topic.yaml 预算 → 契约 budget 字段（补齐缺省值）。"""
        tb = self.topic.get("budget") or {}
        return {
            "llm_tokens_total": int(tb.get("llm_tokens_total", 1500000)),
            "experiment_wall_clock_min": int(tb.get("experiment_wall_clock_min", 120)),
            "seeds_per_task": int(tb.get("seeds_per_task", 2)),
            "per_task_timeout_s": int(tb.get("per_task_timeout_s", 60)),
        }
