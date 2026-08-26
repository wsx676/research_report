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

# 只跑 planning + experiment 阶段（D3 实装：契约冻结 + 沙箱执行）
python code/main.py --topic agent_context --stages planning,experiment

# 只跑 writing 阶段（D4 实装：证据审计 → ICLR 模板 draft → PDF）
python code/main.py --topic agent_context --stages writing

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
| `minifars/contract.py` | experiment_contract.yaml schema + 校验器（baselines≥2、analysis≥1、指标一致性） | §5.2 |
| `minifars/planner.py` | **PlannerAgent**：accepted_proposal → 实验契约（校验失败自纠重试一次） | §5.2 |
| `minifars/skills.py` | 策展技能库 v1：benchmark runner/多 seed/绘图/LLM-as-Judge 四模板 + 合成方法库 | §5.3 |
| `minifars/experiment.py` | **ExperimentOrchestrator**：模板渲染 → 沙箱执行 → run_meta 落盘 + 有效性门 + 预算降级，消费 PR2 CheckpointManager 断点续跑 | §5.3 |
| `minifars/analysis.py` | **AnalysisAgent**：制品证据审计 → blueprint.json（无证据不入蓝图，LLM 只润色不得引入新数值）+ 绘图脚本生成 pgfplots/TikZ 图 | §5.4 |
| `minifars/draft.py` | **DraftAgent** + CitationChecker：按蓝图逐节写 draft.tex（引用白名单=Survey 卡片）+ references.bib | §5.4 |
| `minifars/format_check.py` | **FormatAgent**：tectonic 编译 + BibTeX/数值一致性/摘要审计，消费 PR3 AcademicFormatValidator 组件 | §5.4 |
| `minifars/templates/iclr2026_conference.sty` | vendored 轻量 ICLR 会议版式（natbib + pgfplots，tectonic 离线可编译） | §5.4 |
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
- GateAgent 依赖 `jiuwenswarm.common.quality_gate`（PR1 组件），ExperimentOrchestrator
  依赖 `jiuwenswarm.common.checkpoint`（PR2 组件），FormatAgent 依赖
  `jiuwenswarm.common.academic_format`（PR3 组件）：均经 JiuwenSwarm 环境
  的 workswarm 可编辑安装引入，无需额外配置；Ideation 熔断语义 = 连续 3 轮无候选
  过门 → 强制放行最高分（`accepted_proposal.md` front-matter `status=forced_accept`）。
- 实验执行（D3）：每任务 = 技能库模板渲染脚本 → subprocess 沙箱（墙钟看门狗 +
  PermissionError 退避重试）→ `results/<task_id>.json` + `<task_id>.run_meta.json`
  （命令/seed/模型版本/token/时间戳五要素）；每任务完成即 git commit（检查点），
  杀进程后重跑自动从未完成任务续跑；main 后过有效性门，不支持假设 →
  跳过 analysis 且 `negative_result.json` 完整保留负结果（FARS 算法诚实）。
  D3 用确定性合成基准（`skills.SYNTHETIC_SCORES`）验证引擎全链路，
  正式轮次接入真实评测数据集时扩展方法库与指标别名表即可。
- 写作链（D4）：AnalysisAgent 的 claim 证据链只从制品推导（LLM 仅润色文本，
  引入未登记数值即整条回落）；DraftAgent 引用白名单 = Survey 文献卡片
  （CitationChecker 拒绝幻觉引用）；FormatAgent 数值一致性审计 = draft 中
  每个小数必须在 exp/results 登记表内（防跨 run 数值混用/LLM 编造），
  通过后 tectonic 编译产出 `paper/paper_v1.pdf` + `format_report.json`。
  负结果经 limitations 节按 candid analysis 诚实成文。
