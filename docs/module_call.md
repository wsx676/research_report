# openJiuwen / JiuwenSwarm 模块调用说明（module_call.md）

> 状态：**草稿 v0.1**（D1 验收遗留项，D2 起随实现持续更新）
> 依据：本地 jiuwenswarm 源码（atomgit openJiuwen/jiuwenswarm，develop 分支）实测调研，引用处标注源码路径。
> 用途：参赛提交 docs/module_call.md 底稿——复述 JiuwenSwarm 的 Skill 与子 Agent 注册机制，并说明 miniFARS 的挂接方式。

---

## 1. 总体分层

```
jiuwenswarm（应用层，本仓库）           ← Team/Swarm 编排、Web 通道、技能市场
    │  agents/swarm/registry.py
    ▼  register_swarm_providers()
openjiuwen（框架层，pip 依赖 0.1.17）  ← harness 元素注册表、rail 回调、内置子 Agent
    │
    ▼
workswarm（运行时底座 0.2.5b1）        ← Agent Server / Gateway / 频道进程模型
```

jiuwenswarm 不直接改写框架内部，而是把自有能力（工具、rail、子 Agent）**注册**进 openjiuwen 的统一登记表，由框架按 `RailSpec` / `BuiltinToolSpec` / `SubAgentSpec` 装配到每个 Team 成员上。

## 2. Skill 注册机制

### 2.1 物理形态：目录 + SKILL.md

一个 Skill = 一个目录，内含 `SKILL.md`：

```
skills/<skill_name>/SKILL.md
```

`SKILL.md` = **YAML front-matter + Markdown 提示词正文**（实测样例：`~/.jiuwenswarm/agent/workspace/skills/skill-creator/SKILL.md`）：

```yaml
---
name: skill-creator
description: Unified entry point for all Skill creators. ...（英文，检索索引用）
description_cn: 所有 Skill Creator 的统一入口。...（中文展示用）
---
# Skill Creator（统一入口 / 路由）
你是所有 Skill Creator 的统一入口，同时负责路由：...
```

要点：`description` 参与 Skill 检索索引（能力树节点描述上限 150 字符，见 `symphony/indexing/tree/schema.py` 的 `SKILL_DESCRIPTION_MAX_LENGTH`），写法上必须"动词开头、说清触发场景"；正文是给模型的行为指令。

### 2.2 四个技能来源

| 来源 | 物理位置 | 说明 |
|---|---|---|
| 内置技能 | 仓库 `builtin skills dir` | 随发行版分发 |
| 用户技能 | `~/.jiuwenswarm/agent/workspace/skills/` | 单 Agent 模式的工作库（本机已验证：skill-creator、swarmskill-creator 等 5 个） |
| MCP 捆绑 | `<mcp_pkg>/skills/`（每子目录一技能；扁平布局则整包一技能） | MCP 安装时自动注入（`server/runtime/mcp/skill_installer.py`） |
| Team Skills Hub | 市场 `https://teamskills.openjiuwen.com`（`/api/v1/plugins` 搜索、`/api/v1/artifacts/{id}` 安装） | 技能市场：搜索/安装/发布/删除四操作（`skill_manager.py`） |

### 2.3 管理器与生命周期

- **`SkillManager`**（`jiuwenswarm/server/runtime/skill/skill_manager.py`，约 4200 行）是唯一管理者：
  - skills 根目录随模式切换：workspace 模式 = `<workspace>/skills`；独立模式 = 全局 agent 目录；
  - 状态文件 `skills_state.json`、市场目录 `skills/_marketplace`；
  - **`reload_skills()`**：重新扫描 skills_dir，增量移除已删除 Skill 的缓存——技能热加载入口（`agent_adapter/interface.py` 在技能变更后主动调用以立即生效）。
- **可见性控制**：`skills-visibility.json` 由 `TeamWorkspaceManager.initialize` 单一写者维护——"Skill 只有一份物理库，按 Agent 的可见性是元数据"（`team_helpers.py` 原注释）。
- **检索**：`SKILL_RETRIEVAL` 工具 + `symphony/skill_retrieval`（能力树召回，`scan_skill_inventory` 扫描 manager 的 skills 目录建索引）。
- **技能进化**：`TEAM_SKILL_EVOLUTION` / `TEAM_SKILL_CREATE` / `MEMBER_SKILL_EVOLUTION` rail——Team 运行中可自主创建/进化技能并回写库。

## 3. 子 Agent（Sub-Agent）注册机制

### 3.1 声明：`@harness_element` 装饰器

每个子 Agent 是一个**工厂函数** + 装饰器声明（实测样例：`jiuwenswarm/agents/swarm/providers/code_subagents.py`）：

```python
@harness_element(
    kind=ElementKind.SUBAGENT,        # 元素类别：工具/rail/子 Agent 三类之一
    name=CODE_AGENT,                  # 注册名，SubAgentSpec.factory_name 按此解析
    description="Code execution sub-agent reusing ...",
    input_model=CodeAgentInput,       # 构造参数的 Pydantic 模型（ConstructionInput）
)
def build_code_agent(factory_kwargs: dict, ctx: SwarmBuildContext):
    inp = CodeAgentInput.resolve(factory_kwargs, ctx)
    model = ctx.extras.get(_PARENT_MODEL_EXTRAS_KEY)   # 复用父 Agent 的模型
    ...
    return build_code_agent_config(model, ...)
```

