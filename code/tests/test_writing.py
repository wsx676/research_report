# -*- coding: utf-8 -*-
"""test_writing.py：DraftAgent + CitationChecker + FormatAgent（§5.4 Step2/3）。

覆盖：引用白名单（幻觉引用拒绝）、离线模板可编译结构、数值一致性审计、
BibTeX 校验、摘要规范；编译环节在真实运行验证（单测不拉 LaTeX bundle）。
"""
import json

import pytest

from minifars.analysis import AnalysisAgent
from minifars.draft import CitationChecker, DraftAgent, SECTIONS, latex_escape
from minifars.format_check import (FormatAgent, audit_abstract, audit_citations,
                                   audit_numbers, number_registry)

from test_analysis import CARDS, TOPIC, make_project


# ------------------------------------------------------------------ citations
def test_citation_keys_deterministic_and_checked():
    checker = CitationChecker(CARDS["cards"])
    keys = list(checker.key_of)
    assert len(keys) == 2
    # key = 姓+年+标题首词，确定性稳定（跨 run 一致性审计前提）
    assert any(k.startswith("chen2026") for k in keys)
    assert any(k.startswith("karten2026") for k in keys)
    assert checker.check([keys[0]]) == []
    assert checker.check(["ghost2025fabricated"]) == ["ghost2025fabricated"]
    bib = checker.bib_entries(keys)
    assert "@article{" in bib and "arXiv preprint arXiv:2608.11111" in bib


def test_citation_key_collision_suffix():
    dup = [dict(CARDS["cards"][0]), dict(CARDS["cards"][0])]
    keys = list(CitationChecker(dup).key_of)
    assert len(keys) == 2 and keys[0] != keys[1]  # 重复卡片加后缀


# ------------------------------------------------------------------ draft
def _offline_chain(tmp_path):
    """AnalysisAgent(offline) → DraftAgent(offline) 全链。"""
    project = make_project(tmp_path)
    bp_path = AnalysisAgent(TOPIC, None, project,
                            project / "paper").run()["blueprint"]
    DraftAgent(TOPIC, None, project, project / "paper").run(bp_path)
    return project


def test_draft_offline_structure_and_ascii(tmp_path):
    project = _offline_chain(tmp_path)
    tex = (project / "paper" / "draft.tex").read_text(encoding="utf-8")
    for name in SECTIONS:
        assert f"\\section" in tex
    assert all(f"\\section{{{t}}}" in tex for t in
               ("Introduction", "Related Work", "Method", "Experiments",
                "Analysis", "Limitations", "Conclusion"))
    assert "\\begin{abstract}" in tex
    tex.encode("ascii")  # 中文假设原文不得泄漏进 tex（ICLR 英文稿）
    assert "\\input{figures/fig1.tex}" in tex  # 分析图入文
    assert "\\label{tab:main}" in tex and "0.6600" in tex  # 表数=真实数据
    assert (project / "paper" / "iclr2026_conference.sty").exists()
    # 引用只来自文献卡片且经校验；bib 与 cite 一一对应
    bib = (project / "paper" / "references.bib").read_text(encoding="utf-8")
    assert audit_citations(tex, bib) == []
    assert "@article{" in bib


def test_draft_llm_sections(tmp_path, fake_llm):
    project = make_project(tmp_path)
    bp_path = AnalysisAgent(TOPIC, None, project,
                            project / "paper").run()["blueprint"]
    checker = CitationChecker(CARDS["cards"])
    key = next(iter(checker.key_of))
    sections = {s: f"LLM body for {s}." for s in SECTIONS}
    sections["related_work"] = f"Related work cites~\\cite{{{key}}} only."
    payload = json.dumps({"abstract": "An abstract grounded in evidence.",
                          "sections": sections})
    llm = fake_llm([payload])
    DraftAgent(TOPIC, llm, project, project / "paper").run(bp_path)
    assert llm.last_bind == ("writing", "drafter")
    tex = (project / "paper" / "draft.tex").read_text(encoding="utf-8")
    assert "LLM body for introduction." in tex
    bib = (project / "paper" / "references.bib").read_text(encoding="utf-8")
    assert key in bib and audit_citations(tex, bib) == []


def test_draft_llm_partial_fallback(tmp_path, fake_llm):
    """LLM 缺节输出 → 整篇回落确定性模板（不产出残缺稿）。"""
    project = make_project(tmp_path)
    bp_path = AnalysisAgent(TOPIC, None, project,
                            project / "paper").run()["blueprint"]
    llm = fake_llm([json.dumps({"abstract": "x", "sections": {"method": "only"}})])
    DraftAgent(TOPIC, llm, project, project / "paper").run(bp_path)
    tex = (project / "paper" / "draft.tex").read_text(encoding="utf-8")
    assert "\\section{Conclusion}" in tex and "only" not in tex


# ------------------------------------------------------------------ format
def test_audits_detect_violations():
    registry = {"0.6600", "0.6000"}
    assert audit_numbers("score 0.6600 vs 0.1234", registry) == \
        ["unregistered number 0.1234 (not in sanctioned value set)"]
    assert audit_numbers("all good 0.6600", registry) == []
    assert audit_numbers("no registry", set()) == []  # dry-run 场景跳过
    assert audit_citations("\\cite{a,b}", "@misc{a,\n") == \
        ["citation b missing from references database"]
    assert audit_citations("\\citet{a}", "@misc{a,\n") == []
    assert audit_abstract("\\begin{abstract}ok\\end{abstract}") == []
    long_abs = "\\begin{abstract}" + " ".join(["word"] * 251) + "\\end{abstract}"
    assert "exceeds limit 250" in audit_abstract(long_abs)[0]
    assert audit_abstract("no abstract here") == ["abstract environment missing"]


def test_number_registry_reads_artifacts(tmp_path):
    project = make_project(tmp_path)
    reg = number_registry(project)
    assert {"0.4500", "0.6000", "0.6600", "0.9900"} <= reg  # 含门 threshold


def test_format_agent_audit_pass_offline(tmp_path):
    """离线全链产出必须通过三道审计（compile_pdf=False 避免单测拉 bundle）。"""
    project = _offline_chain(tmp_path)
    fmt = FormatAgent(project, project / "paper")
    report = fmt.run(compile_pdf=False)
    assert report["audit_problems"] == []
    saved = json.loads((project / "paper" / "format_report.json")
                       .read_text(encoding="utf-8"))
    assert saved["audit_problems"] == []


def test_format_agent_rejects_foreign_number(tmp_path):
    project = _offline_chain(tmp_path)
    tex_path = project / "paper" / "draft.tex"
    tex_path.write_text(tex_path.read_text(encoding="utf-8") +
                        "\n% injected 0.7777\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="0.7777"):
        FormatAgent(project, project / "paper").run(compile_pdf=False)


def test_latex_escape():
    assert latex_escape("a_b & 50%") == r"a\_b \& 50\%"


def test_sanitize_llm_latextext():
    from minifars.draft import sanitize_llm_latextext
    # 裸下划线 method 名转义（曾致 XeTeX Missing $）
    assert sanitize_llm_latextext("runs proposed_context_compression") == \
        r"runs proposed\_context\_compression"
    # 合法命令与已转义字符不被破坏
    assert sanitize_llm_latextext(r"\cite{k} a\_b \%") == r"\cite{k} a\_b \%"
    # 命令参数花括号保留
    assert sanitize_llm_latextext(r"\texttt{exp/code}") == r"\texttt{exp/code}"
