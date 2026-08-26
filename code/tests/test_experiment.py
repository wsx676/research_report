# -*- coding: utf-8 -*-
"""test_experiment.py：Experiment Swarm 执行引擎（§5.3）。

覆盖验收标准：run_meta 五要素、有效性门两条路径（通过/早停负结果）、
断点续跑、预算降级（错误路径）；评审回归：C1 时间戳、C2 续跑门恢复、
M1 基线缺失、m1 direction 缺省、m3 墙钟持久化。
"""
import json
import time

import pytest

from minifars.artifacts import validate_run_meta
from minifars.contract import save_contract
from minifars.experiment import (ExperimentOrchestrator, evaluate_effectiveness,
                                 read_result, run_experiment_stage)
from minifars.skills import (SYNTHETIC_SCORES, parse_result_output,
                             render_task_script)


def make_contract(tmp_path, threshold=0.0, main_method="proposed_context_compression"):
    """构造合法契约：2 基线 + 主实验 + 消融（合成方法名对齐技能库）。"""
    contract = {
        "schema": "v0",
        "hypothesis": "H：压缩提升完成率",
        "predict": "M1.score - max(B).score ≥ 阈值",
        "tasks": {
            "env_setup": [{"id": "E1", "name": "环境自检", "method": "env_check",
                           "metric": "score", "seeds": [0]}],
            "baselines": [
                {"id": "B1", "name": "随机基线", "method": "random_baseline",
                 "metric": "score", "seeds": [0, 1]},
                {"id": "B2", "name": "TopK 基线", "method": "topk_retrieval_baseline",
                 "metric": "score", "seeds": [0, 1]},
            ],
            "main": [{"id": "M1", "name": "主实验", "method": main_method,
                      "metric": "score", "seeds": [0, 1]}],
            "effectiveness_gate": {"metric": "M1.score", "direction": "gt",
                                   "threshold": threshold,
                                   "compare_to": "max_baseline.score",
                                   "on_fail": "early_stop_keep_negative"},
            "analysis": [{"id": "A1", "name": "消融", "method": "ablation_no_compression",
                          "metric": "score", "seeds": [0]}],
        },
        "budget": {"llm_tokens_total": 1500000, "per_task_timeout_s": 60,
                   "seeds_per_task": 2},
    }
    return save_contract(contract, tmp_path / "plan" / "experiment_contract.yaml")


def test_full_run_gate_passed(tmp_path):
    contract_path = make_contract(tmp_path)
    commits = []
    orch = ExperimentOrchestrator(tmp_path, contract_path,
                                  commit_fn=lambda m: commits.append(m) or "sha")
    summary = orch.run()
    assert summary["completed"] == ["E1", "B1", "B2", "M1", "A1"]
    assert summary["gate"]["passed"] is True  # proposed(0.66) > topk(0.60)
    assert summary["skipped"] == {}
    assert len(commits) == 5  # 每任务一个检查点提交

    results = tmp_path / "exp" / "results"
    for tid in ("E1", "B1", "B2", "M1", "A1"):
        metrics = read_result(results, tid)
        assert "score" in metrics
        meta = json.loads((results / f"{tid}.run_meta.json")
                          .read_text(encoding="utf-8"))
        assert validate_run_meta(meta) == []          # 验收 D3-1：五要素合规
        assert meta["command"] and meta["model"] == "synthetic-v0"
        assert meta["tokens"] == {"in": 0, "out": 0}
        assert (results / f"{tid}.json").exists()
    assert (results / "gate_verdict.json").exists()
    assert (results / "run_summary.json").exists()
    assert not (results / "negative_result.json").exists()
    assert (tmp_path / "exp" / "code" / "M1.py").exists()      # 生成脚本留痕
    assert (tmp_path / "exp" / "logs" / "M1.log").exists()     # 日志留痕
    # 确定性复现：同 seed 同 method 分数完全一致
    assert read_result(results, "M1")["per_seed"] == \
        json.loads((tmp_path / "exp" / "results" / "M1.json")
                   .read_text(encoding="utf-8"))["metrics"]["per_seed"]


def test_gate_failed_skips_analysis_keeps_negative(tmp_path):
    # threshold 拉高到 0.5：margin ≈ 0.06 必失败 → 早停保留负结果
    contract_path = make_contract(tmp_path, threshold=0.5)
    orch = ExperimentOrchestrator(tmp_path, contract_path,
                                  commit_fn=lambda m: "sha")
    summary = orch.run()
    assert summary["gate"]["passed"] is False
    assert "A1" not in summary["completed"]
    assert summary["skipped"] == {"A1": "early_stop_keep_negative"}
    neg = json.loads((tmp_path / "exp" / "results" / "negative_result.json")
                     .read_text(encoding="utf-8"))
    assert neg["kind"] == "negative_result"
    assert neg["gate"]["passed"] is False
    assert not (tmp_path / "exp" / "results" / "A1.json").exists()


