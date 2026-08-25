# -*- coding: utf-8 -*-
"""HypothesisAgent 单测（全离线：FakeLLM + 固定 JSON 响应）。"""
import json

from minifars.artifacts import parse_proposal
from minifars.hypothesis import (HypothesisAgent, extract_json_array,
                                 validate_hypotheses)

TOPIC = {
    "name": "agent_context",
    "title": "Context Engineering for LLM Agents",
    "sub_directions": ["compression", "memory", "failure recovery"],
    "budget": {"llm_tokens_total": 1500000},
}

HYPO_JSON = json.dumps([
    {"id": f"H{i}", "title": f"假设{i}", "problem_statement": f"问题{i}",
     "testable_prediction": f"预测{i}", "minimal_experiment": f"实验{i}",
     "sub_direction": TOPIC["sub_directions"][i % 3]}
    for i in range(1, 6)
], ensure_ascii=False)


class TestExtractJsonArray:
    def test_plain_json(self):
        assert len(extract_json_array(HYPO_JSON)) == 5

    def test_fenced_json(self):
        text = f"前置说明\n```json\n{HYPO_JSON}\n```\n后置说明"
        assert len(extract_json_array(text)) == 5

    def test_noisy_text_with_embedded_array(self):
        text = f"好的，以下是假设：\n{HYPO_JSON}\n希望有帮助。"
        assert len(extract_json_array(text)) == 5

    def test_invalid_json_returns_empty(self):
        assert extract_json_array("完全不是 JSON") == []
        assert extract_json_array("[broken json") == []

    def test_non_array_json_returns_empty(self):
        assert extract_json_array('{"a": 1}') == []


class TestValidateHypotheses:
    def test_five_valid_items_pass(self):
        items = extract_json_array(HYPO_JSON)
        assert validate_hypotheses(items) == []

    def test_too_few_items_flagged(self):
        items = extract_json_array(HYPO_JSON)[:3]
        problems = validate_hypotheses(items)
        assert any("count=3" in p for p in problems)

    def test_missing_field_flagged(self):
        items = [{"id": "H1", "title": "t"}]
        problems = validate_hypotheses(items)
        assert any("缺字段" in p for p in problems)


class TestHypothesisAgentRun:
    def test_run_writes_proposals_and_summary(self, tmp_path, fake_llm):
        llm = fake_llm([HYPO_JSON])
        gaps = tmp_path / "research_gaps.md"
        gaps.write_text("# 研究空白清单\n\n- 空白A\n- 空白B\n", encoding="utf-8")

        agent = HypothesisAgent(TOPIC, llm, proposals_dir=tmp_path / "proposals")
        produced = agent.run(gaps)

        # hypotheses.json 汇总
        summary = json.loads((tmp_path / "proposals" / "hypotheses.json")
                             .read_text(encoding="utf-8"))
        assert summary["count"] == 5
        assert summary["validation"] == []
        assert len(summary["hypotheses"]) == 5

        # P001.md 可被 schema v0 读回，四要素在正文
        parsed = parse_proposal(tmp_path / "proposals" / "P001.md")
        assert parsed["meta"]["status"] == "candidate"
        assert parsed["meta"]["id"] == "P001"
        assert "问题陈述" in parsed["body"]
        assert "可验证预测" in parsed["body"]
        assert "所需最小实验" in parsed["body"]

        # strong 档 agent 归因 + 预算约束进 prompt
        assert llm.last_bind == ("ideation", "hypothesis_lead")
        assert "1500000" in llm.prompts[0]
        assert produced["hypotheses"].endswith("hypotheses.json")

    def test_run_drops_incomplete_items(self, tmp_path, fake_llm):
        broken = json.dumps([
            {"id": "H1", "title": "完整", "problem_statement": "p",
             "testable_prediction": "t", "minimal_experiment": "e",
             "sub_direction": "compression"},
            {"id": "H2", "title": "缺字段"},
        ], ensure_ascii=False)
        llm = fake_llm([broken])
        gaps = tmp_path / "g.md"
        gaps.write_text("gaps", encoding="utf-8")

        agent = HypothesisAgent(TOPIC, llm, proposals_dir=tmp_path / "proposals")
        agent.run(gaps)

        assert not (tmp_path / "proposals" / "P002.md").exists()
        assert (tmp_path / "proposals" / "P001.md").exists()

    def test_run_without_llm_writes_placeholders(self, tmp_path):
        gaps = tmp_path / "g.md"
        gaps.write_text("gaps", encoding="utf-8")
        agent = HypothesisAgent(TOPIC, None, proposals_dir=tmp_path / "proposals")
        produced = agent.run(gaps)
        summary = json.loads((tmp_path / "proposals" / "hypotheses.json")
                             .read_text(encoding="utf-8"))
        assert summary["count"] == 3  # 3 个子方向占位
        parsed = parse_proposal(tmp_path / "proposals" / "P001.md")
        assert "placeholder" in parsed["meta"]["title"]
