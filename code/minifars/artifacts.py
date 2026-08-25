# -*- coding: utf-8 -*-
"""制品 schema v0（设计文档 §5/§6.1 的落盘约定）。

三类制品 + 一个蓝图：
1. proposals/*.md     YAML front-matter 元数据 + 正文（含被淘汰的，供审计）
2. exp/results/*.json 实验指标；run_meta.json 记录复现要素
3. paper/blueprint.json  claim ↔ 证据映射（§5.4，D4 使用）

D1 只定 schema 与校验器；字段随 D2~D4 迭代向后兼容扩展。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

SCHEMA_VERSION = "v0"

# ------------------------------------------------------------------ proposals
PROPOSAL_REQUIRED_FIELDS = ("id", "title", "stage", "status", "created_at")
PROPOSAL_STATUSES = ("candidate", "accepted", "rejected", "forced_accept")


def proposal_front_matter(pid: str, title: str, status: str = "candidate",
                          gate_score: Optional[float] = None,
                          extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    meta = {
        "schema": SCHEMA_VERSION,
        "id": pid,
        "title": title,
        "stage": "ideation",
        "status": status,
        "gate_score": gate_score,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    meta.update(extra or {})
    return meta


def write_proposal(proposals_dir: Path | str, meta: Dict[str, Any], body_md: str,
                   filename: Optional[str] = None) -> Path:
    """落盘一个 proposal；缺省按 meta['id'] 命名（accepted_proposal.md 等
    固定名制品通过 filename 指定）。"""
    path = Path(proposals_dir) / (filename or f"{meta['id']}.md")
    fm = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False)
    path.write_text(f"---\n{fm}---\n\n{body_md.lstrip()}", encoding="utf-8")
    return path


def load_hypotheses(path: Path | str) -> List[Dict[str, Any]]:
    """读 proposals/hypotheses.json 的 hypotheses 数组（缺文件/坏结构返回 []）。"""
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    items = data.get("hypotheses", []) if isinstance(data, dict) else []
    return [d for d in items if isinstance(d, dict)]


def load_cards(path: Path | str) -> List[Dict[str, Any]]:
    """读 survey_cards.json 的 cards 数组（缺文件/坏结构返回 []）。"""
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    cards = data.get("cards", []) if isinstance(data, dict) else []
    return [d for d in cards if isinstance(d, dict)]


def parse_proposal(path: Path | str) -> Dict[str, Any]:
    """读回 front-matter 与正文；缺必填字段抛 ValueError。"""
    text = Path(path).read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, flags=re.S)
    if not m:
        raise ValueError(f"{path}: 缺少 YAML front-matter")
    meta = yaml.safe_load(m.group(1)) or {}
    missing = [k for k in PROPOSAL_REQUIRED_FIELDS if k not in meta]
    if missing:
        raise ValueError(f"{path}: front-matter 缺字段 {missing}")
    if meta.get("status") not in PROPOSAL_STATUSES:
        raise ValueError(f"{path}: 非法 status={meta.get('status')!r}")
    return {"meta": meta, "body": m.group(2)}


# ------------------------------------------------------------------ experiment
RUN_META_REQUIRED = ("task_id", "task_type", "command", "seed", "model",
                     "started_at", "finished_at", "status")
CONTRACT_TASK_TYPES = ("env_setup", "baselines", "main", "effectiveness_gate", "analysis")


def run_meta(task_id: str, task_type: str, command: str, seed: int, model: str,
             started_at: str, finished_at: str, status: str,
             tokens: Optional[Dict[str, int]] = None,
             extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """run_meta.json 构造器：复现五要素（命令/seed/模型版本/token/时间戳）。"""
    meta: Dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "task_id": task_id,
        "task_type": task_type,
        "command": command,
        "seed": seed,
        "model": model,
        "started_at": started_at,
        "finished_at": finished_at,
        "status": status,
        "tokens": tokens or {},
    }
    meta.update(extra or {})
    return meta


def validate_run_meta(meta: Dict[str, Any]) -> List[str]:
    """返回缺失/非法项列表（空 = 通过）。验收标准 D3-1 依赖此校验。"""
    problems = [f"missing:{k}" for k in RUN_META_REQUIRED if k not in meta]
    if meta.get("task_type") and meta["task_type"] not in CONTRACT_TASK_TYPES:
        problems.append(f"bad_task_type:{meta['task_type']}")
    return problems


def write_result(results_dir: Path | str, task_id: str, metrics: Dict[str, Any],
                 meta: Dict[str, Any]) -> Dict[str, Path]:
    """指标与 run_meta 成对落盘：results/<task_id>.json + <task_id>.run_meta.json"""
    results_dir = Path(results_dir)
    problems = validate_run_meta(meta)
    if problems:
        raise ValueError(f"run_meta 校验失败: {problems}")
    p_metrics = results_dir / f"{task_id}.json"
    p_meta = results_dir / f"{task_id}.run_meta.json"
    p_metrics.write_text(json.dumps({"schema": SCHEMA_VERSION, "task_id": task_id,
                                     "metrics": metrics}, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    p_meta.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"metrics": p_metrics, "run_meta": p_meta}


# ------------------------------------------------------------------ blueprint
def blueprint_skeleton(title: str, central_claim: str) -> Dict[str, Any]:
    """blueprint.json 骨架（§5.4）：每条 claim 必须显式链接证据，无证据不得入蓝图。"""
    return {
        "schema": SCHEMA_VERSION,
        "paper_title": title,
        "central_claim": central_claim,
        "claims": [
            # 示例条目，展示强约束结构；AnalysisAgent（D4）负责真实填充
            {
                "id": "C1",
                "text": "",
                "section": "",
                "evidence": [
                    {
                        "source_artifact": "exp/results/<task_id>.json",
                        "figure_candidate": "paper/figures/fig1.pdf",
                        "support_strength": "strong",  # strong | moderate | weak
                    }
                ],
            }
        ],
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def validate_blueprint(bp: Dict[str, Any]) -> List[str]:
    """硬规则：无证据支撑的 claim 拒绝入蓝图（§5.4 Step1）。"""
    problems = []
    for c in bp.get("claims", []):
        if not c.get("text"):
            problems.append(f"claim {c.get('id')}: 空文本")
        if not c.get("evidence"):
            problems.append(f"claim {c.get('id')}: 无证据链接（违反证据优先原则）")
    return problems
