# -*- coding: utf-8 -*-
"""GateAgent 单测（全离线）：PR1 QualityGate 组件实岗 + 硬检查 + 熔断。

重点覆盖验收标准 D2：
- accepted_proposal.md 含中心假设/可量化预测/最小实验/评分记录；
- 熔断路径（全不达标输入 → 连续 3 轮无过门 → 强制放行最高分）。
"""
import json

import pytest

from minifars.artifacts import parse_proposal
from minifars.gate import GateAgent

TOPIC = {
    "name": "agent_context",
    "sub_directions": ["compression", "memory"],
    "budget": {"max_tokens": 5000000},
}

HYPOS = [
    {"id": "H1", "title": "假设一", "problem_statement": "问题一",
     "testable_prediction": "recall >= +15%", "minimal_experiment": "loco-bench",
     "sub_direction": "compression"},
    {"id": "H2", "title": "假设二", "problem_statement": "问题二",
     "testable_prediction": "latency -30%", "minimal_experiment": "bench",
     "sub_direction": "memory"},
]


def _write_hypos(tmp_path, items=HYPOS):
    p = tmp_path / "hypotheses.json"
    p.write_text(json.dumps({"schema": "v0", "hypotheses": items},
                            ensure_ascii=False), encoding="utf-8")
    return p


def _scores_json(score, ids=("H1", "H2"), automation=True, budget=True):
    return json.dumps([
        {"id": i, "scores": {"novelty": score, "verifiability": score,
                             "compute_feasibility": score, "boundary_clarity": score},
         "automation_ok": automation, "budget_ok": budget, "rationale": "测试评分"}
        for i in ids], ensure_ascii=False)


def _make_pfiles(tmp_path, items=HYPOS):
    """按 HypothesisAgent 的落盘约定生成 P001.md/P002.md（供终态更新检查）。"""
    from minifars.artifacts import proposal_front_matter, write_proposal
    for idx, it in enumerate(items, start=1):
        meta = proposal_front_matter(f"P{idx:03d}", title=it["title"],
                                     status="candidate",
                                     extra={"sub_direction": it["sub_direction"],
                                            "source": "hypothesis_agent"})
        write_proposal(tmp_path, meta, f"# {it['title']}\n\n正文\n")


class TestGateReview:
    def test_pass_writes_snapshot_and_records(self, tmp_path, fake_llm):
        llm = fake_llm([_scores_json(0.9)])
        agent = GateAgent(TOPIC, llm, proposals_dir=tmp_path)
        decision, path = agent.review(_write_hypos(tmp_path), round_no=1)

        assert decision.passed
        assert decision.candidate_id == "H1"
        assert not decision.forced
        assert llm.last_bind == ("ideation", "gate_agent")

        snap = json.loads((tmp_path / "gate_scores_r1.json").read_text(encoding="utf-8"))
        assert snap["decision"]["passed"] is True
        assert snap["candidates"][0]["weighted"] == pytest.approx(0.9)
        # PR1 组件审计流水（框架贡献实岗证据）
        lines = (tmp_path / "gate_reviews" / "quality_gate_reviews.jsonl") \
            .read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["gate"] == "ideation_gate"
        assert record["decision"]["candidate_id"] == "H1"

    def test_hard_check_blocks_high_score(self, tmp_path, fake_llm):
        # H1 分数 0.9 但 automation_ok=False（需人工标注）：硬检查拦截
        llm = fake_llm([json.dumps([
            {"id": "H1", "scores": {d: 0.9 for d in
                                    ("novelty", "verifiability",
                                     "compute_feasibility", "boundary_clarity")},
             "automation_ok": False, "budget_ok": True, "rationale": "高分但需人工"},
            {"id": "H2", "scores": {d: 0.2 for d in
                                    ("novelty", "verifiability",
                                     "compute_feasibility", "boundary_clarity")},
             "automation_ok": True, "budget_ok": True, "rationale": "低分"},
        ])])
        agent = GateAgent(TOPIC, llm, proposals_dir=tmp_path)
        decision, _ = agent.review(_write_hypos(tmp_path))
        assert not decision.passed
        assert "automation_ok" in decision.hard_check_failures["H1"]

    def test_placeholder_without_llm_never_passes(self, tmp_path):
        agent = GateAgent(TOPIC, None, proposals_dir=tmp_path)
        decision, _ = agent.review(_write_hypos(tmp_path))
        assert not decision.passed  # 0.5 < 0.6 阈值：诚实不过门

    def test_score_alignment_missing_candidate_gets_zero(self, tmp_path, fake_llm):
        # LLM 只评了 H2：H1 缺评按 0 分处理（低分淘汰，不静默丢失）
        llm = fake_llm([_scores_json(0.9, ids=("H2",))])
        agent = GateAgent(TOPIC, llm, proposals_dir=tmp_path)
        decision, _ = agent.review(_write_hypos(tmp_path))
        assert decision.passed
        assert decision.candidate_id == "H2"
        assert decision.weighted["H1"] == pytest.approx(0.0)


