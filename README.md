# bili-video-agent

> B站 AI 视频创作平台 · 任务编排 Agent 服务（实习求职 demo）

输入一个中文主题 → 七阶段流水线（调研 / 脚本 / 分镜 / 素材 / 配音 / 渲染 / 质检）→ 45–90 秒成片。
任务异步执行、状态机落库、支持断点续跑与故障恢复。

**当前状态：M1 完成（文本链路）**，ASSETS 及之后仍是占位实现，见
[`docs/reports/M0-服务骨架.md`](docs/reports/M0-服务骨架.md) 与 [`docs/reports/M1-文本链路.md`](docs/reports/M1-文本链路.md)。

> 未配置 `.env` 时自动使用 `MockLLM`，全链路可离线跑通；接真模型只需填 `LLM_BASE_URL / LLM_API_KEY / LLM_MODEL`。

## 快速开始

```powershell
uv venv --python 3.11 .venv
uv pip install --python .\.venv\Scripts\python.exe fastapi uvicorn pydantic pydantic-settings pytest httpx python-dotenv

.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000   # 终端 1
.\.venv\Scripts\python.exe -m app.worker --name w1                        # 终端 2
.\.venv\Scripts\python.exe -m app.worker --name w2                        # 终端 3（可选）

start http://127.0.0.1:8000
```

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q                                  # 42 passed
.\.venv\Scripts\python.exe scripts\two_workers.py --workers 2 --tasks 3  # 并发不重复执行
```

## 架构

```
POST /tasks ─► tasks(PENDING) ─► worker lease（原子 UPDATE…RETURNING）
    ─► 7 阶段状态机 ─► 每阶段 checkpoint 落盘 ─► SSE/轮询进度 ─► mp4
```

- 设计文档：[`ARCHITECTURE.md`](ARCHITECTURE.md)
- 执行计划：[`PROJECT-PLAN.md`](PROJECT-PLAN.md)

## 目录

```
app/
  contracts.py      # 七阶段产物契约
  settings.py       # 配置收口
  db.py             # SQLite + WAL + 事务
  core/
    state_machine.py  # 阶段/状态/超时/预算
    queue.py          # enqueue / lease / heartbeat / reclaim
    runner.py         # 状态机推进 + 幂等 + 重试
    idem.py           # 幂等键
    trace.py          # 事件（trace）
  stages/           # 七个阶段（M0 为 fakes）
  api/routes.py     # HTTP 接口
  worker.py         # worker 进程
web/index.html      # 单页控制台
tests/              # 42 条
scripts/            # 并发验证 / 后续故障注入
docs/reports/       # 每阶段实现报告
```

## 里程碑

| M | 内容 | 状态 |
|---|---|---|
| M0 | 服务骨架 | ✅ |
| M1 | 文本链路（research/script/storyboard） | ✅ |
| M2 | 素材 + 配音 | ⬜ |
| M3 | 渲染出片 | ⬜ |
| M4 | 分布式加固（租约/回收/故障注入） | ⬜ |
| M5 | 质检 + 回退 + 字幕对齐 | ⬜ |
| M6 | i2v 高光镜 + 并发 | ⬜ |
| M7 | 评测 + 测试 + 文档 | ⬜ |
