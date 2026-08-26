# -*- coding: utf-8 -*-
"""test_analysis.py：AnalysisAgent 证据审计（§5.4 Step1）。

覆盖：claim 证据链 100% 指向真实制品、无证据拒绝入蓝图、图数=真实数据、
LLM 只润色文本不得引入新数值（结果幻觉防线）。
"""
import json

import pytest

from minifars.analysis import (AnalysisAgent, build_claims, check_evidence_files,
                               collect_evidence, support_strength)
from minifars.artifacts import validate_blueprint
from minifars.contract import contract_skeleton, save_contract

TOPIC = {"name": "agent_context", "title": "Context Engineering for LLM Agents"}

CARDS = {"schema": "v0", "cards": [
    {"paper_id": "arxiv:2608.11111v1", "title": "ReWorld Interactive Memory",
     "authors": ["Zhifei Chen", "Luozhou Wang"], "published": "2026-08-24",
     "url": "http://arxiv.org/abs/2608.11111v1", "source": "arxiv"},
    {"paper_id": "arxiv:2608.22222v1", "title": "Prime Agent Harness",
     "authors": ["Seth Karten"], "published": "2026-08-24",
     "url": "http://arxiv.org/abs/2608.22222v1", "source": "arxiv"},
]}


def make_project(tmp_path, with_results=True, with_contract=True):
    """合成一个含 contract/results/cards 的可审计项目。"""
    if with_contract:
        plan = tmp_path / "plan"
        plan.mkdir(parents=True, exist_ok=True)
        save_contract(contract_skeleton(), plan / "experiment_contract.yaml")
    proposals = tmp_path / "proposals"
    proposals.mkdir(parents=True, exist_ok=True)
    (proposals / "accepted_proposal.md").write_text(
        "---\nid: P001\ntitle: t\nstage: ideation\nstatus: accepted\n"
        "created_at: 2026-08-26T08:00:00+0800\n---\n\n# accepted\n",
        encoding="utf-8")
    survey = proposals / "survey"
    survey.mkdir(exist_ok=True)
    (survey / "survey_cards.json").write_text(
        json.dumps(CARDS, ensure_ascii=False), encoding="utf-8")
    if with_results:
        results = tmp_path / "exp" / "results"
        results.mkdir(parents=True, exist_ok=True)

        def _res(tid, score, method):
            (results / f"{tid}.json").write_text(json.dumps(
                {"schema": "v0", "task_id": tid,
                 "metrics": {"score": score, "method": method, "n_seeds": 2,
                             "per_seed": {"0": {"score": score}}}},
                ensure_ascii=False), encoding="utf-8")
            # run_meta 与指标成对（C5 可复现性 claim 的证据）
            (results / f"{tid}.run_meta.json").write_text(json.dumps(
                {"schema": "v0", "task_id": tid, "task_type": "baselines",
                 "command": f"python exp/code/{tid}.py", "seed": 0,
                 "model": "synthetic-v0", "started_at": "2026-08-26T10:00:00+0800",
                 "finished_at": "2026-08-26T10:00:01+0800", "status": "ok"},
                ensure_ascii=False), encoding="utf-8")
        _res("B1", 0.4500, "random_baseline")
        _res("B2", 0.6000, "topk_retrieval_baseline")
        _res("M1", 0.6600, "proposed_context_compression")
        (results / "gate_verdict.json").write_text(json.dumps(
            {"passed": False,
             "reason": "main(0.6600) - baseline(0.6000) = +0.0600 < threshold 0.99",
             "main_value": 0.66, "baseline_value": 0.60,
             "direction": "gt", "threshold": 0.99}), encoding="utf-8")
        (results / "negative_result.json").write_text(json.dumps(
            {"schema": "v0", "kind": "negative_result", "hypothesis": "h",
             "predict": "p",
             "gate": {"passed": False, "main_value": 0.66},
             "note": "analysis 已按契约 on_fail 跳过"}, ensure_ascii=False),
            encoding="utf-8")
    return tmp_path