class TestCircuitBreaker:
    def test_breaker_fires_after_max_rounds(self, tmp_path, fake_llm):
        """验收标准：全不达标输入 → 连续 3 轮无过门 → 熔断强制放行最高分。"""
        low = _scores_json(0.2)  # 全部 0.2 << 阈值 0.6
        llm = fake_llm([low, low, low])
        agent = GateAgent(TOPIC, llm, proposals_dir=tmp_path, max_rounds=3)
        hypo_path = _write_hypos(tmp_path)

        d1, _ = agent.review(hypo_path, round_no=1)
        assert not d1.passed and not d1.forced
        d2, _ = agent.review(hypo_path, round_no=2)
        assert not d2.passed and not d2.forced
        d3, _ = agent.review(hypo_path, round_no=3)
        # 第 3 轮熔断：强制放行（H1/H2 同分，取先提交者）
        assert d3.passed and d3.forced
        assert d3.candidate_id in ("H1", "H2")
        assert "circuit breaker" in d3.reason
        # 3 轮全部留痕（审计完整性）
        lines = (tmp_path / "gate_reviews" / "quality_gate_reviews.jsonl") \
            .read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 3

    def test_breaker_respects_hard_checks_first(self, tmp_path, fake_llm):
        """熔断池优先硬检查干净的候选：H2 分低但干净 > H1 分高但超预算。"""
        low = json.dumps([
            {"id": "H1", "scores": {d: 0.5 for d in
                                    ("novelty", "verifiability",
                                     "compute_feasibility", "boundary_clarity")},
             "automation_ok": True, "budget_ok": False, "rationale": "超预算"},
            {"id": "H2", "scores": {d: 0.2 for d in
                                    ("novelty", "verifiability",
                                     "compute_feasibility", "boundary_clarity")},
             "automation_ok": True, "budget_ok": True, "rationale": "低分但干净"},
        ])
        llm = fake_llm([low, low, low])
        agent = GateAgent(TOPIC, llm, proposals_dir=tmp_path, max_rounds=3)
        hypo_path = _write_hypos(tmp_path)
        for r in (1, 2, 3):
            decision, _ = agent.review(hypo_path, round_no=r)
        assert decision.passed and decision.forced
        assert decision.candidate_id == "H2"  # 硬检查干净者优先


class TestAccept:
    def test_accept_writes_proposal_and_finalizes_status(self, tmp_path, fake_llm):
        llm = fake_llm([_scores_json(0.9)])
        agent = GateAgent(TOPIC, llm, proposals_dir=tmp_path)
        _make_pfiles(tmp_path)
        decision, _ = agent.review(_write_hypos(tmp_path))
        path = agent.accept(decision, _write_hypos(tmp_path))

        text = path.read_text(encoding="utf-8")
        # 验收标准：四要素 + 评分记录
        assert "## 中心假设" in text
        assert "## 可量化预测" in text
        assert "## 所需最小实验" in text
        assert "## GateAgent 评分记录" in text
        assert "recall >= +15%" in text
        assert "0.900" in text          # 加权总分
        assert "淘汰候选存档" in text  # 被淘汰假设存档（§5.1）
        assert "P002.md" in text

        meta = parse_proposal(path)["meta"]
        assert meta["id"] == "P001"
        assert meta["status"] == "accepted"
        assert meta["gate_score"] == pytest.approx(0.9)
        # P0xx.md 终态：胜者 accepted，其余 rejected
        assert parse_proposal(tmp_path / "P001.md")["meta"]["status"] == "accepted"
        assert parse_proposal(tmp_path / "P002.md")["meta"]["status"] == "rejected"

    def test_accept_marks_forced_status(self, tmp_path, fake_llm):
        low = _scores_json(0.2)
        llm = fake_llm([low, low, low])
        agent = GateAgent(TOPIC, llm, proposals_dir=tmp_path, max_rounds=3)
        _make_pfiles(tmp_path)
        hypo_path = _write_hypos(tmp_path)
        decision = None
        for r in (1, 2, 3):
            decision, _ = agent.review(hypo_path, round_no=r)
        path = agent.accept(decision, hypo_path)
        assert parse_proposal(path)["meta"]["status"] == "forced_accept"
        assert "熔断强制放行" in path.read_text(encoding="utf-8")

    def test_accept_unknown_winner_raises(self, tmp_path, fake_llm):
        llm = fake_llm([_scores_json(0.9)])
        agent = GateAgent(TOPIC, llm, proposals_dir=tmp_path)
        decision, _ = agent.review(_write_hypos(tmp_path))
        with pytest.raises(ValueError):
            agent.accept(decision, tmp_path / "not_exist.json")
