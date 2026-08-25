# openJiuwen框架贡献说明文档

## 1. 代码优化点

1. **Agent调度优化**：优化openJiuwen子Agent并行调度逻辑，减少上下文重复传输，降低Token消耗。
2. **记忆模块轻量化**：优化MemoryPool持久化策略，减少磁盘IO与内存占用。
3. **工具调用容错增强**：新增工具调用超时重试、异常捕获，提升Agent稳定性。
4. **学术格式模块拓展**：新增论文摘要、关键词、参考文献格式校验插件，拓展openJiuwen科研场景能力。

## 2. 功能拓展点

- 新增科研论文写作专用子Agent模板
- 新增PDF导出标准化接口
- 新增学术规范校验工具集

## 3. PR提交信息

- PR名称：【科研场景拓展】优化Agent调度+新增论文写作模块
- PR链接：https://github.com/xxx/openJiuwen/pull/XXXX（样例占位链接，实际替换真实PR）
- 提交分支：feature/sci-paper-agent
- 合并状态：待合并 / 已合并（根据实际填写）

## 4. 兼容性说明

本次优化完全兼容openJiuwen现有版本，无破坏性变更，可直接用于科研、写作类Agent开发。