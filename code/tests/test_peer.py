# -*- coding: utf-8 -*-
"""PeerAgent 单测（全离线：固定查重输入 + FakeLLM 质询替身）。"""
import json

from minifars.peer import PeerAgent, novelty_hints

TOPIC = {
    "name": "agent_context",
    "sub_directions": ["compression", "memory"],
    "budget": {"max_tokens": 5000000},
}

HYPOS = [
    {"id": "H1", "title": "Recall improvement via context compression",
     "testable_prediction": "recall +15%", "minimal_experiment": "loco-benchmark"},
    {"id": "H2", "title": "Agent memory 上下文管理的新方法",
     "testable_prediction": "占位预测", "minimal_experiment": "占位实验"},
]

CARDS = [
    {"paper_id": "arxiv:2601.00001", "title": "Context Compression for Long-Horizon Agents",
     "abstract": "compression recall improvement study"},
    {"paper_id": "arxiv:2501.00002", "title": "Old Paper on Memory",
     "abstract": "memory management"},
]


def _write_hypos(tmp_path):
    p = tmp_path / "hypotheses.json"
    p.write_text(json.dumps({"schema": "v0", "hypotheses": HYPOS},
                            ensure_ascii=False), encoding="utf-8")
    return p


def _write_cards(tmp_path):
    p = tmp_path / "survey_cards.json"
    p.write_text(json.dumps({"schema": "v0", "cards": CARDS},
                            ensure_ascii=False), encoding="utf-8")
    return p


class TestNoveltyHints:
    def test_finds_most_similar_card(self):
        hints = novelty_hints(HYPOS, CARDS)
        # H1 与卡片1 共享 compression/recall/improvement/context 等词
        assert hints["H1"][0]["title"].startswith("Context Compression")
        assert hints["H1"][0]["overlap"] >= 3

    def test_chinese_bigram_matches(self):
        # H2 中文标题经 bigram 与 "Old Paper on Memory" 的 memory 无重叠，
        # 但卡片2 摘要含 "memory management"，与 H2 的 "memory 上下文管理"
        # 共享英文 token memory —— hints 非空即可证明中英混合分词生效
        hints = novelty_hints(HYPOS, CARDS)
        assert any("memory" in h["title"].lower() or h["overlap"] > 0
                   for h in hints["H2"])

    def test_empty_inputs(self):
        assert novelty_hints([], CARDS) == {}
        assert novelty_hints(HYPOS, []) == {h["id"]: [] for h in HYPOS}


class TestPeerAgentRun:
    def test_review_writes_json_and_attribution(self, tmp_path, fake_llm):
        llm = fake_llm([json.dumps([
            {"id": "H1", "novelty_concern": "与卡片1重叠", "feasibility_score": 0.8,
             "major_weakness": "预测模糊", "suggested_fix": "给出数值下界"},
            {"id": "H2", "novelty_concern": "无", "feasibility_score": 0.7,
             "major_weakness": "无", "suggested_fix": "无"},
        ], ensure_ascii=False)])
        agent = PeerAgent(TOPIC, llm, proposals_dir=tmp_path)
        res = agent.review(_write_hypos(tmp_path), _write_cards(tmp_path), round_no=1)

        payload = json.loads((tmp_path / "peer_review_r1.json").read_text(encoding="utf-8"))
        assert payload["schema"] == "v0"
        assert payload["round"] == 1
        assert len(payload["reviews"]) == 2
        assert payload["reviews"][0]["feasibility_score"] == 0.8
        assert "Context Compression" in payload["novelty_hints"]["H1"][0]["title"]
        assert res["items"][0]["id"] == "H1"
        # strong 档 agent 归因
        assert llm.last_bind == ("ideation", "peer_reviewer")
        # 查重线索进 prompt
        assert "Context Compression" in llm.prompts[0]

    def test_review_placeholder_without_llm(self, tmp_path):
        agent = PeerAgent(TOPIC, None, proposals_dir=tmp_path)
        res = agent.review(_write_hypos(tmp_path), _write_cards(tmp_path))
        payload = json.loads((tmp_path / "peer_review_r1.json").read_text(encoding="utf-8"))
        assert payload["reviews"][0]["feasibility_score"] == 0.5
        assert "离线占位" in payload["reviews"][0]["novelty_concern"]
        assert len(res["items"]) == 2

    def test_review_survives_missing_cards_file(self, tmp_path, fake_llm):
        llm = fake_llm(["[]"])  # LLM 空输出：全部走占位补齐
        agent = PeerAgent(TOPIC, llm, proposals_dir=tmp_path)
        res = agent.review(_write_hypos(tmp_path), tmp_path / "nope.json")
        assert len(res["items"]) == 2
        assert res["items"][0]["novelty_concern"] == "（质询缺失：占位）"

    def test_review_clamps_bad_feasibility_score(self, tmp_path, fake_llm):
        llm = fake_llm([json.dumps([
            {"id": "H1", "feasibility_score": 7.5},
            {"id": "H2", "feasibility_score": "bad"},
        ])])
        agent = PeerAgent(TOPIC, llm, proposals_dir=tmp_path)
        res = agent.review(_write_hypos(tmp_path), _write_cards(tmp_path))
        assert res["items"][0]["feasibility_score"] == 1.0   # 钳位
        assert res["items"][1]["feasibility_score"] == 0.5   # 非法值回落