# ------------------------------------------------------------------ evidence
def test_claims_fully_linked_to_artifacts(tmp_path):
    project = make_project(tmp_path)
    ev = collect_evidence(project)
    claims = build_claims(ev)
    ids = [c["id"] for c in claims]
    assert ids == ["C1", "C2", "C3", "C4", "C5"]
    assert check_evidence_files({"claims": claims}, project) == []
    # C2 数值必须来自制品（主实验 0.66，margin 对最强基线 B2 0.60）
    c2 = claims[1]["text"]
    assert "0.6600" in c2 and "0.6000" in c2 and "B2" in c2
    # 门未过 + margin 为正 → moderate
    assert claims[1]["evidence"][0]["support_strength"] == "moderate"


def test_analysis_offline_blueprint(tmp_path):
    project = make_project(tmp_path)
    agent = AnalysisAgent(TOPIC, None, project, project / "paper")
    out = agent.run()
    bp = json.loads(open(out["blueprint"], encoding="utf-8").read())
    assert validate_blueprint(bp) == []
    assert check_evidence_files(bp, project) == []  # 验收：100% 可点开核对
    # 图数=真实数据：fig1 由绘图脚本读 results 生成
    fig1 = (project / "paper" / "figures" / "fig1.tex").read_text(encoding="utf-8")
    assert "0.6600" in fig1 and "(B1,0.4500)" in fig1
    assert (project / "paper" / "figures" / "fig2.tex").exists()


def test_no_artifacts_raises(tmp_path):
    """无契约无结果 → 拒绝产出空蓝图（宁停勿幻觉）。"""
    (tmp_path / "proposals").mkdir()
    with pytest.raises(RuntimeError, match="无可用制品证据"):
        AnalysisAgent(TOPIC, None, tmp_path, tmp_path / "paper").run()


def test_missing_evidence_file_detected(tmp_path):
    project = make_project(tmp_path)
    bp = {"claims": [{"id": "C1", "text": "t", "section": "introduction",
                      "evidence": [{"source_artifact": "exp/results/NOPE.json"}]}]}
    problems = check_evidence_files(bp, project)
    assert len(problems) == 1 and "NOPE.json" in problems[0]


def test_support_strength_levels():
    assert support_strength({"passed": True}, 0.5) == "strong"
    assert support_strength({"passed": False}, 0.05) == "moderate"
    assert support_strength({"passed": False}, -0.01) == "weak"


# ------------------------------------------------------------------ refine
def _refine_payload(**overrides):
    base = {
        "C1": "We study the hypothesis from the accepted proposal.",
        "C2": "The proposed method scores 0.6600 with a margin of +0.0600 "
              "over baseline B2 at 0.6000.",
        "C3": "The pre-registered gate does not meet its registered threshold.",
        "C4": "We honestly report a negative result.",
        "C5": "Every number is reproducible via seeded scripts.",
    }
    base.update(overrides)
    return json.dumps({"claims": [{"id": k, "text": v}
                                  for k, v in base.items()]},
                      ensure_ascii=False)


def test_llm_refine_accepts_rephrase(tmp_path, fake_llm):
    project = make_project(tmp_path)
    llm = fake_llm([_refine_payload()])
    agent = AnalysisAgent(TOPIC, llm, project, project / "paper")
    agent.run()
    assert llm.last_bind == ("writing", "analysis")  # 归因正确
    bp = json.loads((project / "paper" / "blueprint.json").read_text(encoding="utf-8"))
    assert bp["claims"][3]["text"] == "We honestly report a negative result."


def test_llm_refine_rejects_new_numbers(tmp_path, fake_llm):
    """LLM 引入证据中不存在的数值 = 结果幻觉信号 → 该条回落确定性文本。"""
    project = make_project(tmp_path)
    llm = fake_llm([_refine_payload(
        C2="Our method reaches 0.9999 recall, a huge gain over 0.6000.")])
    agent = AnalysisAgent(TOPIC, llm, project, project / "paper")
    agent.run()
    bp = json.loads((project / "paper" / "blueprint.json").read_text(encoding="utf-8"))
    c2 = next(c for c in bp["claims"] if c["id"] == "C2")
    assert "0.9999" not in c2["text"] and "0.6600" in c2["text"]


def test_llm_refine_fallback_on_bad_json(tmp_path, fake_llm):
    project = make_project(tmp_path)
    llm = fake_llm(["这不是 JSON"])
    agent = AnalysisAgent(TOPIC, llm, project, project / "paper")
    agent.run()  # 不抛错：回落确定性文本
    bp = json.loads((project / "paper" / "blueprint.json").read_text(encoding="utf-8"))
    assert validate_blueprint(bp) == []
