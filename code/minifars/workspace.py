# -*- coding: utf-8 -*-
"""共享工作区初始化器（设计文档 §6.1：制品留痕 = 可审计性）。

规范目录：
    workspace/<project_id>/
    ├── topic.yaml
    ├── proposals/          # 全部候选假设（含淘汰的）
    ├── plan/               # experiment_contract.yaml
    ├── exp/{code, logs, results, checkpoints}/
    ├── paper/{figures}/    # blueprint.json / draft.tex / final.pdf
    └── metering/           # token/时长逐条流水

初始化即 git init，所有阶段提交都进版本库（决赛复现审核出示完整轨迹）。
"""
from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from typing import List, Optional

from . import STAGES

# §6.1 目录规范（相对 project 目录）
WORKSPACE_DIRS: List[str] = [
    "proposals",
    "plan",
    "exp/code",
    "exp/logs",
    "exp/results",
    "exp/checkpoints",
    "paper/figures",
    "metering",
]

# 阶段 → 主要落盘目录映射（编排器用于阶段间只传制品路径，§4.2）
STAGE_DIRS = {
    "ideation": "proposals",
    "planning": "plan",
    "experiment": "exp",
    "writing": "paper",
}

GITIGNORE = """# 运行期大文件/缓存不进版本库（制品本体全部入库）
__pycache__/
*.pyc
exp/logs/*.log.bak
"""


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def _ensure_git_identity(repo: Path) -> None:
    """仅在当前仓库缺失身份时设置 local 级身份（不动全局 git config）。"""
    for key, fallback in (("user.name", "miniFARS"), ("user.email", "minifars@local")):
        probe = subprocess.run(["git", "config", key], cwd=str(repo),
                               capture_output=True, text=True)
        if probe.returncode != 0 or not probe.stdout.strip():
            _git(repo, "config", "--local", key, fallback)


def init_workspace(workspace_root: Path | str, project_id: str,
                   topic_file: Optional[Path | str] = None) -> Path:
    """按 §6.1 建 workspace/<project_id>/ 并 git init。幂等，可重复调用。

    Args:
        workspace_root: workspace 根目录
        project_id: 项目标识（如 agent_context-20260825-1645）
        topic_file: 可选的 topic.yaml 源文件，存在则复制入工作区

    Returns:
        项目目录 Path
    """
    project = Path(workspace_root) / project_id
    for rel in WORKSPACE_DIRS:
        (project / rel).mkdir(parents=True, exist_ok=True)
    if topic_file is not None:
        shutil.copyfile(topic_file, project / "topic.yaml")

    if not (project / ".git").exists():
        _git(project, "init", "-b", "main")
        _ensure_git_identity(project)
        (project / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
        commit(project, f"chore: init workspace {project_id} (§6.1 skeleton)")
    return project


def commit(project: Path | str, message: str, allow_empty: bool = False) -> Optional[str]:
    """git add -A + commit；无变更时返回 None（幂等续跑友好）。"""
    project = Path(project)
    _git(project, "add", "-A")
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=str(project))
    if staged.returncode == 0 and not allow_empty:
        return None
    args = ["commit", "-m", message]
    if allow_empty:
        args.append("--allow-empty")
    _git(project, *args)
    return _git(project, "rev-parse", "--short", "HEAD").stdout.strip()


def commit_stage(project: Path | str, stage: str, note: str = "") -> Optional[str]:
    """阶段完成后打一个规范化提交（CheckpointManager 的前身约定，D3 扩展）。"""
    assert stage in STAGES, f"unknown stage: {stage}"
    msg = f"[{stage}] {note}".rstrip()
    return commit(project, msg)


def new_project_id(topic_name: str) -> str:
    return f"{topic_name}-{time.strftime('%Y%m%d-%H%M')}"
