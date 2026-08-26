# -*- coding: utf-8 -*-
"""test_planner.py：PlannerAgent（§5.2 accepted_proposal → 契约）。"""
import json

import pytest

from minifars.contract import load_contract
from minifars.planner import PlannerAgent

TOPIC = {"name": "agent_context", "title": "Context Engineering for LLM Agents",
         "budget": {"llm_tokens_total": 1500000, "experiment_wall_clock_min": 120,
                    "seeds_per_task": 2}}

VALID_CONTRACT = {
    "hypothesis": "上下文压缩可提升长程任务完成率",
    "predict": "主实验 score 高于最强基线 ≥ 0.02",
    "tasks": {
        "env_setup": [{"id": "E1", "name": "环境自检", "method": "env_check",
                       "metric": "score", "seeds": [0]}],
        "baselines": [
            {"id": "B1", "name": "随机基线", "method": "random_baseline",
             "metric": "score", "seeds": [0, 1]},
            {"id": "B2", "name": "TopK 检索基线", "method": "topk_retrieval_baseline",
             "metric": "score", "seeds": [0, 1]},
        ],
        "main": [{"id": "M1", "name": "主实验", "method": "proposed_context_compression",
                  "metric": "score", "seeds": [0, 1]}],
        "effectiveness_gate": {"metric": "M1.score", "direction": "gt",
                               "threshold": 0.0, "compare_to": "max_baseline.score",
                               "on_fail": "early_stop_keep_negative"},
        "analysis": [{"id": "A1", "name": "消融", "method": "ablation_no_compression",
                      "metric": "score", "seeds": [0]}],
    },
    "budget": {"llm_tokens_total": 1500000, "per_task_timeout_s": 30,
               "seeds_per_task": 2},
}


def _write_accepted(tmp_path):
    p = tmp_path / "proposals" / "accepted_proposal.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\nid: P006\n---\n\n# 中心假设\n压缩上下文提升完成率。\n",
                 encoding="utf-8")
    return p


def test_offline_skeleton_contract(tmp_path, fake_llm):
    accepted = _write_accepted(tmp_path)
    planner = PlannerAgent(TOPIC, None, tmp_path / "plan")
    out = planner.run(accepted)
    contract = load_contract(out["contract"])  # 骨架必须通过校验
    assert contract["derived_from"] == str(accepted)


def test_generate_valid_contract(tmp_path, fake_llm):
    accepted = _write_accepted(tmp_path)
    llm = fake_llm([json.dumps(VALID_CONTRACT, ensure_ascii=False)])
    planner = PlannerAgent(TOPIC, llm, tmp_path / "plan")
    out = planner.run(accepted)
    contract = load_contract(out["contract"])
    assert contract["hypothesis"] == VALID_CONTRACT["hypothesis"]
    assert contract["derived_from"] == str(accepted)
    assert llm.last_bind == ("planning", "planner")  # 归因正确
    assert "baselines ≥ 2" in llm.prompts[0]  # 硬性要求写入 prompt


def test_retry_once_on_invalid_then_success(tmp_path, fake_llm):
    bad = json.dumps({"hypothesis": "x"})  # 缺 tasks/predict/budget
    good = json.dumps(VALID_CONTRACT, ensure_ascii=False)
    llm = fake_llm([bad, good])
    planner = PlannerAgent(TOPIC, llm, tmp_path / "plan")
    out = planner.run(_write_accepted(tmp_path))
    assert len(llm.prompts) == 2  # 校验错误回填重试一次
    assert "未通过校验" in llm.prompts[1]
    assert load_contract(out["contract"])["predict"]


def test_two_invalid_outputs_raise(tmp_path, fake_llm):
    llm = fake_llm(['{"hypothesis": "x"}', '{"predict": "y"}'])
    planner = PlannerAgent(TOPIC, llm, tmp_path / "plan")
    with pytest.raises(Exception, match="两次产出均未通过校验"):
        planner.run(_write_accepted(tmp_path))


def test_unparseable_output_raises(tmp_path, fake_llm):
    llm = fake_llm(["完全没有 JSON 的回复", "还是没有"])
    planner = PlannerAgent(TOPIC, llm, tmp_path / "plan")
    with pytest.raises(Exception):
        planner.run(_write_accepted(tmp_path))


def test_budget_defaults_injected(tmp_path, fake_llm):
    topic = {"name": "t", "budget": {}}  # 全缺省
    llm = fake_llm([json.dumps(VALID_CONTRACT, ensure_ascii=False)])
    planner = PlannerAgent(topic, llm, tmp_path / "plan")
    planner.run(_write_accepted(tmp_path))
    prompt = llm.prompts[0]
    assert "1500000" in prompt  # 缺省 token 预算注入
    assert "seeds_per_task = 2" in prompt
