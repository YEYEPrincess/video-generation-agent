# Agent 框架对照：LangGraph / Agents SDK / MCP

| 项 | 内容 |
|---|---|
| 日期 | 2026-10-26 |
| 目的 | 给 JD 第 4 条「快速理解 Agent 框架」补硬证据：不是"看过文档"，是"接过、跑过、说得清代价" |
| 模型 | DeepSeek `deepseek-chat`（同一把 key、同一套 prompt、同一套校验器） |
| 方法 | `scripts/compare_engines.py`，同一主题跑三种编排，只改"编排层"这一个变量 |

---

## 1. 三个适配器

| 文件 | 行数 | 它替代了什么 | 保留了什么 |
|---|---:|---|---|
| `app/agent/graph_engine.py` | 88 | 手写状态机的**节点调度** | 同一套 prompt、同一个 LLM 客户端、同一套校验器 |
| `app/agent/sdk_engine.py` | 52 | "让模型吐 JSON 再解析" | 契约校验（`ResearchBrief`）没有省 |
| `app/mcp_server.py` | 106 | 对外只暴露 4 个工具（提交/查状态/列任务/取成片） | 编排仍在 worker，不在工具里跑长任务 |

对照基线：`app/core/runner.py`（238 行）+ `app/core/queue.py`（134 行）。

---

## 2. 实测（同一主题）

```
[native   ] 5.91s  angle='年轻人重看老动画不是怀旧，是拿弹幕当解说、把老番当电子榨菜'
[langgraph] 6.20s  angle='年轻人重看老动画不是怀旧，是在逃离当下新番的信息过载'
[sdk      ] 3.63s  angle=68 字，tokens=1465，contract_valid=False
```

| 指标 | native | LangGraph | Agents SDK |
|---|---:|---:|---:|
| 编排层代码 | 238 行 | **88 行** | 52 行 |
| 端到端耗时 | 5.9s | 6.2s | 3.6–6.9s（波动大） |
| 模型调用次数 | 2 | 2（可重试回环） | 2（含一次 tool round-trip） |
| 产出结构 | JSON 文本 → 自己解析 | 图状态里的 dict | **tool 参数，直接是对象** |
| 契约合规 | 通过 | 通过 | **不通过**（angle 68 字 > 上限 40） |
| 断点续跑 | 自己写（幂等键 + 落库） | `MemorySaver` 自带 | 无 |

> 延迟差异不显著：一次调用里 90% 时间在模型侧，编排层是毫秒级。**框架不是性能瓶颈。**

---

## 3. 四个发现（面试可直接讲）

### 3.1 结构化参数 ≠ 合规参数

Agents SDK 最大的卖点是"模型调用函数、参数结构化"，我们不用再抠 JSON。
但它只管**形状**（angle 是 string、hooks 是 array），不管**约束**（angle 必须 10–40 字）：

```
angle_len = 68  →  ResearchBrief.model_validate 失败
```

所以 `sdk_engine.validate_brief()` 必须保留——**框架把"解析失败"消灭了，把"校验失败"留给了你。**
这条如果不亲自跑，是想不到的。

### 3.2 LangGraph 的 checkpointer 正好对上"断点续跑"

我们手写这块花了两天（租约、心跳、回收、幂等键）。
LangGraph 的 `MemorySaver` + `thread_id` 在**单进程内**解决了同样的问题，10 行代码。

但它只解决"图的状态"，不解决：
- 多进程抢任务（还是要有队列和租约）
- 产物幂等（`ARTIFACT` 落盘还是自己的事）
- 计费与预算（框架不管）

> 一句话：**框架能替你管"执行到哪了"，管不了"谁来执行"和"执行几次算一次"。**

### 3.3 MCP 的定位是"协议"，不是"框架"

把 4 个工具包成 MCP server 后，Claude Desktop / Cursor 可以直接：
提交任务 → 轮询状态 → 拿到成片路径。

写的时候踩到一个真实约束：**MCP 工具调用会超时**，所以工具里绝不能跑 90 秒的渲染，
只能"提交 + 查询"。这和我们的异步架构刚好契合——**不是我们迁就 MCP，是两者本来就该这样分**。

### 3.4 什么时候**不**用框架

- 单 agent、流程固定 → 手写 60 行循环更可控，出错时你知道每一行在干什么；
- 需要严格预算/幂等/多进程 → 框架只覆盖一半，剩下还是自己写；
- 需要"每一步都能重放" → 我们的 `events` 表比框架的 trace 更贴合业务。

**结论：编排用 LangGraph（图/检查点），Agent 能力用 SDK（工具调用），对外用 MCP（协议），
但队列、幂等、计费必须留在自己手里。**

---

## 4. 怎么跑

```powershell
# 三引擎对照（真实调用，约 20 秒）
.\.venv\Scripts\python.exe scripts\compare_engines.py --topic "为什么年轻人开始重看老动画"

# MCP server（stdio，给 Claude Desktop / Cursor 用）
.\.venv\Scripts\python.exe -m app.mcp_server
```

客户端配置见 `app/mcp_server.py` 顶部注释。

---

## 5. 已知限制

| 项 | 说明 |
|---|---|
| LangGraph 版只覆盖 RESEARCH + SCRIPT | 没有接素材/配音/渲染；它的价值是"对照"，不是"替换" |
| SDK 版没有接检索工具 | 只有 `save_brief` 一个 tool，refs 用占位 |
| MCP 没做鉴权 | 本机 stdio，天然隔离；要远程暴露必须加 token |
| 三个适配器各有一套 prompt 调用 | 复用同一份 `.md` 模板，但调用点分散，长期应收敛成一个 `agent runtime` |
