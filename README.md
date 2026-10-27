# bili-video-agent

> 输入一个中文主题 → 七阶段流水线 → 45 秒可交付短片。
> 异步任务、状态机落库、**杀进程还能续跑**、质检回退只重做坏镜头。
>
> *An async, resumable multi-modal agent service that turns a topic into a short video.*

[![tests](https://img.shields.io/badge/tests-101%20passed-3ddc97)](#工程实践)
[![eval](https://img.shields.io/badge/eval-15%2F15%20real-3ddc97)](#评测)

![成片抽帧](docs/assets/real-frame.png)

---

## 关键数字

| 指标 | 值 | 口径 |
|---|---|---|
| 单元测试 | **96 条全绿**（~2 分钟） | `pytest -q` |
| 评测成功率 | **15/15 DONE**（真实模型） | DeepSeek + CogView，720p draft |
| 成片时长 | 50.6–61.8s（中位 53.5） | 真实模型；Mock 批恒为 45.0s |
| 单条耗时 | 中位 **144.6s** / P95 173.1s | 720p；1080p 约 180–220s |
| 画面运动量 | 中位 20.67（阈值 ≥1.0） | 首镜两帧灰度差 |
| QC 均分 | 95/100 | 9 项检查 |
| 故障注入 | **3/3 PASS** | kill -9 / 上游 500 / 超时 |
| 代码量 | ~5.9k 行 Python | 含测试与脚本 |
| **译制链路**（中→英） | 53.6s → 53.6s，13 句，漂移 0 | 最大加速 1.32x，质检 5/5 |

> 真实模型那批成片有「AI生成」水印、画面为文生图占位级别；
> 另有一批 20 条 Mock 压测（文本恒定、时长全为 45.0s），只用于验证流水线稳定性。
> 详见 [诚实边界](#诚实边界)。

---

## 它是什么

一个**任务编排服务**，不是一个网页玩具：

```
提交主题 → 202 {task_id} → worker 抢任务 → 7 阶段 → mp4 + 字幕 + trace
```

- **异步**：HTTP 只登记，worker 执行；视频生成要几分钟，同步必超时
- **状态机落库**：`current_stage` 在库里，进程只是执行器
- **租约 + 心跳**：worker 死了，任务被另一个进程接手，从断点续跑
- **幂等**：重复执行不重复扣费、不重复出产物
- **质检回退**：坏镜头单独重做，配音零重算

## 它不是什么

| 不做 | 原因 |
|---|---|
| 长视频（>90s）、多镜头转场 | 一致性没保障 |
| 用户/鉴权/多租户 | demo 级，明确写出 |
| 多机部署 | 单机 SQLite，队列接口已抽好，换 Redis 只改一个文件 |
| 自动发布到 B站 | 无授权 |

---

## 架构

```
                    ┌──────────────────────────────────────────────┐
   POST /tasks ───► │  API（FastAPI） 幂等提交 · 202 · 任务查询        │
                    └───────────────┬──────────────────────────────┘
                                    │ INSERT status=PENDING
                    ┌───────────────▼──────────────────────────────┐
                    │  SQLite（WAL） tasks/stages/shots/artifacts/events │
                    │  既是存储，也是队列                                │
                    └───────────────┬──────────────────────────────┘
                                    │ 原子抢锁 UPDATE…RETURNING
                    ┌───────────────▼──────────────────────────────┐
                    │  Worker ×N：抢任务 → 心跳续租 → 跑阶段 → 校验     │
                    │                                                │
                    │  RESEARCH🤖 → SCRIPT⚙️ → STORYBOARD⚙️ →         │
                    │  ASSETS🤖 → VOICE⚙️ → RENDER⚙️ → QC🤖           │
                    │                                    └─坏镜→回 ASSETS │
                    └────────────────────────────────────────────────┘
```

**7 个阶段里只有 3 个用模型**（调研 / 素材 / 质检），其余是确定性代码。
理由：判断归模型，算术和编解码归代码——模型只会给时长引入方差。

| 阶段 | 输入 → 输出 | 谁执行 |
|---|---|---|
| RESEARCH | 主题 → 角度/钩子/参考/关键词 | 🤖 |
| SCRIPT | 简报 → 标题/分段/时长 | ⚙️ 模型写内容，代码算时长 |
| STORYBOARD | 脚本 → 镜头（旁白/画面/景别/时长） | ⚙️ 纯代码切分 |
| ASSETS | 分镜 → 图片（前 N 镜升级视频） | 🤖 批量改写画面 + 并发出图 |
| VOICE | 旁白 → 配音 + SRT | ⚙️ edge-tts，实测时长 |
| RENDER | 素材+配音 → mp4 | ⚙️ ffmpeg |
| QC | 全产物 → DONE / PARTIAL / RETRY | 🤖 路由器 |

---

## 快速开始

```powershell
uv venv --python 3.11 .venv
uv pip install --python .\.venv\Scripts\python.exe -r requirements.txt

copy .env.example .env      # 可选：填 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL；不填走 Mock

.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000   # 终端 1
.\.venv\Scripts\python.exe -m app.worker --name w1               # 终端 2
.\.venv\Scripts\python.exe -m app.worker --name w2               # 终端 3（可选）
```

打开 <http://127.0.0.1:8000>，输入主题 → 看进度 + trace → 播放成片。

**一键验证**：

```powershell
.\scripts\verify.ps1            # 92 条测试 + 三种故障注入
.\scripts\verify.ps1 -WithEval  # 再加 20 条评测（约 10 分钟）
```

---

## 三个关键机制

### 1. 原子抢锁（不需要 Redis）

```sql
UPDATE tasks SET status='RUNNING', worker_id=?, lease_until=?
WHERE id=(SELECT id FROM tasks WHERE status='PENDING' ORDER BY created_at LIMIT 1)
  AND status='PENDING'
RETURNING *
```

单条语句即原子。worker 心跳续租，租约过期由回收线程打回 `PENDING`。

### 2. 幂等键 = 断点续跑 + 精准重做，同一套机制

```
idem = sha1(task_id | stage | 该阶段的真实上游 | 配置版本)
```

关键在「真实上游」：`VOICE` 依赖 `STORYBOARD`（旁白），**不依赖 `ASSETS`（素材）**。
所以 QC 换掉一镜素材时：ASSETS 键变 → 重做；VOICE 键不变 → 跳过。
这就是「只重做坏镜头、配音零重算」全部的实现。

### 3. QC 是路由器，不是打分器

```
有坏镜 && 无结构性失败 && 轮次未用尽 → RETRY（只标那几镜）
否则有 block 失败                    → PARTIAL（写清原因）
否则                                 → DONE
```

能自动判的只有：时长、黑帧/静音/削波、字幕覆盖、画面是否纯色。
「镜头与旁白是否相关」判不了——所以它只挡「技术上不能发」，不假装能判「好不好看」。

---

## 目录结构

```
app/
├── main.py              # FastAPI 入口（工厂模式，便于测试注入）
├── worker.py            # worker 进程：抢任务 + 心跳 + 推进
├── db.py                # SQLite(WAL) 连接 / 建表 / 轻量迁移
├── contracts.py         # 七阶段产物契约（pydantic）
├── errors.py            # 8 个错误码 + AppError
├── core/
│   ├── queue.py         # enqueue / lease / heartbeat / reclaim  ← 换 Redis 只改这里
│   ├── runner.py        # 状态机推进 + 幂等 + 重试 + QC 回退
│   ├── artifacts.py     # 产物落盘与登记
│   ├── state_machine.py # 阶段、超时、预算、STAGE_INPUTS
│   └── trace.py         # 10 类事件
├── stages/              # 七个阶段，每个 = 输入契约 + 输出契约 + 校验器
├── agent/
│   ├── llm.py           # OpenAI 兼容客户端 + MockLLM
│   ├── graph_engine.py  # LangGraph 版编排（对照）
│   ├── sdk_engine.py    # OpenAI Agents SDK 版（对照）
│   └── tools/           # 图像 / TTS / ffmpeg / i2v
├── mcp_server.py        # MCP server：4 个工具
├── dub/                 # 译制流水线：asr / translate / tts / align / subs / qc
└── prompts/             # prompt 外置为 .md
tests/                   # 101 条
scripts/                 # 故障注入 / 批量运行 / 三引擎对照
eval/                    # 20 条 golden set + 评测脚本
docs/reports/            # 每个里程碑的实现报告（含 bug 复盘）
```

## 第二条流水线：视频译制（中 → 英）

同一条平台长出第二条链路：给它一条**已带中文字幕和配音**的视频，输出英文配音 + 英文字幕版。

```
video → 抽音轨 → ASR(whisper 本地) → 翻译(LLM, 整段一次)
      → 英文 TTS → 时间轴对齐(atempo + adelay) → 盖原字幕 + 换音轨 + 烧字幕 → 质检
```

```powershell
.\.venv\Scripts\python.exe scripts\dub.py --video "dev_out\videos\01_xxx.mp4"
```

实测：53.63s → 53.63s，13 句，最大加速 1.32x，尾部漂移 0.0s，质检 5/5 通过。

三个关键设计（详见 [`docs/reports/DUB-译制流水线.md`](docs/reports/DUB-译制流水线.md)）：

1. **翻译要带"字符预算"**：英文比中文长 30%–50%，不限制长度就会把时间轴拖爆。
   prompt 里写 `≤ sec×14 字符`，代码里再校验并带着"哪句超了多少"重试。
2. **时间轴用"静音底轨 + adelay + amix"**，不是 concat——concat 会吃掉句子之间的停顿。
3. **原片字幕是烧死的**：先用 OpenCV 找字幕带并覆盖，再烧新字幕；
   否则双语叠在一起（这是第一版最明显的 bug）。

## 三种 Agent 框架都接了一遍

同一主题、同一套 prompt、同一套校验器，只换编排层（`scripts/compare_engines.py`）：

| | 原生状态机 | LangGraph | OpenAI Agents SDK |
|---|---|---|---|
| 编排代码 | 238 行 | **88 行** | 52 行 |
| 耗时 | 5.9s | 6.2s | 3.6–6.9s |
| 断点续跑 | 自己写（幂等键 + 落库） | `MemorySaver` 自带 | 无 |
| 产出 | JSON 文本 → 自己解析 | 图状态 dict | **tool 参数，直接是对象** |

MCP 另起一个 server，把流水线暴露成 4 个工具（提交/查状态/列任务/取成片）：

```powershell
.\.venv\Scripts\python.exe -m app.mcp_server    # stdio，可挂到 Claude Desktop / Cursor
```

三个结论（详见 [`docs/reports/AGENT-框架对照.md`](docs/reports/AGENT-框架对照.md)）：

1. **结构化参数 ≠ 合规参数**：SDK 的 tool call 保证 angle 是字符串，但不保证它 ≤40 字（实测 68 字），契约校验不能省。
2. **框架管"执行到哪了"，管不了"谁来执行"和"执行几次算一次"**——队列、租约、幂等、计费仍在自己手里。
3. **框架不是性能瓶颈**：90% 时间在模型侧，编排层是毫秒级。

---

## 评测

| 口径 | 样本 | 成功率 | 时长中位 | 单条耗时 | motion 中位 | QC |
|---|---|---|---|---|---|---|
| **真实模型**（DeepSeek + CogView，720p） | 15 | **100%** | 53.5s（50.6–61.8） | 144.6s | 20.67 | 95 |
| Mock（流水线压测，720p） | 20 | 100% | 45.0s（恒定） | 28.1s | 6.03 | 95 |

- 真实模型这组：`eval/report-real.md`
- Mock 压测：`eval/report.md`

两条结论值得注意：

1. **真实模型下时长才有分布**（50.6–61.8s），Mock 全是 45.0s——因为 Mock 对任何主题都返回同一份文案。
2. **单条耗时从 28s 涨到 145s**，其中真实出图 ~50s、1080p/720p 编码各占大头；评测用 720p draft 才跑得动 15 条。

**评测里特意加了一条 `motion_score`**：抽两帧算灰度差。
因为 M3–M6 之间有个 bug——所有技术指标全绿，但画面其实是静止的（详见
[`docs/reports/M6-图生视频.md`](docs/reports/M6-图生视频.md)）。**指标绿 ≠ 内容是活的。**

---

## 产物存在哪里

每条任务一个目录，**不覆盖、可追溯**：

```
artifacts/<task_id>/
├── RESEARCH/v1/research.json      # 选题简报
├── SCRIPT/v1/script.json          # 标题 / 分段 / 时长
├── STORYBOARD/v1/storyboard.json  # 镜头表
├── ASSETS/
│   ├── v1/assets.json             # 每镜素材清单（provider / degraded）
│   └── media/shot_00.png ...      # 真实生成图（或占位图）
├── VOICE/
│   ├── v1/voice.json              # 每镜实测时长
│   ├── media/shot_00.mp3 ...      # 配音
│   └── audio.srt                  # 字幕
├── RENDER/
│   ├── v1/render.json             # 成片元数据（时长/黑帧/静音/峰值）
│   ├── final.mp4                  # ★ 最终成片（还有一份在工作目录 work/final.mp4）
│   └── work/clip_*.mp4            # 每镜片段，便于排查"哪一镜有问题"
└── QC/v1/qc.json                  # 9 项检查 + 决策
```

- **成片路径固定为** `artifacts/<task_id>/RENDER/final.mp4`
- `task_id` 在提交任务时的 202 响应里返回，也可在首页进度条或 `GET /tasks` 查到
- 批量运行时想快速找片子：`scripts/run_batch.py` 会把副本放到 `dev_out/videos/`



## 工程实践

| 项 | 做法 |
|---|---|
| 错误码 | 只允许 8 个（`E_VALIDATION/E_TOOL_TIMEOUT/E_TOOL_UPSTREAM/E_TOOL_EMPTY/E_BUDGET/E_LOOP/E_RENDER/E_INTERNAL`），`except Exception` 只出现在 worker 最外层 |
| 可观测 | `events` 表 10 类事件，每步都有耗时/token/成本；前端可回放 |
| 测试 | 92 条：契约、状态机、队列并发（4 线程抢 5 任务）、幂等、租约回收、渲染、回退、i2v 协议 |
| 失败注入 | `scripts/chaos.py` 真进程 `kill -9` + HTTP stub 500 / 超时 |
| 技术债 | [`docs/debt.md`](docs/debt.md) 18 条，每条写明代价与偿还计划 |
| 决策记录 | [`docs/adr/`](docs/adr/) |

---

## 诚实边界

1. **评测口径**：15 条真实模型 + 20 条 Mock 压测。真实画面来自 CogView 免费档，右下角有「AI生成」水印。
   接入付费图像模型后画质会变，数字需要重跑。
2. **画面是占位图**（无 `IMG_API_KEY`）。配 key 后走文生图，逻辑已实现并测试。
3. **i2v 只验证了协议**（无厂商 key），真机走 `kenburns` 回落。
4. **字幕是一镜一句**，没有词级时间轴（`docs/debt.md` D-15）。
5. **「镜头与旁白相关」没有自动判定**，QC 只挡纯色/黑图/坏文件（D-16）。

---

## 文档

| 文档 | 内容 |
|---|---|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | 架构、状态机、契约 |
| [`PROJECT-PLAN.md`](PROJECT-PLAN.md) | 八里程碑执行计划与验收标准 |
| [`docs/reports/`](docs/reports/) | M0–M7 每阶段实现报告（含 bug 复盘） |
| [`docs/debt.md`](docs/debt.md) | 技术债登记 |
| [`docs/demo-script.md`](docs/demo-script.md) | 90 秒演示脚本 |
| [`eval/report.md`](eval/report.md) | 评测结果 |