def test_checkpoint_resume_no_double_commit(tmp_path):
    contract_path = make_contract(tmp_path)
    commits = []
    orch = ExperimentOrchestrator(tmp_path, contract_path,
                                  commit_fn=lambda m: commits.append(m) or "sha")
    orch.run()
    assert len(commits) == 5

    # 模拟进程重启：新 Orchestrator 从检查点续跑，全部跳过、不重复提交
    orch2 = ExperimentOrchestrator(tmp_path, contract_path,
                                   commit_fn=lambda m: commits.append(m) or "sha")
    summary2 = orch2.run()
    assert len(summary2["completed"]) == 5
    assert len(commits) == 5  # 幂等：无新提交
    state = json.loads((tmp_path / "exp" / "checkpoints" /
                        "checkpoint_state.json").read_text(encoding="utf-8"))
    assert set(state) == {"E1", "B1", "B2", "M1", "A1"}


def test_partial_resume_runs_only_pending(tmp_path):
    contract_path = make_contract(tmp_path)
    orch = ExperimentOrchestrator(tmp_path, contract_path,
                                  commit_fn=lambda m: "sha")
    orch.run()
    # 模拟中断：删除 A1 的检查点与结果，重跑只补 A1
    state_path = tmp_path / "exp" / "checkpoints" / "checkpoint_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    del state["A1"]
    state_path.write_text(json.dumps(state), encoding="utf-8")
    (tmp_path / "exp" / "results" / "A1.json").unlink()

    executed = []
    orig_run_task = ExperimentOrchestrator._run_task

    def spy(self, task, timeout_s):
        executed.append(task["id"])
        return orig_run_task(self, task, timeout_s)

    ExperimentOrchestrator._run_task = spy
    try:
        orch2 = ExperimentOrchestrator(tmp_path, contract_path,
                                       commit_fn=lambda m: "sha")
        orch2.run()
    finally:
        ExperimentOrchestrator._run_task = orig_run_task
    assert executed == ["A1"]  # 只补跑未完成任务
    assert (tmp_path / "exp" / "results" / "A1.json").exists()