### 3.2 注册：manifest catalog → 框架登记表

`jiuwenswarm/agents/swarm/registry.py` 的 `register_swarm_providers()` 是全流程入口（**每进程一次，幂等**）：

1. import provider 模块 → 触发所有 `@harness_element` 声明 → 填充 **manifest catalog**（元素元数据的唯一真源）；
2. `ensure_harness_elements_registered()`：确保 openjiuwen 内置元素（explore/plan/browser 子 Agent、web_search/web_fetch/lsp/worktree 等工具）已声明注册；
3. `register_from_catalog()`：从 catalog 驱动真实注册——工具、工厂 rail、类 rail（经 `register_rail_provider`）、**子 Agent**；
4. `register_build_context_factory(...)`：注册 build context 种子工厂，使子 Agent 构造上下文可序列化/恢复（spawn payload / 会话恢复）。

### 3.3 装配：Spec 引用

配置侧（`config_specs`）通过 `SubAgentSpec(factory_name=...)` / `RailSpec` / `BuiltinToolSpec` **按名引用**注册表中的元素，框架据此为每个 Team 成员装配。分工：explore / plan / browser 由 openjiuwen 提供；code_agent、statusline_setup、swarm browser（按成员隔离的 browser_key）在 jiuwenswarm 侧。

### 3.4 Team 层（多 Agent 编排）

- **`TeamManager`**（`agents/harness/team/team_manager.py`）：会话级管理，挂载 rail 上下文（`register_team_rail_context`）、技能 rail（`register_team_skill_rail`）、监控器、流式任务；
- **A2X 客户端**（`agents/harness/team/a2x/client/`）：`register_agent` / `register_blank_agent` 向外部注册团队成员（A2X 跨进程 Agent 协议）；
- **SkillUseRail**（`agent_adapter/interface.py`）：把技能库接入模型调用的 rail 层。

## 4. Rail 回调体系（miniFARS PR2 挂点）

`openjiuwen/core/single_agent/rail/base.py` 提供框架级回调：

| 事件 | 时机 | miniFARS 用途 |
|---|---|---|
| `BEFORE_MODEL_CALL` / `AFTER_MODEL_CALL` | 每次 LLM 调用前后 | **MeteringRail**（PR2）：记 in/out tokens、时延、成本 → calls.jsonl |
| `ON_MODEL_EXCEPTION` | 调用异常 | 失败调用也计量（status=error） |
| `BEFORE_INVOKE` / `AFTER_INVOKE` | Agent 执行前后 | 阶段级计时 |

`ModelCallInputs(messages, tools, model_context, response)` 携带调用全量上下文与响应——计量中间件从 `response.usage` 提取 token 数。D1 的 `minifars/metering.py` 已按此数据形态实现，D3 移植为 rail 后经 `register_rail_provider` 注册。

## 5. miniFARS 挂接方式（我们的调用路径）

| 层 | 当前（D1 已实现） | 计划 |
|---|---|---|
| 顶层编排 | `code/main.py` → `minifars/pipeline.py` 四阶段串行（Python 直调，阶段间只传制品路径） | 封装为 `paper_pipeline` team skill（SKILL.md + SwarmFlow 工作流），经 SkillManager 热加载 |
| LLM 访问 | `minifars/llm.py`（Anthropic 协议直连，全部调用过 MeteringMiddleware） | 迁移到框架模型端点，计量改挂 `AFTER_MODEL_CALL` rail（PR2） |
| 质量门 | `minifars/` 流水线内调用 `jiuwenswarm/common/quality_gate.py`（PR1 组件，`GateConfig.ideation_default()` 四维 rubric） | 保持组件级复用；Ideation Swarm 的 GateAgent 直接调用 |
| 子 Agent | 暂未注册（编排在 Python 进程内） | D2 起将 Survey/Hypothesis/Peer/Gate 四角色注册为 swarm 子 Agent 或 team 成员 |

## 6. 对 PR 贡献的意义

向 JiuwenSwarm 贡献的通用组件（QualityGate、MeteringRail、CheckpointManager、AcademicFormatValidator）按上述体系落位：

- **纯组件**（无框架依赖）→ `jiuwenswarm/common/`（PR1 QualityGate 已落此，282 行 + 19 单测）；
- **需要拦截模型调用的**（计量）→ openjiuwen rail 体系（PR2）；
- 提交形式：feature 分支 + patch 兜底（`pr_patches/`），详见 framework_contribution.md（D5 编写）。

---

*调研证据：`agents/swarm/registry.py` L160-180、`providers/code_subagents.py` L92-98、`server/runtime/skill/skill_manager.py` L112-115/L4002、`~/.jiuwenswarm/agent/workspace/skills/*/SKILL.md`。*
