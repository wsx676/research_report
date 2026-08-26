# -*- coding: utf-8 -*-
"""策展技能库 skills/（设计文档 §5.3）：已验证的实验代码模板。

ExperimentAgent/Orchestrator 只允许基于这些模板组合生成实验代码，
禁止从零写评测逻辑——压缩错误率与 Token（FARS 机制 2）。

v1 四个模板：
1. benchmark_runner  —— 单方法基准评测（多 seed 聚合，核心执行路径）
2. multi_seed        —— 多 seed 并行聚合（并入 benchmark_runner 的 seeds 参数）
3. plot              —— matplotlib 统一风格绘图（analysis 任务可选调用）
4. llm_judge         —— LLM-as-a-Judge 评分器（开放式任务预留，D3 不消耗 token）

合成基准说明：D3 用确定性合成方法（SYNTHETIC_SCORES）验证执行引擎全链路
（断点续跑/有效性门/预算降级）；正式轮次把真实评测数据集接入
METHOD 分派即可，脚本结构与 run_meta 约定不变。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

#: 模板注册表（名称 → 用途说明；Orchestrator 渲染时校验 method 模板归属）
SKILL_TEMPLATES: Dict[str, str] = {
    "benchmark_runner": "单方法基准评测：固定随机源多 seed 执行，聚合均值落盘",
    "multi_seed": "多 seed 并行聚合（benchmark_runner 的 seeds 参数即此模板）",
    "plot": "matplotlib 统一风格绘图（analysis 可选，缺 matplotlib 时跳过）",
    "llm_judge": "LLM-as-a-Judge 评分器（开放式指标预留，需经 MeteringMiddleware）",
}

#: 确定性合成方法库：方法名 → (基准分, 抖动幅度)。
#: 复现性保证：score = base + Random(f"{method}:{seed}").uniform(-j, j)。
SYNTHETIC_SCORES: Dict[str, tuple] = {
    "env_check": (1.0, 0.0),
    "random_baseline": (0.45, 0.05),
    "topk_retrieval_baseline": (0.60, 0.04),
    "proposed_context_compression": (0.66, 0.04),
    "ablation_no_compression": (0.61, 0.04),
    "ablation_no_rerank": (0.63, 0.04),
    "sensitivity_window_size": (0.64, 0.03),
}

#: 脚本头部注释（注入生成脚本，标明模板来源与禁改约束）
_SCRIPT_HEADER = '''# -*- coding: utf-8 -*-
"""[auto-generated] 技能库模板 benchmark_runner（miniFARS §5.3）。
task: {task_id} / {task_name}
method: {method}
约定：评测逻辑只允许由技能库模板组合，禁止手改；stdout 输出
一行 "RESULT_JSON: {{...}}" 供 Orchestrator 解析。
"""
'''

_SCRIPT_BODY = '''import json
import random

SYNTHETIC_SCORES = {scores!r}


def run_method(method, seed):
    base, jitter = SYNTHETIC_SCORES.get(method, (0.5, 0.05))
    rng = random.Random(f"{{method}}:{{seed}}")
    return {{"score": round(base + rng.uniform(-jitter, jitter), 4)}}


def main():
    seeds = {seeds!r}
    per_seed = {{str(s): run_method({method!r}, s) for s in seeds}}
    mean = sum(v["score"] for v in per_seed.values()) / len(per_seed)
    metrics = {{"score": round(mean, 4), "per_seed": per_seed,
                "n_seeds": len(seeds), "method": {method!r}}}
    # 别名兼容：契约可能用语义化指标名（METRIC_ALIASES），同步输出
    for alias, target in {aliases!r}.items():
        if alias != target:
            metrics[alias] = metrics[target]
    print("RESULT_JSON: " + json.dumps(metrics, ensure_ascii=False))


if __name__ == "__main__":
    main()
'''

#: 生成脚本统一入口名（Orchestrator 以此执行）
RESULT_MARKER = "RESULT_JSON: "

#: 契约 metric 别名 → 合成基准输出键。v1 合成方法只产 score，LLM 规划
#: 契约时可能写语义化指标名（如 recall_security）；别名表做映射兼容，
#: 正式轮次接入真实评测后按数据集扩展此表（禁止静默吞掉未知指标，
#: 见 contract.validate_contract 的一致性校验）。
METRIC_ALIASES: Dict[str, str] = {
    "recall_security": "score",
    "risk_reduction": "score",
    "completion_rate": "score",
    "security_rule_count": "score",
    "env_ok": "score",
    "score": "score",
}


def resolve_metric(name: str) -> str:
    """契约指标名 → 合成基准实际输出键（未知指标原样返回，由读取侧暴露缺失）。"""
    return METRIC_ALIASES.get(str(name), str(name))


def render_task_script(task: Dict[str, Any]) -> str:
    """按 benchmark_runner 模板渲染任务脚本（自包含，可直接 subprocess 执行）。

    Args:
        task: 契约任务条目，需含 id/name/method；可选 seeds 列表。
    """
    seeds = list(task.get("seeds") or [0])
    return (
        _SCRIPT_HEADER.format(task_id=task.get("id"), task_name=task.get("name"),
                              method=task.get("method"))
        + _SCRIPT_BODY.format(scores=SYNTHETIC_SCORES, seeds=seeds,
                              method=task.get("method"),
                              aliases=METRIC_ALIASES)
    )


def parse_result_output(stdout: str) -> Dict[str, Any]:
    """从脚本 stdout 解析 RESULT_JSON 行；无有效行返回 {}。"""
    for line in reversed(stdout.splitlines()):
        if line.startswith(RESULT_MARKER):
            try:
                data = json.loads(line[len(RESULT_MARKER):])
            except json.JSONDecodeError:
                return {}
            return data if isinstance(data, dict) else {}
    return {}


def known_methods() -> List[str]:
    """合成方法清单（PlannerAgent prompt 可选注入，避免 LLM 编造方法名）。"""
    return sorted(SYNTHETIC_SCORES)


def validate_method(method: str) -> bool:
    """方法名是否在技能库已知方法表中（未知 → 合成兜底 0.5±0.05）。"""
    return method in SYNTHETIC_SCORES


def parse_seeds(raw: Any) -> List[int]:
    """容错解析 seeds 字段（契约 YAML 可能给 str/None）。"""
    if isinstance(raw, (list, tuple)):
        return [int(s) for s in raw]
    if raw is None:
        return [0]
    return [int(raw)]


def describe_templates() -> str:
    """技能库说明文本（写入 exp/logs 供审计）。"""
    return "\n".join(f"- {name}: {desc}" for name, desc in SKILL_TEMPLATES.items())


__all__ = ["SKILL_TEMPLATES", "SYNTHETIC_SCORES", "RESULT_MARKER",
           "render_task_script", "parse_result_output", "known_methods",
           "validate_method", "parse_seeds", "describe_templates"]
