# -*- coding: utf-8 -*-
"""paper_pipeline 顶层编排 skill（设计文档 §4.2）。

- 四阶段串行调度：ideation → planning → experiment → writing
- 阶段间不做同步消息传递，只传制品路径（共享工作区交接，可断点续跑）
- 每阶段结束打一个 [stage] git 提交（CheckpointManager 前身，D3 扩展）
- D1 骨架：各阶段产出 schema 合规的占位制品；--smoke 时做 3 次真实极小
  LLM 调用验证全链路（计量流水同步产生真实记录）
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

import yaml

from . import STAGES
from .artifacts import (blueprint_skeleton, proposal_front_matter, run_meta,
                        write_proposal, write_result)
from .config import PipelineConfig, load_env
from .llm import LLMClient, build_client
from .metering import MeteringMiddleware
from .workspace import STAGE_DIRS, commit_stage


@dataclass
class StageContext:
    """阶段上下文：只携带制品路径与配置，不携带上游全文（§6.2 上下文压缩）。"""

    project: Path
    topic: Dict
    artifacts: Dict[str, str] = field(default_factory=dict)  # stage -> 主制品路径
    dry_run: bool = True


# ------------------------------------------------------------------ stages
def stage_ideation(ctx: StageContext, client: Optional[LLMClient]) -> Dict[str, str]:
    """Ideation Swarm（D2 实装）：产出候选假设 proposals/*.md。"""
    proposals_dir = ctx.project / STAGE_DIRS["ideation"]
    pid = "P001"
    if ctx.dry_run or client is None:
        body = (f"# [dry-run] 占位假设\n\n主题：{ctx.topic.get('name')}\n\n"
                "D2 将由 SurveyAgent/HypothesisAgent 生成真实候选。\n")
    else:
        cli = client.bind("ideation", "hypothesis_lead")
        resp = cli.chat(
            f"针对研究方向「{ctx.topic.get('name')}」，用一句话提出一个可验证的候选假设。",
            max_tokens=64)
        body = f"# 候选假设 P001\n\n{LLMClient.text_of(resp)}\n"
    meta = proposal_front_matter(pid, title=f"candidate-{ctx.topic.get('name')}",
                                 status="candidate")
    path = write_proposal(proposals_dir, meta, body)
    return {"proposals": str(path)}


def stage_planning(ctx: StageContext, client: Optional[LLMClient]) -> Dict[str, str]:
    """Planning Swarm（D3 实装）：把 accepted_proposal 翻译为实验契约。"""
    contract_path = ctx.project / STAGE_DIRS["planning"] / "experiment_contract.yaml"
    contract = {
        "schema": "v0",
        "derived_from": ctx.artifacts.get("ideation", "proposals/P001.md"),
        "budget": ctx.topic.get("budget", {}),
        # 五类任务骨架（§5.2）；D3 由 PlannerAgent 填充并强校验 baselines>=2
        "tasks": [
            {"task_id": "t0_env", "task_type": "env_setup"},
            {"task_id": "t1_base", "task_type": "baselines"},
            {"task_id": "t2_main", "task_type": "main"},
            {"task_id": "t3_gate", "task_type": "effectiveness_gate"},
            {"task_id": "t4_analysis", "task_type": "analysis"},
        ],
        "dry_run": ctx.dry_run,
    }
    contract_path.write_text(yaml.safe_dump(contract, allow_unicode=True, sort_keys=False),
                             encoding="utf-8")
    return {"contract": str(contract_path)}


def stage_experiment(ctx: StageContext, client: Optional[LLMClient]) -> Dict[str, str]:
    """Experiment Swarm（D3 实装）：按契约执行任务，指标 + run_meta 落盘。"""
    results_dir = ctx.project / "exp" / "results"
    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    meta = run_meta(task_id="t0_env", task_type="env_setup",
                    command="python -V", seed=0,
                    model=(client.model.name if client else "none"),
                    started_at=now, finished_at=now, status="ok" if not ctx.dry_run else "dry_run",
                    tokens={"in": 0, "out": 0})
    paths = write_result(results_dir, "t0_env", {"placeholder": True}, meta)
    return {"results": str(paths['metrics'].parent)}


def stage_writing(ctx: StageContext, client: Optional[LLMClient]) -> Dict[str, str]:
    """Writing Swarm（D4 实装）：blueprint.json → draft.tex → final.pdf。"""
    paper_dir = ctx.project / STAGE_DIRS["writing"]
    bp = blueprint_skeleton(title=f"Toward {ctx.topic.get('name')}",
                            central_claim="[D4] 由 AnalysisAgent 从制品证据提炼")
    bp_path = paper_dir / "blueprint.json"
    bp_path.write_text(json.dumps(bp, ensure_ascii=False, indent=2), encoding="utf-8")
    (paper_dir / "draft.tex").write_text(
        "% miniFARS draft —— D4 由 DraftAgent 按蓝图逐节写作\n"
        "\\documentclass{article}\n\\begin{document}\n\\title{placeholder}\n"
        "\\maketitle\n\\end{document}\n", encoding="utf-8")
    return {"blueprint": str(bp_path)}


STAGE_FUNCS = {
    "ideation": stage_ideation,
    "planning": stage_planning,
    "experiment": stage_experiment,
    "writing": stage_writing,
}

# --smoke 时每个阶段挑选一个代表性 agent 做一次极小真实调用（仅 ideation/planning/writing）
SMOKE_AGENTS = {"ideation": "hypothesis_lead", "planning": "planner", "writing": "drafter"}


# ------------------------------------------------------------------ pipeline
class PaperPipeline:
    """四阶段串行编排器；阶段产物只有路径进入下游（§4.2）。"""

    def __init__(self, project: Path, topic: Dict, config: PipelineConfig,
                 dry_run: bool = True, smoke: bool = False):
        self.project = project
        self.topic = topic
        self.config = config
        self.dry_run = dry_run
        self.smoke = smoke
        self.metering = MeteringMiddleware(project / "metering", prices=config.prices)
        self.client: Optional[LLMClient] = None
        if not dry_run or smoke:
            env = load_env()
            tier = config.tiers.get("strong")
            if tier is None:
                raise ValueError("config.yaml 缺少 models.strong 分级配置")
            self.client = build_client(env, tier, self.metering)

    def run(self) -> Dict[str, str]:
        ctx = StageContext(project=self.project, topic=self.topic, dry_run=self.dry_run)
        t_stage0 = time.perf_counter()
        for stage in STAGES:
            t0 = time.perf_counter()
            produced = STAGE_FUNCS[stage](ctx, self.client)
            ctx.artifacts.update({stage: next(iter(produced.values()), "")})
            # 编排级流水：阶段耗时 + 该阶段 LLM 调用明细另见 calls.jsonl
            self.metering.record(stage=stage, agent="orchestrator",
                                 model="pipeline",
                                 latency_ms=int((time.perf_counter() - t0) * 1000),
                                 extra={"artifacts": produced, "dry_run": self.dry_run})
            if self.smoke and stage in SMOKE_AGENTS:
                self._smoke_call(stage)  # 流水先于提交落盘，保证入库完整
            sha = commit_stage(self.project, stage,
                               note="dry-run skeleton" if self.dry_run else "stage done")
            print(f"[pipeline] {stage:<10} done  artifacts={produced}  commit={sha}")
        print(f"[pipeline] all stages done in {time.perf_counter() - t_stage0:.1f}s")
        return ctx.artifacts

    def _smoke_call(self, stage: str) -> None:
        """验收用真实极小调用：3 条真实流水，token 消耗可忽略。"""
        cli = self.client.bind(stage, SMOKE_AGENTS[stage])
        # M2 先产 thinking 块，预算需覆盖思考+正文，64 足够一句 "OK"
        resp = cli.chat("Reply with exactly: OK", max_tokens=64)
        print(f"[smoke] {stage}/{SMOKE_AGENTS[stage]} -> "
              f"{LLMClient.text_of(resp).strip()!r}")


def load_topic(path: Path | str) -> Dict:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if "name" not in data:
        raise ValueError(f"{path}: topic.yaml 必须包含 name 字段")
    return data
