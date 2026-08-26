# -*- coding: utf-8 -*-
"""FormatAgent（设计文档 §5.4 Step3）：LaTeX 编译 + 合规/一致性审计。

三道校验（审计逻辑消费 jiuwenswarm.common.academic_format，PR3 组件，
移植 FARS internal-consistency 检查）：
1. BibTeX 校验：正文 \\cite key 必须全部存在于 references.bib；
2. 数值一致性审计：draft.tex 中出现的小数必须全部来自制品数值登记表
   （exp/results + gate_verdict）——防止不同 run 的数值混用或 LLM 编造数字，
   这是 FARS 八类诚信失效模式之一；
3. tectonic 编译（XeTeX 引擎，离线 bundle），产出 paper_v1.pdf。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from jiuwenswarm.common.academic_format import (  # PR3 组件
    AcademicFormatValidator, audit_abstract, audit_citations, audit_numbers)

__all__ = ["FormatAgent", "compile_tex", "find_tectonic", "number_registry",
           "build_validator", "audit_abstract", "audit_citations",
           "audit_numbers"]


# ------------------------------------------------------------------ audits
def build_validator(project: Path | str) -> AcademicFormatValidator:
    """从制品构建数值登记表校验器：results 各任务均值/per-seed + 门判定
    数值 + margin 派生量。统一登记 4 位规范化与原文两种形态。"""
    project = Path(project)
    validator = AcademicFormatValidator()
    results_dir = project / "exp" / "results"

    for f in results_dir.glob("*.json"):
        if f.name.endswith(".run_meta.json") or f.stem == "run_summary":
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        metrics = data.get("metrics") or data  # gate/negative 无 metrics 包裹
        for v in metrics.values():
            validator.register_number(v)
            if isinstance(v, dict):  # per_seed
                for vv in v.values():
                    if isinstance(vv, dict):
                        for vvv in vv.values():
                            validator.register_number(vvv)
                    else:
                        validator.register_number(vv)
    # margin 是登记数值的派生量（gate reason 中会出现），显式入册
    gate_path = results_dir / "gate_verdict.json"
    if gate_path.exists():
        try:
            gate = json.loads(gate_path.read_text(encoding="utf-8"))
            mv, bv = gate.get("main_value"), gate.get("baseline_value")
            if isinstance(mv, (int, float)) and isinstance(bv, (int, float)):
                validator.register_number(mv - bv)
        except (json.JSONDecodeError, OSError):
            pass
    return validator


def number_registry(project: Path | str) -> set:
    """制品数值登记表（兼容入口；新代码请用 build_validator）。"""
    return build_validator(project).registry


# ------------------------------------------------------------------ compile
def find_tectonic() -> Optional[str]:
    """tectonic 可执行文件定位：PATH → 环境变量 → conda env 常见位置。"""
    exe = shutil.which("tectonic")
    if exe:
        return exe
    env_bin = os.environ.get("TECTONIC_BIN")
    if env_bin and Path(env_bin).exists():
        return env_bin
    candidates = [
        Path(os.environ.get("CONDA_PREFIX", "")) / "Library" / "bin" / "tectonic.exe",
        Path.home() / ".conda" / "envs" / "JiuwenSwarm" / "Library" / "bin" / "tectonic.exe",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def compile_tex(paper_dir: Path | str, tex_name: str = "draft.tex",
                out_name: str = "paper_v1.pdf",
                tectonic_bin: Optional[str] = None) -> Dict[str, Any]:
    """tectonic 编译（失败重试一次；bundle 首次拉取需网络）。"""
    paper_dir = Path(paper_dir)
    exe = tectonic_bin or find_tectonic()
    if exe is None:
        return {"ok": False, "pdf": None,
                "error": "tectonic 不可用（PATH/TECTONIC_BIN/conda 均未找到）"}
    last_err = ""
    default_pdf = paper_dir / (Path(tex_name).stem + ".pdf")
    target = paper_dir / out_name
    for attempt in range(2):
        try:
            proc = subprocess.run(
                [exe, tex_name, "--outdir", "."],
                cwd=str(paper_dir), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=600)
        except (subprocess.TimeoutExpired, OSError) as exc:
            # 超时/可执行失效也是编译失败的一种：记入报告后重试，
            # 而非裸 traceback 穿透（违背先写报告再抛错的契约）
            last_err = f"tectonic invocation failed: {exc}"
            time.sleep(1.0)
            continue
        if proc.returncode == 0 and default_pdf.exists():
            default_pdf.replace(target)  # tectonic 按输入名产出，统一改名
            return {"ok": True, "pdf": str(target), "attempts": attempt + 1}
        last_err = (proc.stderr or proc.stdout)[-800:]
        time.sleep(1.0)  # 网络抖动拉 bundle 的短退避
    return {"ok": False, "pdf": None, "error": last_err}


# ------------------------------------------------------------------ agent
class FormatAgent:
    """编译 + 审计三件套 → format_report.json（§5.4 Step3）。"""

    def __init__(self, project: Path | str, paper_dir: Path | str,
                 metering=None, tectonic_bin: Optional[str] = None):
        self.project = Path(project)
        self.paper_dir = Path(paper_dir)
        self.metering = metering
        self.tectonic_bin = tectonic_bin

    def run(self, draft_path: Optional[Path | str] = None,
            bib_path: Optional[Path | str] = None,
            compile_pdf: bool = True) -> Dict[str, Any]:
        t0 = time.perf_counter()
        draft_path = Path(draft_path or self.paper_dir / "draft.tex")
        bib_path = Path(bib_path or self.paper_dir / "references.bib")
        tex_text = draft_path.read_text(encoding="utf-8")
        bib_text = (bib_path.read_text(encoding="utf-8")
                    if bib_path.exists() else "")

        validator = build_validator(self.project)
        problems = validator.validate(tex_text, bib_text)

        report: Dict[str, Any] = {
            "schema": "v0",
            "draft": str(draft_path),
            "audit_problems": problems,
            "compile": None, "pdf": None,
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        if compile_pdf:
            comp = compile_tex(self.paper_dir, draft_path.name,
                               tectonic_bin=self.tectonic_bin)
            report["compile"] = comp
            report["pdf"] = comp.get("pdf")
            if not comp["ok"]:
                problems.append(f"LaTeX 编译失败: {comp.get('error', '')[:200]}")

        report_path = self.paper_dir / "format_report.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        self._record(t0, problems, report.get("pdf"))

        if problems:
            raise RuntimeError(f"FormatAgent 校验未通过: {problems}")
        print(f"[format] 校验全过，PDF: {report['pdf']}")
        return report

    def _record(self, t0: float, problems: List[str], pdf) -> None:
        if self.metering is None:
            return
        self.metering.record(stage="writing", agent="format",
                             model="pipeline",
                             latency_ms=int((time.perf_counter() - t0) * 1000),
                             extra={"problems": len(problems),
                                    "pdf": bool(pdf)})
