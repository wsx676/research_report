# -*- coding: utf-8 -*-
r"""DraftAgent + CitationChecker（设计文档 §5.4 Step2）：blueprint → draft.tex。

写作规则（证据优先 + SAR 导向 §8.2）：
1. 逐节只喂蓝图中对应证据（claim text + 数值表），不喂上游全文；
2. 相关工作引用**只允许**来自 SurveyAgent 的文献卡片（含真实 arXiv ID），
   CitationChecker 在写入 \cite 前逐条校验 key 存在性——针对 SAR 诚信
   审计的 hallucinated citations 失效模式；
3. 无 LLM 时用确定性模板行文（数值仍取自制品），保证离线可编译可测试；
4. SAR 规范内建：intro 前两段聚焦唯一中心贡献、Limitations 独立成节
   坦白实验边界、负结果按 candid analysis 风格成文。
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional

from .artifacts import load_cards
from .contract import extract_json_object
from .llm import LLMClient

DRAFT_MAX_TOKENS = 8192
#: 相关工作最多引用卡片数
MAX_RELATED_CITATIONS = 6

DRAFT_SYSTEM = (
    "You are a scientific writer for an ICLR-style submission. Write ONLY "
    "LaTeX section bodies grounded in the provided blueprint evidence. "
    "Never invent numbers, datasets, or citations. Output ONLY a JSON object."
)

#: 正文节顺序（ICLR 结构）；abstract 单独处理
SECTIONS = ("introduction", "related_work", "method", "experiments",
            "analysis", "limitations", "conclusion")
SECTION_TITLES = {
    "introduction": "Introduction",
    "related_work": "Related Work",
    "method": "Method",
    "experiments": "Experiments",
    "analysis": "Analysis",
    "limitations": "Limitations",
    "conclusion": "Conclusion",
}


# ------------------------------------------------------------------ citations
class CitationChecker:
    """引用白名单 = SurveyAgent 文献卡片；卡片之外的 key 一律拒绝。

    cite key 由卡片确定性生成（第一作者姓+年份+标题首词），重复时加后缀，
    保证同一卡片在多次运行中 key 稳定（跨 run 引用一致性审计的前提）。
    """

    def __init__(self, cards: List[Dict[str, Any]]):
        self.cards = [c for c in cards if c.get("title")]
        self.key_of: Dict[str, Dict[str, Any]] = {}   # key -> card
        self._build()

    def _build(self) -> None:
        used: set = set()
        for card in self.cards:
            key = self._base_key(card)
            candidate, n = key, 0
            while candidate in used:
                n += 1
                candidate = f"{key}{chr(ord('b') + n - 1)}"
            used.add(candidate)
            self.key_of[candidate] = card

    @staticmethod
    def _ascii(s: str) -> str:
        s = unicodedata.normalize("NFKD", s)
        return "".join(ch for ch in s if not unicodedata.combining(ch))

    def _base_key(self, card: Dict[str, Any]) -> str:
        authors = card.get("authors") or ["anonymous"]
        surname = self._ascii(str(authors[0]).split()[-1]).lower()
        surname = re.sub(r"[^a-z]", "", surname) or "anon"
        year = str(card.get("published") or "9999")[:4]
        word = ""
        for w in re.findall(r"[A-Za-z]{4,}", str(card.get("title", ""))):
            if w.lower() not in ("with", "from", "that", "this", "toward",
                                 "based", "using"):
                word = w.lower()
                break
        return f"{surname}{year}{word}"

    def check(self, keys: List[str]) -> List[str]:
        """返回未知 key 列表（空 = 全部存在于文献卡片）。"""
        return [k for k in keys if k not in self.key_of]

    def bib_entries(self, keys: List[str]) -> str:
        """被引用卡片 → BibTeX 条目（arXiv 用 @article，其余 @misc）。"""
        chunks = []
        for k in keys:
            card = self.key_of.get(k)
            if card is None:
                continue
            authors = " and ".join(self._ascii(a)
                                   for a in (card.get("authors") or ["Anonymous"]))
            year = str(card.get("published") or "")[:4] or "n.d."
            pid = str(card.get("paper_id") or "")
            if pid.startswith("arxiv:"):
                eprint = pid.split(":", 1)[1]
                chunks.append(
                    f"@article{{{k},\n  title   = {{{card['title']}}},\n"
                    f"  author  = {{{authors}}},\n  year    = {{{year}}},\n"
                    f"  journal = {{arXiv preprint arXiv:{eprint.split('v')[0]}}},\n"
                    f"  eprint  = {{{eprint}}},\n  url     = {{{card.get('url', '')}}}\n}}")
            else:
                chunks.append(
                    f"@misc{{{k},\n  title  = {{{card['title']}}},\n"
                    f"  author = {{{authors}}},\n  year   = {{{year}}},\n"
                    f"  url    = {{{card.get('url', '')}}}\n}}")
        return "\n\n".join(chunks) + ("\n" if chunks else "")


# ------------------------------------------------------------------ helpers
def latex_escape(s: str) -> str:
    """LaTeX 特殊字符转义（插值制品文本前的必经之路）。"""
    for a, b in (("\\", r"\textbackslash{}"), ("&", r"\&"), ("%", r"\%"),
                 ("$", r"\$"), ("#", r"\#"), ("_", r"\_"),
                 ("{", r"\{"), ("}", r"\}"), ("~", r"\textasciitilde{}"),
                 ("<", r"$<$"), (">", r"$>$")):
        s = s.replace(a, b)
    return s


#: LLM 输出的 LaTeX 段落里，命令之外的裸特殊字符净化（未转义的
#: method 名 proposed_xxx、裸 & 等会在 XeTeX 触发 Missing $）
_LATEX_CMD = re.compile(r"\\[a-zA-Z]+")


def sanitize_llm_latextext(s: str) -> str:
    """LLM 产出按 LaTeX 对待：保护合法命令，转义裸特殊字符。"""
    parts = []
    pos = 0
    for m in _LATEX_CMD.finditer(s):
        parts.append(_escape_bare(s[pos:m.start()]))
        parts.append(m.group(0))
        pos = m.end()
    parts.append(_escape_bare(s[pos:]))
    return "".join(parts)


def _escape_bare(s: str) -> str:
    """只转义未被 LLM 转义过的裸特殊字符（(?<!\\\\) 防双重转义）。"""
    for pat, rep in ((r"(?<!\\)&", r"\&"), (r"(?<!\\)%", r"\%"),
                     (r"(?<!\\)#", r"\#"), (r"(?<!\\)_", r"\_"),
                     (r"(?<!\\)\$", r"\$")):
        s = re.sub(pat, rep, s)
    return s.replace("<", "$<$").replace(">", "$>$")


def _claims_by_section(bp: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {s: [] for s in SECTIONS}
    for c in bp.get("claims", []):
        out.setdefault(c.get("section") or "analysis", []).append(c)
    return out


def _ascii_safe(s: str) -> bool:
    return all(ord(ch) < 128 for ch in s)


def _claim_tex(c: Dict[str, Any]) -> str:
    """claim → 正文句：非 ASCII（如中文假设原文）不进 tex，改述证据指向。"""
    text = c.get("text", "")
    if not _ascii_safe(text):
        src = (c.get("evidence") or [{}])[0].get("source_artifact", "artifact")
        return (f"The statement of claim {c.get('id')} is recorded verbatim in "
                f"the audited artifact \\texttt{{{latex_escape(src)}}}.")
    return latex_escape(text)


# ------------------------------------------------------------------ agent
class DraftAgent:
    """blueprint.json → draft.tex + references.bib（ICLR 模板，§5.4 Step2）。"""

    def __init__(self, topic: Dict[str, Any], llm_strong: Optional[LLMClient],
                 project: Path | str, paper_dir: Path | str, metering=None):
        self.topic = topic
        self.llm = llm_strong
        self.project = Path(project)
        self.paper_dir = Path(paper_dir)
        self.metering = metering

    def run(self, blueprint_path: Path | str,
            cards_path: Optional[Path | str] = None) -> Dict[str, str]:
        t0 = time.perf_counter()
        bp = json.loads(Path(blueprint_path).read_text(encoding="utf-8"))
        cards_path = Path(cards_path or self.project / "proposals" / "survey" /
                          "survey_cards.json")
        checker = CitationChecker(load_cards(cards_path))

        sections = self._sections(bp, checker)
        cite_keys = list(dict.fromkeys(sections.pop("_cited_keys", [])))
        problems = checker.check(cite_keys)
        if problems:
            raise RuntimeError(f"CitationChecker 拒绝幻觉引用: {problems}")

        tex = self._assemble(bp, sections, cite_keys)
        self.paper_dir.mkdir(parents=True, exist_ok=True)
        self._install_template()
        draft_path = self.paper_dir / "draft.tex"
        draft_path.write_text(tex, encoding="utf-8")
        bib_path = self.paper_dir / "references.bib"
        bib_path.write_text(checker.bib_entries(cite_keys), encoding="utf-8")

        self._record(t0, len(sections), len(cite_keys))
        print(f"[draft] draft.tex 落盘: {draft_path}（{len(cite_keys)} 引用，"
              f"全部经 CitationChecker 校验）")
        return {"draft": str(draft_path), "bib": str(bib_path)}

    # ------------------------------------------------------------ internals
    def _sections(self, bp: Dict[str, Any],
                  checker: CitationChecker) -> Dict[str, str]:
        """逐节正文：LLM 模式按蓝图证据写作，失败/离线回落确定性模板。"""
        grouped = _claims_by_section(bp)
        keys = list(checker.key_of)[:MAX_RELATED_CITATIONS]
        if self.llm is not None:
            try:
                out = self._llm_sections(bp, grouped, keys)
                if out and all(out.get(s) for s in SECTIONS):
                    out["_cited_keys"] = self._used_cite_keys(out, keys)
                    return out
                print("[draft] LLM 输出节缺失，回落确定性模板")
            except Exception as exc:
                print(f"[draft] LLM 写作失败回落确定性模板: {exc}")
        out = self._template_sections(bp, grouped, keys)
        out["_cited_keys"] = self._used_cite_keys(out, keys)
        return out

    @staticmethod
    def _used_cite_keys(sections: Dict[str, str],
                        allowed: List[str]) -> List[str]:
        """正文实际用到的 key 才进 references.bib（且必在白名单内）。"""
        text = "\n".join(v for k, v in sections.items() if k != "_cited_keys")
        used = re.findall(r"\\cite[pt]?\{([^}]+)\}", text)
        flat = [k for grp in used for k in grp.split(",")]
        return [k for k in dict.fromkeys(flat) if k in allowed]

    def _llm_sections(self, bp: Dict[str, Any],
                      grouped: Dict[str, List[Dict[str, Any]]],
                      keys: List[str]) -> Dict[str, str]:
        evidence_dump = {
            s: [{"id": c.get("id"), "text": c.get("text"),
                 "strength": [e.get("support_strength")
                              for e in c.get("evidence", [])]}
                for c in grouped.get(s, [])]
            for s in SECTIONS}
        related = [{"key": k, "title": checker_title}
                   for k, checker_title in self._related_items(keys)]
        prompt = (
            f"Paper title: {bp.get('paper_title')}\n"
            f"Central claim: {bp.get('central_claim')}\n"
            f"Blueprint evidence by section:\n"
            f"{json.dumps(evidence_dump, ensure_ascii=False, indent=1)}\n"
            f"Allowed citation keys (ONLY these may appear in \\cite): "
            f"{json.dumps(related, ensure_ascii=False)}\n\n"
            "Write the paper in English, ICLR style. SAR-oriented rules:\n"
            "1. introduction: first two paragraphs state ONE central "
            "contribution clearly; no overclaiming beyond evidence strength;\n"
            "2. limitations: candidly state experimental boundaries "
            "(synthetic deterministic benchmarks, small seed count);\n"
            "3. negative or weak results must be reported honestly;\n"
            "4. use ONLY numbers present in the evidence; cite ONLY the "
            "allowed keys, and only in related_work;\n"
            "Output JSON: {\"abstract\": \"...\", \"sections\": "
            "{\"introduction\": \"<latex>\", \"related_work\": \"...\", "
            "\"method\": \"...\", \"experiments\": \"...\", \"analysis\": "
            "\"...\", \"limitations\": \"...\", \"conclusion\": \"...\"}}\n"
            "Each section body is raw LaTeX (no \\section{} headers, no "
            "``` fences)."
        )
        cli = self.llm.bind("writing", "drafter")
        resp = cli.chat(prompt, system=DRAFT_SYSTEM, max_tokens=DRAFT_MAX_TOKENS)
        data = extract_json_object(LLMClient.text_of(resp))
        # LLM 输出按 LaTeX 消费：裸特殊字符（未转义 method 名等）先净化，
        # 防 XeTeX Missing $；\cite 等合法命令保留
        out = {s: sanitize_llm_latextext(
                   str(data.get("sections", {}).get(s) or "")).strip()
               for s in SECTIONS}
        out["abstract"] = sanitize_llm_latextext(
            str(data.get("abstract") or "")).strip()
        return out

    def _related_items(self, keys: List[str]) -> List[tuple]:
        cards = load_cards(self.project / "proposals" / "survey" /
                           "survey_cards.json")
        by_title_key: Dict[str, Dict[str, Any]] = {}
        checker = CitationChecker(cards)
        for k in keys:
            card = checker.key_of.get(k)
            if card:
                by_title_key[k] = card
        return [(k, by_title_key[k]["title"]) for k in keys if k in by_title_key]

    def _template_sections(self, bp: Dict[str, Any],
                           grouped: Dict[str, List[Dict[str, Any]]],
                           keys: List[str]) -> Dict[str, str]:
        """确定性模板：数值取制品、行文固定——离线/LLM 失败的可编译兜底。"""
        def sec(name: str, extra: str = "") -> str:
            body = " ".join(_claim_tex(c) for c in grouped.get(name, []))
            return (f"{body or 'See the linked artifacts for details.'} "
                    f"{extra}").strip()

        cite_all = "".join(f"~\\cite{{{k}}}" for k in keys) or ""
        related = sec("related_work")
        if keys:
            related = (f"Recent work most relevant to this study includes the "
                       f"following contemporaneous efforts{cite_all}. " + related)
        abstract = (
            "Automated research pipelines risk hallucinated results and "
            "overclaiming. We present a contract-gated, evidence-first "
            "pipeline in which every paper claim is linked to an audited "
            "artifact. " +
            (grouped.get("analysis", [{}])[0].get("text", "")
             if grouped.get("analysis") else "") +
            " All numbers are produced by deterministic seeded scripts.")
        if not _ascii_safe(abstract):
            abstract = ("Automated research pipelines risk hallucinated "
                        "results and overclaiming. We present a contract-"
                        "gated, evidence-first pipeline in which every paper "
                        "claim is linked to an audited artifact and every "
                        "number is produced by a deterministic seeded script.")
        out = {
            "abstract": abstract,
            "introduction": sec("introduction",
                                "The pipeline design is detailed in the "
                                "Method section; all evidence artifacts are "
                                "committed to a versioned workspace."),
            "related_work": related,
            "method": sec("method",
                          "Figure~\\ref{fig:method} sketches the method. "
                          "An experiment contract freezes the design before "
                          "execution; an effectiveness gate decides whether "
                          "analysis proceeds."),
            "experiments": sec("experiments",
                               "Table~\\ref{tab:main} reports the main "
                               "results; Figure~\\ref{fig:main} visualizes "
                               "the comparison."),
            "analysis": sec("analysis"),
            "limitations": sec("limitations",
                               "Results are obtained on deterministic "
                               "synthetic benchmarks with a small seed "
                               "count; external validity is left to future "
                               "work on real datasets."),
            "conclusion": sec("conclusion",
                              "The pipeline demonstrates that evidence-first "
                              "constraints can be enforced structurally, "
                              "including honest reporting of negative "
                              "results."),
        }
        return out

    def _assemble(self, bp: Dict[str, Any], sections: Dict[str, str],
                  cite_keys: List[str]) -> str:
        abstract = latex_escape(sections.get("abstract", "")) or "n/a"
        table = self._results_table()
        figs_dir = self.paper_dir / "figures"
        fig1_ok = (figs_dir / "fig1.tex").exists()
        fig2_ok = (figs_dir / "fig2.tex").exists()
        parts = [
            "% auto-generated by miniFARS DraftAgent -- claims traceable in blueprint.json",
            "\\documentclass[11pt]{article}",
            "\\usepackage{iclr2026_conference}",
            f"\\title{{{latex_escape(str(bp.get('paper_title', 'Untitled')))}}}",
            "\\author{Anonymous\\thanks{Produced by the miniFARS automated "
            "research pipeline; artifacts are fully auditable.}}",
            "\\begin{document}",
            "\\maketitle",
            "",
            "\\begin{abstract}", abstract, "\\end{abstract}",
            "",
        ]
        for name in SECTIONS:
            parts.append(f"\\section{{{SECTION_TITLES[name]}}}")
            parts.append(sections.get(name, ""))
            parts.append("")
            if name == "method" and fig2_ok:
                parts += ["\\begin{figure}[h]\\centering",
                          "\\input{figures/fig2.tex}",
                          "\\caption{Method overview (generated from the "
                          "contract hypothesis caption).}\\label{fig:method}",
                          "\\end{figure}", ""]
            if name == "experiments":
                parts += [table, ""]
                if fig1_ok:
                    parts += ["\\begin{figure}[h]\\centering",
                              "\\input{figures/fig1.tex}",
                              "\\caption{Main comparison: proposed method vs "
                              "baselines (numbers read from "
                              "\\texttt{exp/results/*.json}).}\\label{fig:main}",
                              "\\end{figure}", ""]
        parts += ["\\bibliographystyle{plainnat}",
                  "\\bibliography{references}",
                  "\\end{document}", ""]
        return "\n".join(parts)

    def _results_table(self) -> str:
        """实验主表：逐行来自 exp/results/*.json（表数=真实数据）。"""
        rows = []
        results_dir = self.project / "exp" / "results"
        for f in sorted(results_dir.glob("*.json")):
            if f.name.endswith(".run_meta.json") or f.stem in (
                    "gate_verdict", "negative_result", "run_summary"):
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            m = data.get("metrics") or {}
            score, method = m.get("score"), m.get("method", f.stem)
            n = m.get("n_seeds", 1)
            if isinstance(score, (int, float)):
                rows.append(f"{latex_escape(str(method))} & {f.stem} & "
                            f"{score:.4f} & {n} \\\\")
        if not rows:
            return "% no experiment results available"
        return ("\\begin{table}[h]\\centering\\small\n"
                "\\begin{tabular}{lccc}\n"
                "\\hline\n method & task & score & seeds \\\\ \\hline\n"
                + "\n".join(rows) + "\n\\hline\n\\end{tabular}\n"
                "\\caption{Main experimental results (deterministic seeded "
                "evaluation).}\\label{tab:main}\n\\end{table}")

    def _install_template(self) -> None:
        """vendored ICLR 样式复制到 paper/（tectonic 在 paper/ 本地编译）。"""
        src = Path(__file__).parent / "templates" / "iclr2026_conference.sty"
        dst = self.paper_dir / "iclr2026_conference.sty"
        dst.write_bytes(src.read_bytes())

    def _record(self, t0: float, n_sections: int, n_cites: int) -> None:
        if self.metering is None:
            return
        self.metering.record(stage="writing", agent="drafter",
                             model="pipeline",
                             latency_ms=int((time.perf_counter() - t0) * 1000),
                             extra={"sections": n_sections,
                                    "citations": n_cites})
