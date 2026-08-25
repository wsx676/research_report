# -*- coding: utf-8 -*-
"""miniFARS —— 基于 JiuwenSwarm 的可审计四阶段科研流水线（参赛代码包）。

模块划分（对齐《方案设计文档.md》）：
- config.py    配置加载（config.yaml + .env 分级路由，§6.2）
- workspace.py 共享工作区初始化器（§6.1 目录规范 + git init）
- artifacts.py 制品 schema v0（proposals / results / run_meta / blueprint）
- metering.py  MeteringMiddleware 最小版（§6.2，PR2 候选组件）
- llm.py       LLM 客户端（Anthropic 协议 + 中间件链）
- pipeline.py  paper_pipeline 顶层编排（四阶段串行，阶段间只传制品路径，§4.2）
"""

__version__ = "0.1.0"

STAGES = ("ideation", "planning", "experiment", "writing")
