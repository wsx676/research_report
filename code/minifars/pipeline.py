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
from .analysis import AnalysisAgent
from .artifacts import (blueprint_skeleton, proposal_front_matter,
                        write_proposal)
from .config import PipelineConfig, load_env
from .contract import contract_skeleton, save_contract
from .draft import DraftAgent
from .experiment import run_experiment_stage
from .format_check import FormatAgent
from .gate import GateAgent
from .hypothesis import HypothesisAgent
from .llm import LLMClient, build_client
from .metering import MeteringMiddleware
from .peer import PeerAgent
from .planner import PlannerAgent
from .survey import SurveyAgent
from .workspace import STAGE_DIRS, commit, commit_stage


@dataclass
class StageContext:
    """阶段上下文：只携带制品路径与配置，不携带上游全文（§6.2 上下文压缩）。"""

    project: Path
    topic: Dict
    artifacts: Dict[str, str] = field(default_factory=dict)  # stage -> 主制品路径
    dry_run: bool = True
    llm_strong: Optional[LLMClient] = None   # 假设生成/写作（strong 档）
    llm_light: Optional[LLMClient] = None    # 摘要/格式校验（light 档，§6.2 分级路由）
    metering: Optional[MeteringMiddleware] = None
    env: Dict[str, str] = field(default_factory=dict)


# ------------------------------------------------------------------ stages
def stage_ideation(ctx: StageContext, client: Optional[LLMClient] = None) -> Dict[str, str]:
    """Ideation Swarm（§5.1）：Survey → Hypothesis → Peer 质询 → Gate 打分。

    D2 完整闭环：SurveyAgent 检索 → HypothesisAgent 生成候选 →
    PeerAgent 质询 →（不过门时 Lead 辩论精炼）→ GateAgent 打分过门，
    最多 max_rounds 轮；连续无候选过门由 QualityGate 熔断强制放行
    最高分草案（防精炼死循环）。出口制品 accepted_proposal.md。
    """
    proposals_dir = ctx.project / STAGE_DIRS["ideation"]
    proposals_dir.mkdir(parents=True, exist_ok=True)  # 幂等：支持脱离编排器单测
    if ctx.dry_run:
        meta = proposal_front_matter("P001", title=f"candidate-{ctx.topic.get('name')}",
                                     status="candidate")
        body = (f"# [dry-run] 占位假设\n\n主题：{ctx.topic.get('name')}\n\n"
                "真实运行由 SurveyAgent/HypothesisAgent 生成候选。\n")
        path = write_proposal(proposals_dir, meta, body)
        acc_meta = proposal_front_matter("P001", title=f"accepted-{ctx.topic.get('name')}",
                                         status="accepted",
                                         extra={"source": "dry_run"})
        acc_body = ("# [dry-run] 占位立项报告\n\n"
                    "真实运行由 PeerAgent 质询 + GateAgent 打分产出。\n")
        acc_path = write_proposal(proposals_dir, acc_meta, acc_body,
                                  filename="accepted_proposal.md")
        return {"accepted": str(acc_path), "proposals": str(path)}

    survey = SurveyAgent(ctx.topic, ctx.llm_light,
                         out_dir=proposals_dir / "survey",
                         metering=ctx.metering,
                         s2_api_key=(ctx.env or {}).get("S2_API_KEY"))
    produced = survey.run()
    hypo = HypothesisAgent(ctx.topic, ctx.llm_strong, proposals_dir)
    produced.update(hypo.run(gaps_path=produced["research_gaps"]))

    # D2 下午：PeerAgent 质询 → Lead 精炼 → GateAgent 打分（§5.1 闭环）
    peer = PeerAgent(ctx.topic, ctx.llm_strong, proposals_dir)
    gate = GateAgent(ctx.topic, ctx.llm_strong, proposals_dir)
    decision = None
    for round_no in range(1, gate.max_rounds + 1):
        peer_res = peer.review(produced["hypotheses"], produced["survey_cards"],
                               round_no)
        decision, _ = gate.review(produced["hypotheses"], round_no)
        if decision.passed:
            break
        if round_no < gate.max_rounds:
            hypo.refine(peer_res["items"], gate_feedback={
                "weighted": decision.weighted, "reason": decision.reason,
                "threshold": gate.threshold})
    if not decision.passed:
        # 循环耗尽仍无候选过门：全员硬检查失败（熔断 withholding）或
        # LLM 未产出可解析假设（空批次）。报清晰错误而非崩溃于 accept。
        raise RuntimeError(
            "Ideation 无候选过门（含熔断 withholding）："
            f"最后判定={decision.reason!r}；"
            "请检查 LLM 输出可解析性、max_tokens 预算或主题预算约束")
    accepted = gate.accept(decision, produced["hypotheses"])

    # 主制品（ctx.artifacts['ideation']）= accepted_proposal.md，
    # 下游 planning 从此翻译实验契约；hypotheses.json 等一并交接
    primary = {"accepted": str(accepted), "hypotheses": produced["hypotheses"]}
    primary.update(produced)
    return primary


