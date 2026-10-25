# 架构文档 v1：B站 AI 视频创作平台 · 任务编排 Agent 服务

> 本文档由 9 个已确认决策推导而来，不含未决策内容。
> 决策日期：D0　｜　目标交付：D3 晚　｜　配套：`SPEC.md`（范围）、`README.md`（对外）

---

## 0. 决策台账（全部已定，改动需写新 ADR）

| # | 决策 | 选择 | 主要理由 | 放弃的选项与代价 |
|---|---|---|---|---|
| D1 | 选题形态 | 全链路 7 阶段任务服务 | 覆盖 JD 四条职责 | 只做自动剪辑：工程分量不足 |
| D2 | 对外叙事 | 一个主题 → 一条能发的视频 | 产品感 + 可演示 | 纯工程骨架：没有"成品" |
| D3 | 状态机粒度 | 7 阶段 | 断点续跑可演示、一张表讲得完 | 镜头级：状态爆炸，讲不清 |
| D4 | Agent 边界 | 3 步走模型（RESEARCH / ASSETS / QC） | 可测、可缓存、成本可控 | 全自主：无法预测步数与成本 |
| D5 | 验收（DoD） | 字幕对齐<1%、镜头相关≥4/5、45–90s、无黑屏静音爆音（+标题/简介/tag 顺手做） | 全部可自动检测 | 封面图：2h+ 且易丑，砍掉 |
| D6 | 产物契约 | 每阶段一个 Pydantic 模型 + 落盘 JSON + version | 断点续跑只读一个文件 | 大 Context：改动即全链路失效 |
| D7 | 质检回环 | 只重做不合格的那一镜，最多 2 次 | 精准、省钱 | 整段重跑：成本翻倍 |
| D8 | 素材来源 | 关键镜 i2v（1–2 镜）+ 其余文生图 | 质感与成本平衡 | 素材库检索：容易图文无关 |
| D9 | 执行模型 | SQLite 队列 + 多 worker 进程 + 租约/心跳 | +1.5h 换"kill 不丢"硬演示 | Redis：多一个组件多一个失败点 |

---

## 1. 系统形状

```
┌────────────────────────────────────────────────────────────────────────┐
│ 前端（单页）   提交主题 · 轮询/SSE 进度 · 分镜卡片 · 播放器 · trace 面板   │
└──────────────────────────────┬─────────────────────────────────────────┘
                               │ POST /tasks  → 202 {task_id}（20ms）
┌──────────────────────────────▼─────────────────────────────────────────┐
│ API（FastAPI）  参数校验 · 幂等提交（Idempotency-Key）· 限流 · 任务查询   │
└──────────────────────────────┬─────────────────────────────────────────┘
                               │ INSERT tasks(status=PENDING)
┌──────────────────────────────▼─────────────────────────────────────────┐
│ SQLite（WAL）  tasks · stages · shots · artifacts · events              │
│                既是存储，也是队列（原子抢锁 = UPDATE…RETURNING）          │
└──────────────────────────────┬─────────────────────────────────────────┘
                               │ lease（原子 UPDATE）
┌──────────────────────────────▼─────────────────────────────────────────┐
│ Worker 进程 ×N（同一份代码，可开多个）                                    │
│   循环：抢任务 → 取当前 stage → 查幂等键 → 跑 → 校验 → 落产物 → 推进     │
│   心跳线程：每 10s 续租；崩溃 → 租约 60s 过期 → 任务回到 PENDING         │
│                                                                        │
│   Agent Loop（手写，4 道闸）                                            │
│   ┌──────────────────────────────────────────────────────────────────┐ │
│   │ RESEARCH ──► SCRIPT ──► STORYBOARD ──► ASSETS ──► VOICE ──►      │ │
│   │   🤖         ⚙️          ⚙️            🤖        ⚙️              │ │
│   │                                                     RENDER ──►   │ │
│   │                                                      ⚙️          │ │
│   │                                                     QC ──► DONE  │ │
│   │                                                      🤖          │ │
│   │                                         不合格 → 回 ASSETS(指定镜头)│ │
│   └──────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────┬─────────────────────────────────────────┘
                               │
        ┌──────────────────────┼──────────────────────┐
        ▼                      ▼                      ▼
   模型 API（LLM/文生图/i2v）  ffmpeg/ASR/TTS    artifacts/（产物 + trace）
```

