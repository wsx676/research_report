# -*- coding: utf-8 -*-
"""stage_ideation 完整闭环集成测试（全离线：mock 检索 + FakeLLM 队列）。

覆盖 D2 验收：
- 正常路径：Survey → Hypothesis → Peer 质询 → Gate 过门 → accepted_proposal.md；
- 熔断路径：Gate 恒不过门 → 3 轮 → QualityGate 强制放行最高分（forced_accept）。
"""
import json

import pytest

import minifars.survey as survey
from minifars.artifacts import parse_proposal
from minifars.pipeline import StageContext, stage_ideation
from minifars.survey import Paper

TOPIC = {
    "name": "agent_context",
    "sub_directions": ["compression"],
    "search_filters": {"months_back": 0, "min_citations": 0},
    "budget": {"max_tokens": 5000000},
}


def _hypo_items(n=5, tag=""):
    return [{
        "id": f"H{i}", "title": f"假设{i}{tag}",
        "problem_statement": f"问题陈述{i}",
        "testable_prediction": f"recall >= +{10 + i}%",
        "minimal_experiment": f"最小实验{i}（<=100K tokens）",
        "sub_direction": "compression",
    } for i in range(1, n + 1)]


def _peer_json(items):
    return json.dumps([{
        "id": it["id"], "novelty_concern": "重叠担忧",
        "feasibility_score": 0.6, "major_weakness": "弱点",
        "suggested_fix": "建议",
    } for it in items], ensure_ascii=False)


def _gate_json(items, score):
    return json.dumps([{
        "id": it["id"],
        "scores": {"novelty": score, "verifiability": score,
                   "compute_feasibility": score, "boundary_clarity": score},
        "automation_ok": True, "budget_ok": True, "rationale": "集成测试",
    } for it in items], ensure_ascii=False)


def _mock_search(monkeypatch):
    def fake_arxiv(q, **kw):
        return [Paper(paper_id=f"arxiv:2601.0000{i}", title=f"Card {i}",
                      abstract=f"compression study {i}", source="arxiv",
                      sub_direction=q) for i in range(2)]

    monkeypatch.setattr(survey, "search_arxiv", fake_arxiv)
    monkeypatch.setattr(survey, "search_semantic_scholar",
                        lambda q, **kw: [])


def _ctx(tmp_path, llm_strong, llm_light):
    return StageContext(project=tmp_path, topic=TOPIC, dry_run=False,
                        llm_strong=llm_strong, llm_light=llm_light,
                        metering=None, env={})


