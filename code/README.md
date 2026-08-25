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

# 只跑 ideation 阶段（D2 实装：Survey + Hypothesis + Peer 质询 + Gate 打分）
python code/main.py --topic agent_context --stages ideation

# 单测（全离线 mock，无网络依赖）
python -m pytest code/tests -q
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
| `minifars/survey.py` | **SurveyAgent**：arXiv/S2 检索 → 文献卡片 + 研究空白清单（light 档） | §5.1 |
| `minifars/hypothesis.py` | **HypothesisAgent**：空白清单 → 5~8 候选假设 P0xx.md + 辩论精炼 refine（strong 档） | §5.1 |
| `minifars/peer.py` | **PeerAgent**：本地新颖性查重线索 + 同行质询 peer_review_r{n}.json（strong 档） | §5.1 |
| `minifars/gate.py` | **GateAgent**：四维 rubric 打分 + 域约束硬检查，消费 PR1 QualityGate 组件（含熔断），出口 accepted_proposal.md | §5.1 |
| `scripts/verify_apis.py` | 文献检索 API 连通性验证（arXiv / Semantic Scholar） | §5.1 |

## 约定

- 每次运行在 `workspace/<topic>-<日期>-<时分>/` 建独立项目，`git init` 后每阶段一个 `[stage]` 提交；
- 计量流水 `metering/calls.jsonl` 逐条记录 {阶段, agent, 模型, tokens, 时延, 成本}，
  D5 的 `resource_report.md` 由其自动生成；
- 阶段间只传制品路径不传全文（上下文压缩红线）。

## 已知注意

- MiniMax-M2 响应含 `thinking` 块且与正文共享 `max_tokens` 预算：预算过小时正文可能为空，
  `LLMClient.text_of()` 已做兜底，正式调用请按任务给足预算。
- 文献检索（2026-08-25 实测）：arXiv API 可用（https + follow_redirects）；
  Semantic Scholar 公共池持续 429 限流，需申请免费 API key 或降级 arXiv 单源。
- LaTeX 工具链：tectonic 0.17.0（conda 安装于 JiuwenSwarm 环境 `Library\bin`），
  ICLR 风格冒烟编译通过（数学/表格/引用/交叉引用，产物在 `tools/latex_smoke/`，不入库）。
- GateAgent 依赖 `jiuwenswarm.common.quality_gate`（PR1 组件）：经 JiuwenSwarm 环境
  的 workswarm 可编辑安装引入，无需额外配置；Ideation 熔断语义 = 连续 3 轮无候选
  过门 → 强制放行最高分（`accepted_proposal.md` front-matter `status=forced_accept`）。