**一次任务的完整生命周期（这条链要能背）**

1. `POST /tasks` → 校验 → 幂等键去重 → 插入 `PENDING` → 返回 `task_id`
2. 某 worker 原子抢到 → 置 `RUNNING` + `worker_id` + `lease_until=now+60s`
3. 心跳线程每 10s 续租（`UPDATE tasks SET lease_until=? WHERE id=? AND worker_id=?`）
4. 主循环取 `current_stage` → 算**幂等键** → 命中则跳过，未命中则执行
5. 阶段执行 → 校验器 → 落 `artifacts` + 更新 `stages` → `current_stage` 前进
6. `QC` 不过 → 只把不合格的 `shots` 置回 `PENDING` → 回 `ASSETS`
7. 终态 → `DONE` / `PARTIAL` / `FAILED` / `NEEDS_HUMAN`
8. worker 崩溃 → 租约过期 → 回收进程把任务置回 `PENDING`、`attempt+1`

---

## 2. 状态机（唯一的真相来源）

### 2.1 任务级

| 状态 | 含义 | 出口 |
|---|---|---|
| `PENDING` | 已入队，未被抢 | → `RUNNING` |
| `RUNNING` | 某 worker 持有租约 | → `DONE` / `FAILED` / `PARTIAL` / `NEEDS_HUMAN` / 租约过期回 `PENDING` |
| `DONE` | 全阶段通过 + QC 通过 | 终态 |
| `PARTIAL` | 有镜头降级（占位图/重做 2 次仍不过），但成片可用 | 终态，**允许演示** |
| `FAILED` | 硬失败（脚本非法 ×3、TTS 连续失败） | 终态，进死信 |
| `NEEDS_HUMAN` | 结构性问题（分镜数越界、镜头与脚本不符） | 终态，人工介入 |

> **`PARTIAL` 是设计出来的，不是兜底。** 它让"任务永远有产出"，是演示 20 条评测时成功率好看的关键——**但要诚实标注哪些是降级产出的**。

### 2.2 阶段级

`PENDING → RUNNING → DONE | FAILED | SKIPPED`

- `SKIPPED`：幂等键命中，复用上游产物（**这是"回退重跑"只重算变化部分的机制**）
- 阶段内重试：最多 3 次，指数退避 + jitter（1s / 3s / 9s）
- 阶段超时：`SCRIPT/STORYBOARD/QC` 120s，`ASSETS` 300s，`VOICE` 180s，`RENDER` 240s

### 2.3 推进规则

```
PENDING ─► RESEARCH ─► SCRIPT ─► STORYBOARD ─► ASSETS ─► VOICE ─► RENDER ─► QC
                                                  ▲                          │
                                                  └──── 仅不合格镜头 ────────┘
                                                          （最多 2 轮）

QC ── 全部通过 ──► DONE
QC ── 2 轮后仍不合格 ──► PARTIAL（该镜用占位/最佳版本，标记 degraded）
QC ── 发现"分镜与脚本语义不符" ──► NEEDS_HUMAN（3 天内不做自动回 STORYBOARD）
```

---

## 3. 七个阶段的契约（三件套）

> 每个阶段 = 输入契约 + 输出契约 + 校验器。**没有校验器的阶段不允许存在**——否则断点续跑无法判断"成功"。

