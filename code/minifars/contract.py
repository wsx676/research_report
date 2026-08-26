# -*- coding: utf-8 -*-
"""experiment_contract.yaml schema 与校验器（设计文档 §5.2）。

契约是机器可读的实验计划，Experiment Swarm 直接解析执行：
- 五类任务中 env_setup/baselines/main/analysis 为任务列表，
  effectiveness_gate 是主实验后的阈值判定配置（不是可执行任务）；
- 强校验：baselines ≥ 2、analysis（含消融）≥ 1——SAR 审稿中
  "Experiments & Evaluation" 是最高频 weakness（FARS 数据 92.9% 评审提及）；
- budget 字段与 MeteringMiddleware/墙钟看门狗联动，超限自动降级。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List

import yaml

from .artifacts import SCHEMA_VERSION
from .skills import METRIC_ALIASES, resolve_metric

#: 执行层可产出的指标键（合成基准 v1：score 及别名映射目标）
KNOWN_RESULT_KEYS = set(METRIC_ALIASES.values()) | {"score"}

#: 任务类别（effectiveness_gate 是判定配置，不在任务执行序列中）
TASK_CATEGORIES = ("env_setup", "baselines", "main", "analysis")
#: 任务执行顺序（§5.3 Orchestrator 按此顺序消费契约）
TASK_ORDER = ("env_setup", "baselines", "main", "analysis")

#: 每个任务必填字段（id 用于 results/<task_id>.json 命名，禁止重复）
REQUIRED_TASK_FIELDS = ("id", "name", "method", "metric")
#: 有效性门必填字段
REQUIRED_GATE_FIELDS = ("metric", "direction", "threshold")

#: 强校验下限（§5.2 设计要点）
MIN_BASELINES = 2
MIN_ANALYSIS = 1


class ContractError(ValueError):
    """契约非法（schema 校验失败）；Orchestrator 拒绝执行非法契约。"""


def validate_contract(contract: Dict[str, Any]) -> List[str]:
    """返回问题清单（空 = 通过）。检查结构、数量下限、id 唯一性与预算字段。"""
    problems: List[str] = []
    if not str(contract.get("hypothesis") or "").strip():
        problems.append("hypothesis 为空")
    if not str(contract.get("predict") or "").strip():
        problems.append("predict 为空")

    tasks = contract.get("tasks")
    if not isinstance(tasks, dict):
        problems.append("tasks 必须是映射（五类任务结构）")
        return problems

    seen: set = set()
    counts = {c: 0 for c in TASK_CATEGORIES}
    for cat in TASK_CATEGORIES:
        items = tasks.get(cat) or []
        if not isinstance(items, list):
            problems.append(f"tasks.{cat} 必须是列表")
            continue
        for idx, t in enumerate(items):
            if not isinstance(t, dict):
                problems.append(f"tasks.{cat}[{idx}] 不是映射")
                continue
            missing = [k for k in REQUIRED_TASK_FIELDS
                       if not str(t.get(k) or "").strip()]
            if missing:
                problems.append(f"tasks.{cat}[{idx}] 缺字段 {missing}")
            metric = str(t.get("metric") or "")
            if metric and resolve_metric(metric) not in KNOWN_RESULT_KEYS:
                problems.append(f"tasks.{cat}[{idx}] 指标 {metric!r} 不在执行层"
                                f"已知指标表 {sorted(KNOWN_RESULT_KEYS)}")
            tid = str(t.get("id") or "")
            if tid:
                if tid in seen:
                    problems.append(f"任务 id 重复: {tid}")
                seen.add(tid)
            counts[cat] += 1

    if counts["baselines"] < MIN_BASELINES:
        problems.append(f"baselines 数量 {counts['baselines']} < {MIN_BASELINES}"
                        "（§5.2：≥2 个已发表方法基线）")
    if not tasks.get("main"):
        problems.append("main 为空（必须有主实验）")
    if counts["analysis"] < MIN_ANALYSIS:
        problems.append(f"analysis 数量 {counts['analysis']} < {MIN_ANALYSIS}"
                        "（§5.2：消融/敏感性至少 1 个）")

    gate = tasks.get("effectiveness_gate")
    if not isinstance(gate, dict):
        problems.append("tasks.effectiveness_gate 必须是映射")
    else:
        missing = [k for k in REQUIRED_GATE_FIELDS if k not in gate]
        if missing:
            problems.append(f"effectiveness_gate 缺字段 {missing}")
        if gate.get("direction") not in ("gt", "lt", None):
            problems.append(f"effectiveness_gate.direction 非法: "
                            f"{gate.get('direction')!r}（仅 gt/lt）")
        ref = str(gate.get("metric") or "")
        if "." in ref:
            key = ref.split(".", 1)[1]
            if resolve_metric(key) not in KNOWN_RESULT_KEYS:
                problems.append(f"effectiveness_gate.metric 指标 {key!r} 不在"
                                f"执行层已知指标表 {sorted(KNOWN_RESULT_KEYS)}")

    budget = contract.get("budget")
    if not isinstance(budget, dict):
        problems.append("budget 必须是映射")
    else:
        for key in ("llm_tokens_total", "per_task_timeout_s", "seeds_per_task"):
            v = budget.get(key)
            if not isinstance(v, int) or v <= 0:
                problems.append(f"budget.{key} 必须为正整数，实际 {v!r}")
    return problems


def extract_json_object(text: str) -> Dict[str, Any]:
    """从 LLM 输出中提取 JSON 对象（容错：围栏/前后噪声；失败返回 {}）。"""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)
    raw = fenced.group(1) if fenced else None
    if raw is None:
        m = re.search(r"\{.*\}", text, flags=re.S)
        raw = m.group(0) if m else None
    if raw is None:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def flatten_tasks(contract: Dict[str, Any]) -> List[Dict[str, Any]]:
    """按 TASK_ORDER 展平任务列表，每项附带 task_type（run_meta 用）。"""
    flat: List[Dict[str, Any]] = []
    tasks = contract.get("tasks") or {}
    for cat in TASK_ORDER:
        for t in tasks.get(cat) or []:
            item = dict(t)
            item["task_type"] = cat
            flat.append(item)
    return flat


def save_contract(contract: Dict[str, Any], path: Path | str) -> Path:
    """落盘契约（YAML，保留键序）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(contract, allow_unicode=True, sort_keys=False),
                 encoding="utf-8")
    return p


