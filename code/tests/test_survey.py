# -*- coding: utf-8 -*-
"""SurveyAgent 单测（全离线：arXiv XML 样例 + mock 检索 + FakeLLM）。"""
import json
from datetime import datetime, timezone

import minifars.survey as survey
from minifars.survey import (Paper, SurveyAgent, filter_recent,
                             merge_papers, parse_arxiv_atom)

ATOM_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2601.00001v1</id>
    <title>Context Compression for Long-Horizon Agents</title>
    <summary> We study compression.  Multi-line  abstract. </summary>
    <published>2026-01-15T00:00:00Z</published>
    <author><name>Alice</name></author>
    <author><name>Bob</name></author>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2501.00002v2</id>
    <title>Old Paper on Memory</title>
    <summary>Old work.</summary>
    <published>2025-01-10T00:00:00Z</published>
    <author><name>Carol</name></author>
  </entry>
</feed>
"""


class TestParsers:
    def test_parse_arxiv_atom_extracts_fields(self):
        papers = parse_arxiv_atom(ATOM_SAMPLE, sub_direction="ctx mgmt")
        assert len(papers) == 2
        p = papers[0]
        assert p.paper_id == "arxiv:2601.00001v1"
        assert p.title == "Context Compression for Long-Horizon Agents"
        assert p.abstract == "We study compression. Multi-line abstract."
        assert p.authors == ["Alice", "Bob"]
        assert p.published == "2026-01-15"
        assert p.sub_direction == "ctx mgmt"

    def test_filter_recent_drops_old_papers(self):
        papers = parse_arxiv_atom(ATOM_SAMPLE)
        now = datetime(2026, 8, 25, tzinfo=timezone.utc)
        kept = filter_recent(papers, months_back=12, now=now)
        assert [p.paper_id for p in kept] == ["arxiv:2601.00001v1"]

    def test_filter_recent_zero_keeps_all(self):
        papers = parse_arxiv_atom(ATOM_SAMPLE)
        assert len(filter_recent(papers, months_back=0)) == 2

    def test_merge_dedupes_by_arxiv_id_and_merges_citations(self):
        arxiv_side = [Paper(paper_id="arxiv:2601.00001", title="T", abstract="",
                            source="arxiv")]
        s2_side = [Paper(paper_id="arxiv:2601.00001", title="T", abstract="",
                         source="semantic_scholar", citation_count=7)]
        merged = merge_papers([arxiv_side, s2_side])
        assert len(merged) == 1
        assert merged[0].citation_count == 7
        assert merged[0].source == "arxiv"  # 先到者优先


class TestSurveyAgentRun:
    TOPIC = {
        "name": "agent_context",
        "sub_directions": ["compression"],
        "search_filters": {"months_back": 0, "min_citations": 0},
    }

    def test_run_writes_cards_and_gaps(self, tmp_path, monkeypatch, fake_llm):
        monkeypatch.setattr(survey, "search_arxiv",
                            lambda q, **kw: parse_arxiv_atom(ATOM_SAMPLE, q))
        monkeypatch.setattr(survey, "search_semantic_scholar",
                            lambda q, **kw: [])
        llm = fake_llm(["## compression\n- 空白点1\n- 空白点2\n## 跨方向\n- 观察"])
        agent = SurveyAgent(self.TOPIC, llm, out_dir=tmp_path / "survey")
        produced = agent.run()

        cards = json.loads((tmp_path / "survey" / "survey_cards.json")
                           .read_text(encoding="utf-8"))
        assert cards["schema"] == "v0"
        assert len(cards["cards"]) == 2
        assert cards["cards"][0]["paper_id"].startswith("arxiv:")

        gaps = (tmp_path / "survey" / "research_gaps.md").read_text(encoding="utf-8")
        assert "研究空白清单" in gaps
        assert "空白点1" in gaps
        assert "survey_cards.json" in gaps  # 溯源标注
        # light 档 agent 归因
        assert llm.last_bind == ("ideation", "survey_agent")
        assert produced["research_gaps"].endswith("research_gaps.md")

    def test_run_without_llm_writes_placeholder_gaps(self, tmp_path, monkeypatch):
        monkeypatch.setattr(survey, "search_arxiv",
                            lambda q, **kw: parse_arxiv_atom(ATOM_SAMPLE, q))
        monkeypatch.setattr(survey, "search_semantic_scholar",
                            lambda q, **kw: [])
        agent = SurveyAgent(self.TOPIC, None, out_dir=tmp_path / "survey")
        produced = agent.run()
        gaps = (tmp_path / "survey" / "research_gaps.md").read_text(encoding="utf-8")
        assert "离线占位" in gaps
        assert "2601.00001" in gaps

    def test_run_survives_arxiv_failure(self, tmp_path, monkeypatch):
        def boom(q, **kw):
            raise survey.httpx.ConnectError("network down")

        monkeypatch.setattr(survey, "search_arxiv", boom)
        monkeypatch.setattr(survey, "search_semantic_scholar",
                            lambda q, **kw: [])
        agent = SurveyAgent(self.TOPIC, None, out_dir=tmp_path / "survey")
        produced = agent.run()  # 不抛异常，空卡片占位
        cards = json.loads((tmp_path / "survey" / "survey_cards.json")
                           .read_text(encoding="utf-8"))
        assert cards["cards"] == []
