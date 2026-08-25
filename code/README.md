# miniFARS code —— 基于 JiuwenSwarm 的四阶段科研流水线

参赛代码包（CCF BDCI 2026 · 华为 openJiuwen 赛题）。设计依据：根目录《方案设计文档.md》。

## 快速开始

```powershell
# 依赖（JiuwenSwarm conda 环境已含；干净环境装这两个即可）
pip install -r requirements.txt

# 密钥：.env 四件套（MODEL_PROVIDER/MODEL_NAME/API_BASE/API_KEY），
# 加载顺序：进程环境变量 > <repo>/.env > ~/.jiuwenswarm/.env

# 空跑骨架（不调 LLM）
python code/main.py --topic agent_context --dry-run

# 空跑 + 3 次真实极小调用（验证计量流水）
python code/main.py --topic agent_context --dry-run --smoke
```

## 目录

| 文件 | 职责 | 设计文档章节 |
|---|---|---|
| `main.py` | CLI 入口 | — |
| `config.yaml` | 分级模型路由（strong/light）+ 计量价格表 | §6.2 |
| `topics/agent_context.yaml` | 主题 A：Agent 上下文工程（子方向 + 算力预算） | §2.1 |
| `minifars/workspace.py` | 共享工作区初始化器（目录规范 + git init） | §6.1 |
| `minifars/artifacts.py` | 制品 schema v0（proposals / results / run_meta / blueprint） | §5、§6.1 |
| `minifars/metering.py` | **MeteringMiddleware**：token/时长/成本流水（PR2 候选） | §6.2、§7 |
| `minifars/llm.py` | LLM 客户端（Anthropic 协议，全部调用经计量中间件） | §6.2 |
| `minifars/pipeline.py` | `paper_pipeline` 顶层编排（四阶段串行，阶段间只传制品路径） | §4.2 |

## 约定

- 每次运行在 `workspace/<topic>-<日期>-<时分>/` 建独立项目，`git init` 后每阶段一个 `[stage]` 提交；
- 计量流水 `metering/calls.jsonl` 逐条记录 {阶段, agent, 模型, tokens, 时延, 成本}，
  D5 的 `resource_report.md` 由其自动生成；
- 阶段间只传制品路径不传全文（上下文压缩红线）。

## 已知注意

- MiniMax-M2 响应含 `thinking` 块且与正文共享 `max_tokens` 预算：预算过小时正文可能为空，
  `LLMClient.text_of()` 已做兜底，正式调用请按任务给足预算。