def load_contract(path: Path | str) -> Dict[str, Any]:
    """读回并校验契约；非法抛 ContractError。"""
    p = Path(path)
    if not p.exists():
        raise ContractError(f"契约不存在: {p}")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    problems = validate_contract(data)
    if problems:
        raise ContractError(f"契约校验失败: {problems}")
    return data


def contract_skeleton() -> Dict[str, Any]:
    """合法契约骨架（dry-run 占位与 PlannerAgent 兜底共用）。"""
    return {
        "schema": SCHEMA_VERSION,
        "hypothesis": "[占位] 中心假设待 PlannerAgent 从 accepted_proposal 提炼",
        "predict": "[占位] 可量化预测",
        "tasks": {
            "env_setup": [{"id": "E1", "name": "环境与数据准备",
                           "method": "env_check", "metric": "score",
                           "seeds": [0]}],
            "baselines": [
                {"id": "B1", "name": "基线一（占位）",
                 "method": "random_baseline", "metric": "score",
                 "seeds": [0, 1]},
                {"id": "B2", "name": "基线二（占位）",
                 "method": "topk_retrieval_baseline", "metric": "score",
                 "seeds": [0, 1]},
            ],
            "main": [{"id": "M1", "name": "主实验（占位）",
                      "method": "proposed_context_compression", "metric": "score",
                      "seeds": [0, 1]}],
            "effectiveness_gate": {
                "metric": "M1.score", "direction": "gt", "threshold": 0.0,
                "compare_to": "max_baseline.score",
                "on_fail": "early_stop_keep_negative",
            },
            "analysis": [{"id": "A1", "name": "消融（占位）",
                          "method": "ablation_no_compression", "metric": "score",
                          "seeds": [0]}],
        },
        "budget": {"llm_tokens_total": 1500000, "per_task_timeout_s": 30,
                   "seeds_per_task": 2},
    }