def stage_planning(ctx: StageContext, client: Optional[LLMClient]) -> Dict[str, str]:
    """Planning Swarm（§5.2，D3 实装）：accepted_proposal → 实验契约。

    PlannerAgent 把过门立项报告翻译为 experiment_contract.yaml，
    强校验 baselines ≥ 2、analysis ≥ 1；契约是实验设计冻结点。
    """
    plan_dir = ctx.project / STAGE_DIRS["planning"]
    plan_dir.mkdir(parents=True, exist_ok=True)  # 幂等：支持脱离编排器单测
    accepted = ctx.artifacts.get("ideation") or str(
        ctx.project / STAGE_DIRS["ideation"] / "accepted_proposal.md")
    if ctx.dry_run:
        contract = contract_skeleton()
        contract["derived_from"] = accepted
        contract["dry_run"] = True
        path = save_contract(contract, plan_dir / "experiment_contract.yaml")
        print(f"[planning] dry-run 契约骨架: {path}")
        return {"contract": str(path)}

    planner = PlannerAgent(ctx.topic, ctx.llm_strong, plan_dir)
    return planner.run(accepted)


def stage_experiment(ctx: StageContext, client: Optional[LLMClient]) -> Dict[str, str]:
    """Experiment Swarm（§5.3，D3 实装）：按契约沙箱执行任务。

    每任务 = 技能库模板渲染 → 沙箱执行 → 指标 + run_meta 五要素落盘；
    main 后过有效性门（失败 → 跳过 analysis、负结果保留）；
    CheckpointManager 每任务 git commit，支持杀进程断点续跑。
    合成基准为确定性离线实验，dry_run 亦走完整执行链路验证引擎。
    """
    contract_path = ctx.artifacts.get("planning") or str(
        ctx.project / STAGE_DIRS["planning"] / "experiment_contract.yaml")
    summary = run_experiment_stage(
        ctx.project, contract_path,
        commit_fn=lambda msg: commit(ctx.project, msg),
        metering=ctx.metering)
    return {"results": summary["results"]}


def stage_writing(ctx: StageContext, client: Optional[LLMClient]) -> Dict[str, str]:
    """Writing Swarm（§5.4，D4 实装）：制品证据 → blueprint.json → draft.tex → PDF。

    AnalysisAgent 证据审计（无证据不入蓝图）→ DraftAgent 按蓝图逐节写作
    （引用只取自 Survey 文献卡片，CitationChecker 逐条校验）→ FormatAgent
    编译 + BibTeX/数值一致性审计。负结果按 candid analysis 诚实成文。
    """
    paper_dir = ctx.project / STAGE_DIRS["writing"]
    paper_dir.mkdir(parents=True, exist_ok=True)  # 幂等：支持脱离编排器单测
    if ctx.dry_run:
        bp = blueprint_skeleton(title=f"Toward {ctx.topic.get('name')}",
                                central_claim="[dry-run] 占位中心论点")
        bp["claims"] = [{
            "id": "C1", "text": "[dry-run] placeholder claim",
            "section": "introduction",
            "evidence": [{"source_artifact": "topic.yaml",
                          "figure_candidate": None,
                          "support_strength": "weak"}]}]
        bp_path = paper_dir / "blueprint.json"
        bp_path.write_text(json.dumps(bp, ensure_ascii=False, indent=2),
                           encoding="utf-8")
        (paper_dir / "draft.tex").write_text(
            "% miniFARS draft —— dry-run 占位（真实运行由 DraftAgent 产出）\n"
            "\\documentclass{article}\n\\begin{document}\n\\title{placeholder}\n"
            "\\maketitle\n\\end{document}\n", encoding="utf-8")
        return {"blueprint": str(bp_path)}

    contract_path = ctx.artifacts.get("planning") or str(
        ctx.project / STAGE_DIRS["planning"] / "experiment_contract.yaml")
    analysis = AnalysisAgent(ctx.topic, ctx.llm_strong, ctx.project,
                             paper_dir, metering=ctx.metering)
    produced = analysis.run(contract_path)
    drafter = DraftAgent(ctx.topic, ctx.llm_strong, ctx.project,
                         paper_dir, metering=ctx.metering)
    produced.update(drafter.run(produced["blueprint"]))
    fmt = FormatAgent(ctx.project, paper_dir, metering=ctx.metering)
    report = fmt.run(draft_path=produced["draft"], bib_path=produced["bib"])
    produced["pdf"] = report["pdf"]
    return produced


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
        self.client: Optional[LLMClient] = None      # strong 档（默认绑定）
        self.client_light: Optional[LLMClient] = None
        self.env: Dict[str, str] = {}
        if not dry_run or smoke:
            self.env = load_env()
            tier = config.tiers.get("strong")
            if tier is None:
                raise ValueError("config.yaml 缺少 models.strong 分级配置")
            self.client = build_client(env=self.env, tier=tier,
                                       metering=self.metering)
            tier_light = config.tiers.get("light") or tier  # 缺省回落 strong
            self.client_light = build_client(env=self.env, tier=tier_light,
                                             metering=self.metering)

    def run(self, stages: Optional[list] | tuple = None) -> Dict[str, str]:
        """按顺序执行阶段子集（默认全部）；断点续跑由 --project-id 配合实现。"""
        plan = [s for s in STAGES if not stages or s in stages]
        ctx = StageContext(project=self.project, topic=self.topic,
                           dry_run=self.dry_run, llm_strong=self.client,
                           llm_light=self.client_light, metering=self.metering,
                           env=self.env)
        t_stage0 = time.perf_counter()
        for stage in plan:
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