class TestIdeationLoop:
    def test_full_loop_accepts_on_first_round(self, tmp_path, monkeypatch, fake_llm):
        _mock_search(monkeypatch)
        items = _hypo_items()
        light = fake_llm(["## compression\n- 空白点1\n- 空白点2"])
        strong = fake_llm([
            json.dumps(items, ensure_ascii=False),   # hypo generate
            _peer_json(items),                       # peer r1
            _gate_json(items, 0.9),                  # gate r1：一次过门
        ])
        produced = stage_ideation(_ctx(tmp_path, strong, light))

        # 出口制品 = accepted_proposal.md（第一个 key，进 ctx.artifacts）
        assert next(iter(produced)) == "accepted"
        acc = tmp_path / "proposals" / "accepted_proposal.md"
        assert str(acc) == produced["accepted"]
        text = acc.read_text(encoding="utf-8")
        assert "## 中心假设" in text
        assert "## 可量化预测" in text
        assert "## 所需最小实验" in text
        assert "## GateAgent 评分记录" in text
        assert "熔断放行：否" in text
        # 胜者 H1（同分取先提交）→ P001 accepted，其余 rejected
        assert parse_proposal(tmp_path / "proposals" / "P001.md")["meta"]["status"] == "accepted"
        assert parse_proposal(tmp_path / "proposals" / "P005.md")["meta"]["status"] == "rejected"
        # 只跑 1 轮：r2/r3 质询文件不存在
        assert (tmp_path / "proposals" / "peer_review_r1.json").exists()
        assert not (tmp_path / "proposals" / "peer_review_r2.json").exists()

    def test_full_loop_breaker_force_accepts(self, tmp_path, monkeypatch, fake_llm):
        """验收标准：全不达标 → 3 轮质询/打分 + 2 轮精炼 → 熔断强制放行。"""
        _mock_search(monkeypatch)
        items = _hypo_items()
        revised = _hypo_items(tag="（修订）")
        light = fake_llm(["## compression\n- 空白点1"])
        strong = fake_llm([
            json.dumps(items, ensure_ascii=False),   # hypo generate
            _peer_json(items), _gate_json(items, 0.2),          # r1
            json.dumps(revised, ensure_ascii=False),            # refine r1
            _peer_json(revised), _gate_json(revised, 0.2),      # r2
            json.dumps(revised, ensure_ascii=False),            # refine r2
            _peer_json(revised), _gate_json(revised, 0.2),      # r3 → 熔断
        ])
        produced = stage_ideation(_ctx(tmp_path, strong, light))

        acc_path = tmp_path / "proposals" / "accepted_proposal.md"
        assert str(acc_path) == produced["accepted"]
        meta = parse_proposal(acc_path)["meta"]
        assert meta["status"] == "forced_accept"
        assert meta["forced"] is True
        text = acc_path.read_text(encoding="utf-8")
        assert "熔断放行：是" in text
        # 3 轮质询 + 2 轮精炼 + 组件 3 条审计流水
        for r in (1, 2, 3):
            assert (tmp_path / "proposals" / f"peer_review_r{r}.json").exists()
        lines = (tmp_path / "proposals" / "gate_reviews" / "quality_gate_reviews.jsonl") \
            .read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 3
        # 精炼后假设落盘（refine 生效）
        hypos = json.loads((tmp_path / "proposals" / "hypotheses.json")
                           .read_text(encoding="utf-8"))
        assert hypos["source"] == "hypothesis_agent_refine"
        assert "修订" in hypos["hypotheses"][0]["title"]

    def test_full_loop_raises_when_all_hard_checks_fail(self, tmp_path, monkeypatch, fake_llm):
        """评审修复：全员硬检查失败 → 熔断 withholding → 清晰 RuntimeError，
        不得放行违反域约束的候选。"""
        _mock_search(monkeypatch)
        items = _hypo_items()
        revised = _hypo_items(tag="（修订）")
        bad_gate = json.dumps([{
            "id": it["id"],
            "scores": {"novelty": 0.9, "verifiability": 0.9,
                       "compute_feasibility": 0.9, "boundary_clarity": 0.9},
            "automation_ok": True, "budget_ok": False, "rationale": "超预算",
        } for it in items], ensure_ascii=False)
        light = fake_llm(["## compression\n- 空白点1"])
        strong = fake_llm([
            json.dumps(items, ensure_ascii=False), _peer_json(items), bad_gate,
            json.dumps(revised, ensure_ascii=False), _peer_json(revised), bad_gate,
            json.dumps(revised, ensure_ascii=False), _peer_json(revised), bad_gate,
        ])
        with pytest.raises(RuntimeError, match="无候选过门"):
            stage_ideation(_ctx(tmp_path, strong, light))

    def test_full_loop_raises_on_unparseable_hypotheses(self, tmp_path, monkeypatch, fake_llm):
        """评审修复：LLM 未产出可解析假设（thinking 吃满预算）→ 空批次计入
        熔断预算，3 轮后清晰 RuntimeError 而非 accept 崩溃。"""
        _mock_search(monkeypatch)
        light = fake_llm(["## compression\n- 空白点1"])
        only_thinking = "[only thinking, no text; 建议增大 max_tokens] 思考中……"
        strong = fake_llm([
            only_thinking, "[]", "[]",          # generate/peer/gate r1
            only_thinking, "[]", "[]",          # refine/peer/gate r2
            only_thinking, "[]", "[]",          # refine/peer/gate r3
        ])
        with pytest.raises(RuntimeError, match="无候选过门"):
            stage_ideation(_ctx(tmp_path, strong, light))

    def test_dry_run_writes_placeholder_accepted(self, tmp_path):
        ctx = StageContext(project=tmp_path, topic=TOPIC, dry_run=True)
        produced = stage_ideation(ctx)
        assert next(iter(produced)) == "accepted"
        acc = parse_proposal(tmp_path / "proposals" / "accepted_proposal.md")
        assert acc["meta"]["status"] == "accepted"
        assert "占位" in acc["body"]