def test_execution_error_recorded_not_crash(tmp_path):
    contract_path = make_contract(tmp_path)
    orch = ExperimentOrchestrator(tmp_path, contract_path,
                                  commit_fn=lambda m: "sha",
                                  python_exe="nonexistent_python_xyz")
    summary = orch.run()  # 沙箱解释器不存在 → 全任务 error，不崩溃
    results = tmp_path / "exp" / "results"
    meta = json.loads((results / "E1.run_meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "error"
    assert summary["gate"]["passed"] is False  # 主实验指标缺失 → 门失败
    assert summary["skipped"].get("A1") == "early_stop_keep_negative"


def test_invalid_contract_rejected(tmp_path):
    contract_path = make_contract(tmp_path)
    import yaml
    data = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    data["tasks"]["baselines"] = []  # 破坏强校验
    contract_path.write_text(yaml.safe_dump(data, allow_unicode=True),
                             encoding="utf-8")
    with pytest.raises(Exception, match="baselines"):
        run_experiment_stage(tmp_path, contract_path)


def test_evaluate_effectiveness_missing_main(tmp_path):
    contract = {"tasks": {"effectiveness_gate": {
        "metric": "M1.score", "direction": "gt", "threshold": 0.0}}}
    verdict = evaluate_effectiveness(contract, tmp_path)
    assert verdict["passed"] is False
    assert "缺失" in verdict["reason"]


def test_evaluate_effectiveness_absolute_threshold(tmp_path):
    # 无 compare_to：绝对阈值比较
    results = tmp_path / "results"
    results.mkdir()
    (results / "M1.json").write_text(json.dumps(
        {"metrics": {"score": 0.7}}), encoding="utf-8")
    contract = {"tasks": {"effectiveness_gate": {
        "metric": "M1.score", "direction": "gt", "threshold": 0.65}}}
    assert evaluate_effectiveness(contract, results)["passed"] is True
    contract["tasks"]["effectiveness_gate"]["threshold"] = 0.75
    assert evaluate_effectiveness(contract, results)["passed"] is False


def test_run_meta_timestamps_sane(tmp_path):
    """评审 C1：started_at 必须是真实日历时间（曾误把 perf_counter 当 epoch）。"""
    contract_path = make_contract(tmp_path)
    orch = ExperimentOrchestrator(tmp_path, contract_path, commit_fn=lambda m: "sha")
    orch.run()
    results = tmp_path / "exp" / "results"
    for f in sorted(results.glob("*.run_meta.json")):
        meta = json.loads(f.read_text(encoding="utf-8"))
        started, finished = meta["started_at"], meta["finished_at"]
        assert started[:4] >= "2020" and finished[:4] >= "2020"  # 无纪元漂移
        assert started <= finished  # 同格式字典序 = 时间序


def test_resume_restores_gate_verdict(tmp_path):
    """评审 C2：断点续跑恢复有效性门判定——早停语义不被重启绕过。"""
    contract_path = make_contract(tmp_path, threshold=0.5)  # 门必失败
    orch = ExperimentOrchestrator(tmp_path, contract_path, commit_fn=lambda m: "sha")
    s1 = orch.run()
    assert s1["skipped"] == {"A1": "early_stop_keep_negative"}

    # 重启续跑：A1 被早停跳过从未 checkpoint，天然处于 pending——
    # 无门判定恢复时它会被错误执行（这正是 C2 缺陷的触发路径）
    orch2 = ExperimentOrchestrator(tmp_path, contract_path, commit_fn=lambda m: "sha")
    s2 = orch2.run()
    # 门判定从落盘结果重算：A1 仍被早停，不执行、不产生矛盾审计
    assert s2["skipped"] == {"A1": "early_stop_keep_negative"}
    assert s2["gate"]["passed"] is False
    assert not (tmp_path / "exp" / "results" / "A1.json").exists()


def test_gate_baseline_missing_fails(tmp_path):
    """评审 M1：契约指定对照但基线指标全缺 → 比较不可判定，保守失败。"""
    results = tmp_path / "results"
    results.mkdir()
    (results / "M1.json").write_text(json.dumps({"metrics": {"score": 0.9}}),
                                      encoding="utf-8")
    contract = {"tasks": {"baselines": [{"id": "B1"}, {"id": "B2"}],
                          "effectiveness_gate": {
                              "metric": "M1.score", "direction": "gt",
                              "threshold": 0.0,
                              "compare_to": "max_baseline.score"}}}
    verdict = evaluate_effectiveness(contract, results)
    assert verdict["passed"] is False  # 不静默退化为绝对阈值放行
    assert "对照基线指标缺失" in verdict["reason"]


def test_direction_null_defaults_gt(tmp_path):
    """评审 m1：direction 显式 null 缺省 gt 语义，不落入 lt。"""
    results = tmp_path / "results"
    results.mkdir()
    (results / "M1.json").write_text(json.dumps({"metrics": {"score": 0.7}}),
                                      encoding="utf-8")
    (results / "B1.json").write_text(json.dumps({"metrics": {"score": 0.6}}),
                                      encoding="utf-8")
    contract = {"tasks": {"baselines": [{"id": "B1"}],
                          "effectiveness_gate": {
                              "metric": "M1.score", "direction": None,
                              "threshold": 0.0,
                              "compare_to": "max_baseline.score"}}}
    verdict = evaluate_effectiveness(contract, results)
    assert verdict["passed"] is True  # 0.7 > 0.6 按 gt 语义通过
    assert verdict["direction"] == "gt"


def test_wall_clock_persists_across_restart(tmp_path):
    """评审 m3：墙钟基准跨进程持久——重启不重置总预算。"""
    contract_path = make_contract(tmp_path)  # threshold=0 门通过
    orch = ExperimentOrchestrator(tmp_path, contract_path, commit_fn=lambda m: "sha")
    orch.run()
    # 模拟：首跑在 200 分钟前开始（超 120min 预算），A1 检查点丢失需重跑
    marker = tmp_path / "exp" / "results" / ".run_started_at"
    marker.write_text(str(time.time() - 200 * 60), encoding="utf-8")
    state_path = tmp_path / "exp" / "checkpoints" / "checkpoint_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    del state["A1"]
    state_path.write_text(json.dumps(state), encoding="utf-8")

    orch2 = ExperimentOrchestrator(tmp_path, contract_path, commit_fn=lambda m: "sha")
    s2 = orch2.run()
    assert s2["skipped"] == {"A1": "skipped_budget"}  # 总墙钟已超，A1 跳过


# ------------------------------------------------------------------ skills
def test_render_script_is_deterministic():
    task = {"id": "M1", "name": "主实验", "method": "proposed_context_compression",
            "seeds": [0, 1]}
    assert render_task_script(task) == render_task_script(task)
    base, jitter = SYNTHETIC_SCORES["proposed_context_compression"]
    assert base > SYNTHETIC_SCORES["topk_retrieval_baseline"][0]  # 主实验强于基线


def test_parse_result_output():
    assert parse_result_output('x\nRESULT_JSON: {"score": 0.5}\n') == {"score": 0.5}
    assert parse_result_output("没有结果行") == {}
    assert parse_result_output("RESULT_JSON: {坏}") == {}
