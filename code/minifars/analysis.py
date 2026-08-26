# -*- coding: utf-8 -*-
"""AnalysisAgent（设计文档 §5.4 Step1）：制品证据审计 → blueprint.json。

证据优先写作的守门环节，三条硬规则：
1. 每条 claim 的 evidence 链**只从制品推导**（proposal/contract/results/
   gate_verdict/negative_result）——LLM 只允许润色 claim 文本，永远不提供
   证据，结构性杜绝结果幻觉与过度声称；
2. 无证据支撑的 claim 拒绝入蓝图（validate_blueprint + 文件存在性双校验）；
3. 分析图由绘图脚本在 results 上执行生成（图数=真实数据，pgfplots 源码
   落盘可审计），方法示意图按图注描述生成。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .artifacts import SCHEMA_VERSION, validate_blueprint
from .contract import extract_json_object
from .llm import LLMClient

#: LLM 润色 claim 文本的最大 token
ANALYSIS_MAX_TOKENS = 4096
#: 支撑强度分级
SUPPORT_LEVELS = ("strong", "moderate", "weak")

ANALYSIS_SYSTEM = (
    "You are an evidence auditor for an automated science pipeline. "
    "Rewrite claim statements into concise academic English. You may ONLY "
    "rephrase; you must NOT add numbers, effects, or conclusions that are "
    "not present in the provided evidence facts. Output ONLY a JSON object."
)


# ------------------------------------------------------------------ evidence
def collect_evidence(project: Path | str,
                     contract_path: Optional[Path | str] = None) -> Dict[str, Any]:
    """汇总 proposal/contract/results 三类制品的可引用事实。

    返回 dict：contract/hypothesis/predict、results（task_id→指标）、
    gate（有效性门判定）、negative（负结果记录）、rel（相对路径表）。
    """
    project = Path(project)
    results_dir = project / "exp" / "results"
    contract_path = Path(contract_path or
                         project / "plan" / "experiment_contract.yaml")

    contract: Dict[str, Any] = {}
    if contract_path.exists():
        try:
            contract = yaml.safe_load(contract_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            contract = {}

    results: Dict[str, Dict[str, Any]] = {}
    for f in sorted(results_dir.glob("*.json")):
        if f.name.endswith(".run_meta.json") or f.stem in (
                "gate_verdict", "negative_result", "run_summary"):
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            metrics = data.get("metrics") if isinstance(data, dict) else None
            if isinstance(metrics, dict):
                results[f.stem] = metrics
        except (json.JSONDecodeError, OSError):
            continue

    def _load(name: str) -> Dict[str, Any]:
        p = results_dir / name
        if not p.exists():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}

    gate = _load("gate_verdict.json")
    negative = _load("negative_result.json")
    run_summary = _load("run_summary.json")

    # 相对 project 的路径表（blueprint 证据链接统一用相对路径，可点开核对）
    rel: Dict[str, str] = {
        "contract": _rel(project, contract_path),
        "accepted": "proposals/accepted_proposal.md",
        "gate": "exp/results/gate_verdict.json",
        "negative": "exp/results/negative_result.json",
        "run_summary": "exp/results/run_summary.json",
    }
    rel.update({f"{tid}": f"exp/results/{tid}.json" for tid in results})
    rel.update({f"{tid}.run_meta": f"exp/results/{tid}.run_meta.json"
                for tid in results})

    return {"contract": contract, "results": results, "gate": gate,
            "negative": negative, "run_summary": run_summary, "rel": rel}


def _rel(project: Path, path: Path) -> str:
    try:
        return Path(path).resolve().relative_to(project.resolve()).as_posix()
    except ValueError:
        return Path(path).as_posix()


def _score_of(metrics: Dict[str, Any]) -> Optional[float]:
    v = metrics.get("score")
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def support_strength(gate: Dict[str, Any], margin: Optional[float]) -> str:
    """支撑强度：门通过→strong；margin 为正但未过门→moderate；否则 weak。"""
    if gate.get("passed"):
        return "strong"
    if margin is not None and margin > 0:
        return "moderate"
    return "weak"


# ------------------------------------------------------------------ claims
def build_claims(ev: Dict[str, Any]) -> List[Dict[str, Any]]:
    """确定性 claim 构建：每条 claim 的证据链全部来自制品事实。

    LLM 可以润色 text，但 evidence 永不来自 LLM（§5.4 硬规则）。
    """
    contract, results = ev["contract"], ev["results"]
    gate, negative, rel = ev["gate"], ev["negative"], ev["rel"]
    claims: List[Dict[str, Any]] = []

    # C1 中心假设（introduction）：契约 hypothesis + 过门立项报告
    if contract.get("hypothesis"):
        claims.append({
            "id": "C1", "section": "introduction",
            "text": (f"We investigate the following hypothesis: "
                     f"{contract['hypothesis']} The quantifiable prediction "
                     f"under test is: {contract.get('predict', 'n/a')}."),
            "evidence": [
                {"source_artifact": rel["contract"], "figure_candidate": None,
                 "support_strength": "moderate"},
                {"source_artifact": rel["accepted"], "figure_candidate": None,
                 "support_strength": "moderate"},
            ]})

    # C2 主实验 vs 基线（experiments）：数值全部取自 results/gate
    main_ids = [t for t in ((contract.get("tasks") or {}).get("main")) or []]
    base_ids = [t for t in ((contract.get("tasks") or {}).get("baselines")) or []]
    main_id = main_ids[0]["id"] if main_ids else "M1"
    main_score = _score_of(results.get(main_id, {}))
    if main_score is not None:
        base_scores = {t["id"]: _score_of(results.get(t["id"], {}))
                       for t in base_ids}
        base_scores = {k: v for k, v in base_scores.items() if v is not None}
        best = max(base_scores, key=base_scores.get) if base_scores else None
        margin = (main_score - base_scores[best]) if best else None
        strength = support_strength(gate, margin)
        margin_txt = (f"an absolute margin of {margin:+.4f} over the strongest "
                      f"baseline {best} ({base_scores[best]:.4f})"
                      if margin is not None else "no baseline comparison available")
        claims.append({
            "id": "C2", "section": "experiments",
            "text": (f"The proposed method ({results[main_id].get('method', main_id)}) "
                     f"achieves a mean score of {main_score:.4f} over "
                     f"{results[main_id].get('n_seeds', 1)} seeds, with "
                     f"{margin_txt}."),
            "evidence": [
                {"source_artifact": rel[main_id],
                 "figure_candidate": "paper/figures/fig1.tex",
                 "support_strength": strength},
                {"source_artifact": rel["gate"], "figure_candidate": None,
                 "support_strength": strength},
            ]})

    # C3 有效性门判定（analysis）：门裁决是测量事实，强度恒 strong
    if gate:
        verdict = "passes" if gate.get("passed") else "does not meet"
        claims.append({
            "id": "C3", "section": "analysis",
            "text": (f"The pre-registered effectiveness gate {verdict} its "
                     f"threshold: {_ascii_reason(gate.get('reason'))}."),
            "evidence": [
                {"source_artifact": rel["gate"], "figure_candidate": None,
                 "support_strength": "strong"},
            ]})

    # C4 负结果诚实声明（limitations）：仅当 negative_result 存在
    if negative:
        claims.append({
            "id": "C4", "section": "limitations",
            "text": (f"The experimental evidence does not support the predicted "
                     f"effect; we report this as a negative result rather than "
                     f"overstating a positive finding. "
                     f"{negative.get('note', '')}"),
            "evidence": [
                {"source_artifact": rel["negative"], "figure_candidate": None,
                 "support_strength": "strong"},
            ]})

    # C5 可复现性（method）：run_meta 五要素 + 确定性打分
    if results:
        any_tid = next(iter(sorted(results)))
        claims.append({
            "id": "C5", "section": "method",
            "text": (f"Every reported number is produced by a deterministic, "
                     f"seeded evaluation script; each task records the full "
                     f"reproduction quintuple (command, seed, model, tokens, "
                     f"timestamps) in its run_meta artifact."),
            "evidence": [
                {"source_artifact": rel[f"{any_tid}.run_meta"],
                 "figure_candidate": None, "support_strength": "strong"},
            ]})
    return claims


def check_evidence_files(bp: Dict[str, Any], project: Path | str) -> List[str]:
    """blueprint 证据链接的文件存在性校验（验收：100% 可点开核对）。"""
    project = Path(project)
    problems = []
    for c in bp.get("claims", []):
        for e in c.get("evidence", []):
            src = e.get("source_artifact") or ""
            if src and not (project / src).exists():
                problems.append(f"claim {c.get('id')}: 证据不存在 {src}")
    return problems


# ------------------------------------------------------------------ figures
FIGURE_SCRIPT = r'''# -*- coding: utf-8 -*-
"""分析图生成脚本（AnalysisAgent 落盘并执行）：图数=真实数据。