| 阶段 | 谁执行 | 输入 | 输出（Pydantic） | 校验器（不通过即失败/重试） |
|---|---|---|---|---|
| **RESEARCH** | 🤖 模型 | topic, 分区 | `ResearchBrief{angle, hooks[3], refs[≥1], keywords[5]}` | refs 非空；angle 长度 10–40 字 |
| **SCRIPT** | ⚙️ 代码+模型 | brief | `Script{title, hook, segments[{text, est_sec}], total_sec}` | `45 ≤ total_sec ≤ 90`；字数/语速一致（±10%） |
| **STORYBOARD** | ⚙️ 代码 | script | `Storyboard{shots[{idx, narration, visual_prompt, motion, est_sec}]}` | `3 ≤ n ≤ 10`；`|Σest_sec − total_sec| ≤ 5%`；每镜 narration 非空 |
| **ASSETS** | 🤖 模型+工具 | storyboard | `Assets{shots[{idx, kind(image|video), path, sim_score, degraded}]}` | 每镜有产物或占位；`sim_score ≥ 0.6` 否则标记待重做 |
| **VOICE** | ⚙️ 工具 | narration 列表 | `Audio{per_shot[path, dur], srt, total_dur}` | 静音段 < 0.3s；`|total_dur − Σest| ≤ 10%` |
| **RENDER** | ⚙️ 工具 | assets + audio + srt | `Video{path, duration, size, has_audio}` | 可解码；黑帧 > 0.5s 判失败；音轨存在 |
| **QC** | 🤖 模型 | 全部产物 | `QCReport{checks[], retry_shots[], score, degraded_reason}` | 4 条 DoD 全绿才 `DONE` |

**DoD 与校验器的映射（重要，面试会问"你怎么保证能发"）**

| DoD | 由谁保证 | 怎么测 |
|---|---|---|
| 字幕对齐 <1% 错字 | `VOICE` 用 ASR 回对齐，不按字数估算 | 抽 20 句人评 + CER 计算 |
| 镜头相关 ≥4/5 | `ASSETS` 的 `sim_score` + `QC` 复核 | CLIP 相似度 + 人评抽样 20 镜 |
| 45–90s | `SCRIPT` 与 `RENDER` 双卡 | ffprobe 读实际时长 |
| 无黑屏/静音/爆音 | `RENDER` 后处理检测 | ffmpeg `blackdetect` / `silencedetect` / 峰值 >0dB |

---

## 4. 幂等与断点续跑（架构的关键机制）

**幂等键**：`idem = sha1(task_id | stage | hash(上游 artifact))`

- 上游未变 → 键不变 → 产物直接复用（`SKIPPED`）
- 上游变了（QC 换了素材）→ 键变 → 只重算受影响的阶段

**由此得到一个漂亮的性质**：

> QC 回退到 `ASSETS` 换掉第 3 镜后，`VOICE` 的输入（narration）没变 → 幂等键没变 → **配音和字幕自动复用，不重跑**。RENDER 因为素材变了才重跑。

**这就是"断点续跑"和"精准重做"用同一套机制实现的证明**——面试讲这个，比讲十个功能都值。

**checkpoint 落盘位置**：`artifacts/{task_id}/{stage}/v{n}.json` + DB 里记 `path/hash/version`

---

## 5. 数据表

```sql
tasks(id PK, topic, status, current_stage, attempt,
      worker_id, lease_until, budget_spent_cents,
      created_at, updated_at, error)

stages(task_id, stage, status, attempt, started_at, finished_at,
       idem_key, artifact_id, error, PRIMARY KEY(task_id, stage))

shots(task_id, idx, narration, visual_prompt, kind,
      asset_path, sim_score, status, retry_count,
      PRIMARY KEY(task_id, idx))          -- 镜头级状态，支撑"只重做一镜"

artifacts(id PK, task_id, stage, version, path, hash, kind, created_at)

events(id PK, task_id, ts, level, stage, type, payload_json)  -- trace
```

**设计取舍（面试会问）**：`shots` 单独建表而不是塞进 storyboard JSON —— **JSON 是"事实"，表是"状态"**。镜头要能被单独改状态、单独重跑，所以它必须是行，不是数组元素。

---

## 6. 租约 / 心跳 / 故障恢复

| 机制 | 实现 | 演示动作 |
|---|---|---|
| 原子抢锁 | `UPDATE tasks SET status='RUNNING', worker_id=?, lease_until=? WHERE id=(SELECT id FROM tasks WHERE status='PENDING' ORDER BY created_at LIMIT 1) AND status='PENDING' RETURNING id` | 开 2 个 worker，看任务不重复 |
| 心跳 | 独立线程每 10s 续租 | 观察 `lease_until` 递增 |
| 租约过期回收 | 回收线程每 15s 扫 `status='RUNNING' AND lease_until < now` → 回 `PENDING` | **`kill -9` worker，30s 内被接手** |
| 死信 | `attempt > 3` → `FAILED` + 记 `error` | 让接口持续 500，看终态 |

