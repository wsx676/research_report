# -*- coding: utf-8 -*-
"""miniFARS 入口：四阶段科研流水线编排。

用法：
    python code/main.py --topic agent_context --dry-run            # 空跑骨架
    python code/main.py --topic agent_context --dry-run --smoke    # 空跑 + 3 次真实极小调用
    python code/main.py --topic agent_context                      # 真实执行（D2+ 逐步实装）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(CODE_DIR))

from minifars.config import load_config                     # noqa: E402
from minifars.pipeline import PaperPipeline, load_topic     # noqa: E402
from minifars.workspace import init_workspace, new_project_id  # noqa: E402


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="miniFARS paper_pipeline")
    p.add_argument("--topic", required=True,
                   help="主题名（对应 code/topics/<topic>.yaml）或 topic.yaml 路径")
    p.add_argument("--dry-run", action="store_true",
                   help="不调用 LLM，仅产出 schema 合规的占位制品")
    p.add_argument("--smoke", action="store_true",
                   help="附带 3 次真实极小 LLM 调用（验证计量流水，token 消耗可忽略）")
    p.add_argument("--project-id", default=None,
                   help="复用已有项目目录（断点续跑）；缺省按主题+时间新建")
    p.add_argument("--stages", default=None,
                   help="只跑指定阶段（逗号分隔，如 ideation）；默认全部四阶段")
    p.add_argument("--workspace-root", default=None, help="覆盖 config.yaml 的工作区根目录")
    p.add_argument("--config", default=None, help="config.yaml 路径（默认 code/config.yaml）")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    config = load_config(args.config)
    ws_root = Path(args.workspace_root) if args.workspace_root else config.workspace_root

    topic_path = Path(args.topic)
    if not topic_path.exists():
        topic_path = CODE_DIR / "topics" / f"{args.topic}.yaml"
    topic = load_topic(topic_path)

    project_id = args.project_id or new_project_id(topic["name"])
    project = init_workspace(ws_root, project_id, topic_file=topic_path)
    print(f"[main] workspace ready: {project}")

    stages = [s.strip() for s in args.stages.split(",")] if args.stages else None
    if stages:
        from minifars import STAGES
        bad = [s for s in stages if s not in STAGES]
        if bad:
            raise SystemExit(f"[main] 未知阶段 {bad}，可选：{list(STAGES)}")

    pipe = PaperPipeline(project, topic, config,
                         dry_run=args.dry_run, smoke=args.smoke)
    artifacts = pipe.run(stages=stages)

    summary = pipe.metering.summarize()
    print("[main] metering total:", json.dumps(summary["total"], ensure_ascii=False))
    print("[main] artifacts:", json.dumps(artifacts, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