fig1.tex —— 主实验 vs 基线柱状图（pgfplots，数值直接读 results/*.json）；
fig2.tex —— 方法示意图（按契约 hypothesis 图注生成的 TikZ 流程框图）。
"""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent.parent
RESULTS = PROJECT / "exp" / "results"

PREAMBLE = ("% auto-generated by make_figures.py -- do not edit by hand;\n"
            "% numbers are read verbatim from exp/results/*.json\n")


def load_scores():
    order, scores = [], {}
    for name in ("B1", "B2", "M1", "A1"):
        p = RESULTS / f"{name}.json"
        if not p.exists():
            continue
        data = json.loads(p.read_text(encoding="utf-8"))
        v = (data.get("metrics") or {}).get("score")
        if isinstance(v, (int, float)):
            order.append(name)
            scores[name] = float(v)
    return order, scores


def fig1():
    order, scores = load_scores()
    if not scores:
        return None
    coords = " ".join(f"({k},{scores[k]:.4f})" for k in order)
    ymax = max(scores.values()) + 0.1
    return (PREAMBLE +
            "\\begin{tikzpicture}\n"
            "\\begin{axis}[ybar,bar width=18pt,width=0.9\\columnwidth,"
            f"height=6cm,ymin=0,ymax={ymax:.2f},"
            f"symbolic x coords={{{','.join(order)}}},"
            "xtick=data,ylabel={score},enlarge x limits=0.2,"
            "nodes near coords,"
            "every node near coord/.append style={font=\\small}]\n"
            "\\addplot[fill=gray!35] coordinates {" + coords + "};\n"
            "\\end{axis}\n\\end{tikzpicture}\n")


def fig2(caption):
    if not caption.isascii():
        caption = ("Method overview (hypothesis recorded verbatim in "
                   "experiment\_contract.yaml)")
    safe = caption.replace('"', "'")[:120]
    for a, b in (("\\", ""), ("&", r"\&"), ("%", r"\%"), ("#", r"\#"),
                 ("_", r"\_")):
        safe = safe.replace(a, b)
    return (PREAMBLE +
            "\\begin{tikzpicture}[node distance=6mm,"
            "box/.style={draw,rounded corners,minimum width=34mm,"
            "minimum height=9mm,align=center,font=\\small}]\n"
            "\\node[box] (q) {Query +\\\\context pool};\n"
            "\\node[box,right=of q] (r) {Retrieval\\\\(top-$k$)};\n"
            "\\node[box,right=of r] (m) {Safety-priority\\\\filtering};\n"
            "\\node[box,right=of m] (o) {Compressed\\\\context};\n"
            "\\draw[->] (q) -- (r); \\draw[->] (r) -- (m); "
            "\\draw[->] (m) -- (o);\n"
            "\\node[below=4mm of r,font=\\scriptsize,text width=9cm,"
            f"align=center] {{{safe}}};\n"
            "\\end{tikzpicture}\n")


def main():
    f1 = fig1()
    if f1:
        (HERE / "fig1.tex").write_text(f1, encoding="utf-8")
    contract = {}
    cp = PROJECT / "plan" / "experiment_contract.yaml"
    if cp.exists():
        try:
            import yaml
            contract = yaml.safe_load(cp.read_text(encoding="utf-8")) or {}
        except Exception:
            pass
    caption = contract.get("hypothesis", "Method overview")
    (HERE / "fig2.tex").write_text(fig2(caption), encoding="utf-8")
    print("FIGURES_OK")


if __name__ == "__main__":
    main()
'''


def generate_figures(paper_dir: Path | str,
                     hypothesis: str = "") -> Dict[str, str]:
    """落盘绘图脚本并在 results 上执行（§5.4：图数=真实数据）。"""
    figures_dir = Path(paper_dir) / "figures"
    figures_dir.mkdir(parents=True, exist_ok=True)
    script = figures_dir / "make_figures.py"
    script.write_text(FIGURE_SCRIPT, encoding="utf-8")
    proc = subprocess.run([sys.executable, str(script)], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=60)
    if proc.returncode != 0 or "FIGURES_OK" not in proc.stdout:
        raise RuntimeError(f"图表生成脚本失败: {proc.stderr[-400:]}")
    produced = {f.stem: str(f) for f in figures_dir.glob("fig*.tex")}
    return produced


# ------------------------------------------------------------------ agent
class AnalysisAgent:
    """证据审计 → blueprint.json（§5.4 Step1，Writing Swarm 入口）。"""

    def __init__(self, topic: Dict[str, Any], llm_strong: Optional[LLMClient],
                 project: Path | str, paper_dir: Path | str,
                 metering=None):
        self.topic = topic
        self.llm = llm_strong
        self.project = Path(project)
        self.paper_dir = Path(paper_dir)
        self.metering = metering

    def run(self, contract_path: Optional[Path | str] = None) -> Dict[str, str]:
        t0 = time.perf_counter()
        ev = collect_evidence(self.project, contract_path)
        claims = build_claims(ev)
        if not claims:
            raise RuntimeError("AnalysisAgent 无可用制品证据（results 为空？）——"
                               "拒绝产出空蓝图")
        if self.llm is not None:
            claims = self._refine(claims, ev)

        blueprint = {
            "schema": SCHEMA_VERSION,
            "paper_title": (f"Toward {self.topic.get('title') or self.topic.get('name')}"
                            if not ev["contract"].get("hypothesis") else
                            self._title(ev)),
            "central_claim": (ev["contract"].get("hypothesis")
                              or claims[0]["text"]),
            "claims": claims,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        problems = validate_blueprint(blueprint) + \
            check_evidence_files(blueprint, self.project)
        if problems:
            raise RuntimeError(f"blueprint 校验失败（证据优先硬规则）: {problems}")

        figures = generate_figures(self.paper_dir,
                                   ev["contract"].get("hypothesis", ""))
        blueprint["figures"] = figures

        self.paper_dir.mkdir(parents=True, exist_ok=True)
        bp_path = self.paper_dir / "blueprint.json"
        bp_path.write_text(json.dumps(blueprint, ensure_ascii=False, indent=2),
                           encoding="utf-8")
        self._record(t0, len(claims), len(figures))
        print(f"[analysis] blueprint 落盘: {bp_path}（{len(claims)} claims, "
              f"{len(figures)} figures，证据链接全部存在）")
        return {"blueprint": str(bp_path)}

    # ------------------------------------------------------------ internals
    def _title(self, ev: Dict[str, Any]) -> str:
        """从契约假设提炼英文标题（LLM 缺省时用主题名兜底）。"""
        return (f"An Empirical Study on {self.topic.get('title') or self.topic.get('name')}: "
                "Evidence from Contract-Gated Automated Experiments")

    def _refine(self, claims: List[Dict[str, Any]],
                ev: Dict[str, Any]) -> List[Dict[str, Any]]:
        """LLM 仅润色 claim 文本；evidence 保持制品推导结果不变。

        校验：输出条数/id 必须与输入一一对应，text 非空且不得引入新数值
        （数值白名单**按 claim 隔离** = 该 claim 证据事实中出现过的浮点数；
        跨 claim 挪用他人数值同样视为证据外数值）；任何违规整条回落
        确定性文本。
        """
        facts = {c["id"]: c["text"] for c in claims}
        # M4：白名单按 claim 隔离——C1 的白名单不含 C2 的分数，
        # LLM 把 C2 的数值挪进 C1 属于证据外数值，整条回落
        allowed_by_id = {cid: _extract_numbers(txt)
                         for cid, txt in facts.items()}
        prompt = (
            "Below are evidence-grounded claim statements of a paper blueprint.\n"
            f"{json.dumps(facts, ensure_ascii=False, indent=1)}\n\n"
            "Rewrite each into polished academic English. Rules:\n"
            "1. Keep every number EXACTLY as given; introduce no new numbers;\n"
            "2. Do not strengthen or weaken the claim semantics;\n"
            f"3. Output JSON: {{\"claims\": [{{\"id\": ..., \"text\": ...}}, ...]}}"
        )
        try:
            cli = self.llm.bind("writing", "analysis")
            resp = cli.chat(prompt, system=ANALYSIS_SYSTEM,
                            max_tokens=ANALYSIS_MAX_TOKENS)
            data = extract_json_object(LLMClient.text_of(resp))
            refined = {c.get("id"): str(c.get("text") or "").strip()
                       for c in data.get("claims", []) if isinstance(c, dict)}
        except Exception as exc:  # LLM 故障不阻塞写作：回落确定性文本
            print(f"[analysis] LLM 润色失败回落确定性文本: {exc}")
            return claims
        out = []
        for c in claims:
            text = refined.get(c["id"]) or c["text"]
            new_numbers = _extract_numbers(text) - allowed_by_id[c["id"]]
            if not text or new_numbers:
                # 引入未见于本 claim 证据的数值 = 结果幻觉信号，整条回落
                text = c["text"]
            out.append({**c, "text": text})
        return out

    def _record(self, t0: float, n_claims: int, n_figures: int) -> None:
        if self.metering is None:
            return
        self.metering.record(stage="writing", agent="analysis",
                             model="pipeline",
                             latency_ms=int((time.perf_counter() - t0) * 1000),
                             extra={"claims": n_claims, "figures": n_figures})


def _extract_numbers(text: str) -> set:
    """文本中数值的数值化集合（符号/指数形态归一，跨形态一致比对）。"""
    out = set()
    for tok in re.findall(r"[-+]?\d+\.\d+(?:[eE][-+]?\d+)?", text):
        try:
            out.add(float(tok))
        except ValueError:
            pass
    return out


_SYMBOL_MAP = (("≥", ">="), ("≤", "<="), ("≠", "!="), ("→", "->"),
               ("×", "x"), ("—", "-"), ("–", "-"))


def _ascii_reason(reason: Any) -> str:
    """门判定 reason 尽力转 ASCII（全角括号 NFKC、数学符号映射）。

    experiment.py 的 reason 恒含全角括号与 ≥/≤——不归一化则 C3 恒被判
    非 ASCII，模板路径的门判定声明恒退化为制品指向句（M5）。归一化后仍
    非 ASCII（如中文缺失说明）则保持原样，由 _claim_tex 的防线兜底。
    """
    s = unicodedata.normalize("NFKC", str(reason if reason else "see artifact"))
    for a, b in _SYMBOL_MAP:
        s = s.replace(a, b)
    return s
