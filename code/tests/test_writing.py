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


def test_draft_llm_missing_abstract_fallback(tmp_path, fake_llm):
    """M6：7 节齐全但缺 abstract → 同样整篇回落（不产 "n/a" 摘要稿）。"""
    project = make_project(tmp_path)
    bp_path = AnalysisAgent(TOPIC, None, project,
                            project / "paper").run()["blueprint"]
    payload = json.dumps({"sections": {s: f"Body {s}." for s in SECTIONS}})
    DraftAgent(TOPIC, fake_llm([payload]), project,
               project / "paper").run(bp_path)
    tex = (project / "paper" / "draft.tex").read_text(encoding="utf-8")
    assert "Body introduction." not in tex  # 回落确定性模板
    assert tex.count("\\begin{abstract}") == 1


def test_draft_llm_abstract_latex_preserved(tmp_path, fake_llm):
    """C2：LLM 摘要已 sanitize，_assemble 不得二次 latex_escape（乱码）。"""
    project = make_project(tmp_path)
    bp_path = AnalysisAgent(TOPIC, None, project,
                            project / "paper").run()["blueprint"]
    key = next(iter(CitationChecker(CARDS["cards"]).key_of))
    sections = {s: f"Body {s}." for s in SECTIONS}
    sections["related_work"] = f"Cites~\\cite{{{key}}}."
    abstract = (r"We improve accuracy by 5\% with \emph{evidence-first} "
                r"top-$k$ retrieval.")
    payload = json.dumps({"abstract": abstract, "sections": sections})
    DraftAgent(TOPIC, fake_llm([payload]), project,
               project / "paper").run(bp_path)
    tex = (project / "paper" / "draft.tex").read_text(encoding="utf-8")
    assert r"5\%" in tex and r"\emph{evidence-first}" in tex
    assert "textbackslash" not in tex   # 双重转义残留 = C2 复发
    assert "top-$k$" in tex             # M3：行内数学原样


def test_draft_llm_cite_variants(tmp_path, fake_llm):
    """M2：natbib 可选参数/变体 cite 的 key 必须进 bib 并过引用审计。"""
    project = make_project(tmp_path)
    bp_path = AnalysisAgent(TOPIC, None, project,
                            project / "paper").run()["blueprint"]
    key = next(iter(CitationChecker(CARDS["cards"]).key_of))
    sections = {s: f"Body {s}." for s in SECTIONS}
    sections["related_work"] = (f"Prior work~\\citep[e.g.]{{{key}}} "
                                f"and \\citealp{{{key}}}.")
    payload = json.dumps({"abstract": "An abstract.", "sections": sections})
    DraftAgent(TOPIC, fake_llm([payload]), project,
               project / "paper").run(bp_path)
    tex = (project / "paper" / "draft.tex").read_text(encoding="utf-8")
    bib = (project / "paper" / "references.bib").read_text(encoding="utf-8")
    assert f"@article{{{key}," in bib or f"@misc{{{key}," in bib
    assert audit_citations(tex, bib) == []


def test_draft_rejects_hallucinated_citation_before_write(tmp_path, fake_llm):
    """m3：幻觉引用在 draft.tex 落盘前被 CitationChecker 拦截（宁停）。"""
    project = make_project(tmp_path)
    bp_path = AnalysisAgent(TOPIC, None, project,
                            project / "paper").run()["blueprint"]
    sections = {s: f"Body {s}." for s in SECTIONS}
    sections["related_work"] = r"Ghost work~\cite{ghost2025fake}."
    payload = json.dumps({"abstract": "An abstract.", "sections": sections})
    with pytest.raises(RuntimeError, match="CitationChecker"):
        DraftAgent(TOPIC, fake_llm([payload]), project,
                   project / "paper").run(bp_path)
    assert not (project / "paper" / "draft.tex").exists()


def test_negative_margin_end_to_end(tmp_path):
    """C1+M5 端到端：主实验低于基线（负结果一等公民场景）不再假阳性崩溃；
    门判定 reason（全角括号/≥）归一化后 C3 原文进稿而非制品指针句。"""
    project = make_project(tmp_path)
    results = project / "exp" / "results"
    m1 = json.loads((results / "M1.json").read_text(encoding="utf-8"))
    m1["metrics"]["score"] = 0.55
    m1["metrics"]["per_seed"] = {"0": {"score": 0.55}}
    (results / "M1.json").write_text(json.dumps(m1), encoding="utf-8")
    (results / "gate_verdict.json").write_text(json.dumps(
        {"passed": False,
         "reason": "main(0.5500) - baseline(0.6000) = -0.0500 < threshold "
                   "0.99（direction=gt）",
         "main_value": 0.55, "baseline_value": 0.60,
         "direction": "gt", "threshold": 0.99}), encoding="utf-8")
    bp_path = AnalysisAgent(TOPIC, None, project,
                            project / "paper").run()["blueprint"]
    DraftAgent(TOPIC, None, project, project / "paper").run(bp_path)
    tex = (project / "paper" / "draft.tex").read_text(encoding="utf-8")
    assert "-0.0500" in tex                 # 负 margin 原样入稿
    assert "direction=gt" in tex            # M5：reason 归一化为 ASCII
    # C3 不再退化为指针句（C1 中文假设的指针句是设计行为，不在检查范围）
    assert "claim C3 is recorded verbatim" not in tex
    assert "main(0.5500)" in tex            # 门判定原文进稿
    tex.encode("ascii")
    report = FormatAgent(project, project / "paper").run(compile_pdf=False)
    assert report["audit_problems"] == []   # C1：审计不再假阳性


def test_citation_checker_empty_author_name():
    """m1：空作者名卡片（S2 路径可产出）不崩溃，key 回落 anonymous。"""
    cards = [{"title": "Empty Author Paper", "authors": [""],
              "published": "2026-01-01", "paper_id": "arxiv:2608.33333v1"}]
    assert list(CitationChecker(cards).key_of) == ["anonymous2026empty"]


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
    # M3：行内数学 $...$ 整体原样（含内部 _ 与 \le）
    math = "top-$k$ where $x_1 \\le k$"
    assert sanitize_llm_latextext(math) == math
    # M3：tabular 环境原样（& 是列分隔符不是裸字符）
    tab = "\\begin{tabular}{ll} a & b \\\\ \\end{tabular}"
    assert sanitize_llm_latextext(tab) == tab
    # M3：数学外的裸下划线仍转义，数学内不转义
    assert sanitize_llm_latextext("a_b $x_1$ c_d") == r"a\_b $x_1$ c\_d"