**默认参数**：lease 60s / 心跳 10s / 回收扫描 15s / 最大 attempt 3。

---

## 7. 目录结构

```
app/
├── main.py                 # FastAPI：POST /tasks, GET /tasks/{id}, GET /events
├── api/schemas.py          # 请求/响应
├── core/
│   ├── state_machine.py    # 阶段枚举 + 合法转换表（纯函数，好测）
│   ├── queue.py            # enqueue / lease / ack / nack / reclaim  ← 换 Redis 只改这里
│   ├── runner.py           # 取任务→取阶段→幂等→执行→校验→推进
│   └── idem.py             # 幂等键计算
├── agent/
│   ├── loop.py             # 手写循环（4 道闸）
│   └── tools/              # llm / t2i / i2v / tts / asr / render / bili_search
├── stages/                 # 七个阶段，每个：run(ctx) + validate(out) + models.py
├── worker.py               # 进程入口：心跳 + 主循环
├── db.py                   # SQLite + WAL + busy_timeout
└── settings.py
tests/                      # 工具 mock、状态机转换、loop 四闸、幂等、租约
scripts/chaos.py            # 故障注入：kill / 500 / timeout
eval/                       # 20 条 golden + run_eval.py + report.md
web/                        # 单页
artifacts/                  # 运行时产物（gitignore）
```

---

## 8. 开工顺序（先立壳，再填肉）

| 里程碑 | 内容 | 时长 | 可验证 |
|---|---|---|---|
| **M0** | `POST /tasks` → 入队 → worker 抢到 → 跑**一个假阶段**（sleep 3s）→ 落 events → 前端轮询看到"1/7" | 90 min | 壳是通的 |
| **M1** | SCRIPT + STORYBOARD（纯文本，无多模态） | 2h | 能看到合法分镜 JSON |
| **M2** | ASSETS（只文生图，串行）+ VOICE（TTS） | 2.5h | 每镜有图有音 |
| **M3** | RENDER（ffmpeg 图+音） | 2h | **能播的 mp4** |
| **M4** | 租约/心跳/回收 + 故障注入（kill/500/超时） | 2h | **kill 不丢** |
| **M5** | QC + 镜头级回退 + ASR 字幕对齐 | 2.5h | 4 条 DoD 自动判定 |
| **M6** | i2v 高光镜（并发 + 轮询 + 超时）+ 图批并发 | 2h | 质感上一档 |
| **M7** | 20 条评测 + pytest + README/ADR + 90s 视频 | 3h | 有数字 |

> **M0 的 90 分钟不要省。** 壳不通就填阶段，第 2 天会遇到"阶段都对但串不起来"，那时重构成本是现在的 10 倍。

---

## 9. 四个"边写边定"的小决策（现在不用纠结）

| # | 问题 | 我的默认建议 | 触发调整的信号 |
|---|---|---|---|
| O1 | 图文相似度怎么算 | 文生图本身就用 `visual_prompt`，相关性天然高；`sim_score` 只在"重选"时用 CLIP 排序 | 人评抽查 <4 分超过 3 例 |
| O2 | QC 回退时 VOICE 要不要重跑 | 不重跑，靠幂等键自动复用 | 旁白文本被 QC 改动 |
| O3 | 要不要 LangGraph 版 | **只移植 RESEARCH 一个阶段**，够讲"我评估过框架" | 时间充裕才全量 |
| O4 | SSE 还是轮询 | 先轮询（5 行），第 3 天换 SSE 当亮点 | 想讲"长连接/心跳"时 |

---

## 10. 还没解决的问题（写进 README 的 "Known Limitations"）

1. 长视频一致性（>90s 时人物/风格漂移）
2. 评测集仅 20 条，只够做回归，不够做结论
3. 单机 SQLite，多机需换 Redis/Postgres
4. 版权：素材全部文生图/i2v 生成，未使用他人视频
5. 无鉴权与配额隔离（demo 级）
