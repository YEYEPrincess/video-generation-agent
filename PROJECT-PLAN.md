# 项目执行计划书 v1

**项目**：B站 AI 视频创作平台 · 任务编排 Agent 服务（demo）
**依据**：`ARCHITECTURE.md`（9 个已确认决策）、`SPEC.md`（范围）
**周期**：D0 晚 ～ D3 晚，17.5 小时净开发 + 6 小时缓冲
**文档性质**：可执行。每完成一个里程碑，必须跑通该节的「校验标准」才能进下一个。

---

## 0. 怎么用这份文档

三条规则，违反任何一条都会在第 2 天付出代价：

1. **契约先行**：先写 `app/contracts.py`（第 4.6 节），再写 `tests/test_contracts.py` 的 golden 样例，**最后**才写实现。顺序反了，你会在第 2 天改 7 个文件。
2. **每个里程碑只有一个验收句**。验收句不通过，不加新功能，只修它。
3. **超时即降级**：任何子任务超过时间盒 30 分钟，立刻执行该节的「降级预案」，把问题记进 `docs/debt.md`，继续往下走。**未完成的降级项比未开始的完美项值钱。**

---

## 1. 项目目标与非目标

### 1.1 目标（验收句，D3 晚逐条判定）

> 输入一个中文主题，系统在 3 分钟内产出**一条 45–90 秒、字幕对齐、镜头与旁白语义相关、无黑屏静音爆音的 mp4**；过程中可随时 `kill -9` 任一 worker，任务在 30 秒内被另一进程接手并从断点续跑；全部行为可在 trace 面板中回放。

### 1.2 非目标（写进 README，防止面试官误判）

| 不做 | 原因 |
|---|---|
| 封面图生成 | 排版 + 中文字体 ≥2h，收益低 |
| 长视频（>90s）、多镜头转场 | 一致性没保障，是 Known Limitation |
| 用户体系、鉴权、多租户 | demo 级，明确写出 |
| 多机部署 / Redis / K8s | 单机 SQLite 足够演示，架构上留了替换点 |
| 自动发布到 B站 | 无授权，且与"能发"无关 |

### 1.3 最终验收总表

| # | 验收项 | 判定方式 | 门槛 |
|---|---|---|---|
| A1 | 端到端出片 | 20 条主题批量跑 | 成功率 ≥ 80% |
| A2 | 时长 | `ffprobe` 读实际时长 | 45–90s，全部命中 |
| A3 | 字幕对齐 | ASR 回读 + 人评 20 句 | CER < 1% |
| A4 | 镜头相关性 | CLIP 相似度 + 人评 20 镜 | 均值 ≥ 4/5 |
| A5 | 无黑屏/静音/爆音 | `blackdetect` / `silencedetect` / `astats` | 0 命中 |
| A6 | 故障恢复 | `scripts/chaos.py` 三种故障 | 3/3 恢复，任务 0 丢失 |
| A7 | 工程化 | `pytest` + 覆盖率 | ≥ 60 条用例，全绿，覆盖率 ≥ 60% |

---

## 2. 里程碑总览

| M | 名称 | 时长 | 依赖 | 一句话验收 | 日程 |
|---|---|---|---|---|---|
| **M0** | 服务骨架（壳） | 90 min | — | 提交任务，另一个进程跑完一个假阶段，前端看到 1/7 | D1 上午 |
| **M1** | 文本链路 | 120 min | M0 | 给主题，产出**通过 schema 校验**的脚本 + 分镜 JSON | D1 上午 |
| **M2** | 素材 + 配音 | 150 min | M1 | 每镜有图、有音，落盘可见 | D1 下午 |
| **M3** | 渲染出片 | 120 min | M2 | **第一条能播的 mp4** | D1 晚 |
| **M4** | 分布式加固 | 120 min | M3 | `kill -9` 后 30s 内被接手，断点续跑 | D2 上午 |
| **M5** | 质检 + 回退 + 字幕 | 150 min | M4 | 4 条 DoD 自动判定，只重做坏镜 | D2 下午 |
| **M6** | i2v 高光镜 + 并发 | 120 min | M5 | 视频镜与图片镜并发跑，各自超时 | D2 晚 |
| **M7** | 评测 + 测试 + 文档 | 180 min | M6 | 7 项验收全绿 + 90s 演示视频 | D3 |

```
D1: M0 ── M1 ── M2 ── M3            （目标：一条能播的片子）
D2: M4 ── M5 ── M6                  （目标：杀不死 + 能自检）
D3: M7 + 缓冲 + 简历/话术            （目标：有数字、能讲）
```

**缓冲 6 小时怎么分**：D1 尾 1h、D2 尾 1h、D3 尾 4h。**不要在 D1 用掉缓冲**。

---

## 3. 全局契约（贯穿所有阶段，先定死再写码）

### 3.1 枚举

```python
class Stage(str, Enum):
    RESEARCH = "RESEARCH"      # 🤖 模型
    SCRIPT = "SCRIPT"          # ⚙️ 代码
    STORYBOARD = "STORYBOARD"  # ⚙️ 代码
    ASSETS = "ASSETS"          # 🤖 模型 + 工具
    VOICE = "VOICE"            # ⚙️ 工具
    RENDER = "RENDER"          # ⚙️ 工具
    QC = "QC"                  # 🤖 模型

STAGE_ORDER = [RESEARCH, SCRIPT, STORYBOARD, ASSETS, VOICE, RENDER, QC]

class TaskStatus(str, Enum):
    PENDING = "PENDING"; RUNNING = "RUNNING"; DONE = "DONE"
    PARTIAL = "PARTIAL"; FAILED = "FAILED"; NEEDS_HUMAN = "NEEDS_HUMAN"

class StageStatus(str, Enum):
    PENDING = "PENDING"; RUNNING = "RUNNING"; DONE = "DONE"
    FAILED = "FAILED"; SKIPPED = "SKIPPED"     # SKIPPED = 幂等命中复用
```

**超时与预算（每阶段）**

| Stage | timeout | 重试 | 预算上限 |
|---|---|---|---|
| RESEARCH | 120s | 2 | 5¢ |
| SCRIPT | 120s | 2 | 8¢ |
| STORYBOARD | 90s | 2 | 5¢ |
| ASSETS | 300s | 1（工具级 2） | 45¢ |
| VOICE | 180s | 2 | 5¢ |
| RENDER | 240s | 1 | 0 |
| QC | 120s | 1 | 8¢ |
| **任务总计** | 20 min | — | **80¢** |

### 3.2 错误码（唯一分类法，别在代码里随手 raise）

| 错误码 | 触发 | 可重试 | 处理 |
|---|---|---|---|
| `E_VALIDATION` | 输出不满足 schema / 校验器 | 是（≤2） | 把错误回灌模型重生成 |
| `E_TOOL_TIMEOUT` | 工具超时 | 是（≤2） | 退避重试 → 仍失败则降级 |
| `E_TOOL_UPSTREAM` | 5xx / 429 / 网络 | 是（≤3，backoff） | 指数退避 + jitter |
| `E_TOOL_EMPTY` | 结果为空 | 是（换 query） | 换关键词重试 1 次 → 占位 |
| `E_BUDGET` | 超预算 | 否 | 立即终止 → `PARTIAL` |
| `E_LOOP` | 步数/重复动作/无进展 | 否 | 终止 → `PARTIAL` |
| `E_RENDER` | ffmpeg 非 0 退出 | 是（1） | 重试 → 降级为纯图 |
| `E_INTERNAL` | 其他 | 否 | → `FAILED`，记 trace |

**规则**：所有异常最终必须归到上表某一项；`except Exception` 只允许出现在 worker 最外层（用于落 `FAILED` 并释放租约）。

### 3.3 幂等键与产物路径

```
idem_key = sha1(f"{task_id}|{stage}|{inputs_hash}|{STAGE_CONFIG_VERSION[stage]}")[:16]
inputs_hash = sha1(canonical_json(上游产物, sort_keys=True, 剔除 volatile 字段))
```

- `STAGE_CONFIG_VERSION`：每阶段一个常量，**改了 prompt / 模型 / 参数就 +1** → 全量重算（这条是专业细节，面试可讲）
- 产物路径：`artifacts/{task_id}/{stage}/v{version}/{filename}`
- DB 存**相对路径**（可迁移），`hash` 存文件内容 sha1（大文件流式算）
- 命中幂等即 `StageStatus.SKIPPED`，不重复扣费

### 3.4 事件（trace）schema

```json
{"ts": "2026-10-25T16:00:00.123Z", "task_id": "t_01", "stage": "ASSETS",
 "level": "info", "type": "tool_call", "duration_ms": 812,
 "tokens": null, "cost_cents": 0,
 "payload": {"tool": "t2i", "args_digest": "…", "ok": true}}
```

`type` 枚举固定 10 个：`state_transition / stage_start / stage_done / stage_skip / tool_call / tool_result / llm_call / validation_fail / retry / lease`。
**payload 只放可序列化的摘要**，不放全量产物（避免 events 表爆炸）。

### 3.5 配置、密钥、预算

- 全部走环境变量，`app/settings.py` 用 pydantic-settings 收口；`.env` 进 `.gitignore`
- 必须存在的变量：`LLM_BASE_URL / LLM_API_KEY / LLM_MODEL / IMG_API_KEY / VIDEO_API_KEY / BUDGET_CENTS=80`
- **禁止在代码里出现任何 `sk-` 字面量**；提交前 `git grep -n "sk-"` 必须为空
- 预算记账：每次工具/模型调用后 `tasks.budget_spent_cents += cost`，超限抛 `E_BUDGET`

### 3.6 代码规范（求职 demo 的门面）

| 项 | 规定 |
|---|---|
| 类型 | 所有函数签名带类型注解，`mypy --ignore-missing-imports` 无 error |
| 日志 | 一律 `logging`，禁止裸 `print`（除了 CLI 入口） |
| 异常 | 只用 3.2 的错误码，自定义 `AppError(code, msg, retryable)` |
| 文件大小 | 单文件 ≤ 300 行，超了拆 |
| Prompt | 全部放 `app/prompts/*.md`，不在代码里拼长字符串 |
| 提交 | `feat(M2): assets stage with t2i + placeholder fallback`，一个 M 至少 3 个 commit |

---

## 4. 数据模型（契约，先写这个文件）

```python
# app/contracts.py —— 纯数据，不含 IO，不含业务逻辑

class Ref(BaseModel):
    title: str = Field(max_length=60)
    ref: str = Field(max_length=120)           # bvid 或 url
    why: str = Field(max_length=40)

class ResearchBrief(BaseModel):
    version: Literal[1] = 1
    partition: str
    angle: str = Field(min_length=10, max_length=40)
    hooks: list[str] = Field(min_length=3, max_length=3)
    refs: list[Ref] = Field(min_length=1, max_length=5)
    keywords: list[str] = Field(min_length=3, max_length=6)

class Segment(BaseModel):
    idx: int
    text: str = Field(min_length=4, max_length=120)
    est_sec: float = Field(gt=0, le=30)

class Script(BaseModel):
    version: Literal[1] = 1
    title: str = Field(max_length=30)
    hook: str = Field(max_length=40)
    segments: list[Segment] = Field(min_length=3, max_length=10)
    total_sec: float
    tts_rate: float = 4.2                      # 字/秒，实测校准

class Shot(BaseModel):
    idx: int
    segment_idx: int                           # 与 Script.segments 的对应
    narration: str = Field(min_length=4, max_length=120)
    visual_prompt: str = Field(max_length=120)  # 主体+风格+构图
    motion: Literal["none","kenburns_in","kenburns_out","pan_lr","i2v"] = "kenburns_in"
    kind_hint: Literal["image","video"] = "image"
    est_sec: float = Field(gt=0, le=20)

class Storyboard(BaseModel):
    version: Literal[1] = 1
    shots: list[Shot] = Field(min_length=3, max_length=10)

class AssetShot(BaseModel):
    idx: int
    kind: Literal["image","video"]
    path: str
    width: int; height: int
    sim_score: float | None = None
    degraded: bool = False
    provider: str
    cost_cents: int = 0
    attempts: int = 1

class Assets(BaseModel):
    version: Literal[1] = 1
    shots: list[AssetShot]
    total_cost_cents: int
    degraded_count: int

class AudioShot(BaseModel):
    idx: int; path: str; dur_sec: float

class Audio(BaseModel):
    version: Literal[1] = 1
    per_shot: list[AudioShot]
    srt_path: str
    total_dur: float
    voice: str
    alignment: Literal["asr","estimate"]

class Video(BaseModel):
    version: Literal[1] = 1
    path: str
    duration_sec: float
    size_bytes: int
    width: int; height: int; fps: int
    has_audio: bool
    black_segments: int = 0
    silence_segments: int = 0
    peak_db: float = -99.0

class Check(BaseModel):
    name: str
    passed: bool
    detail: str = ""
    severity: Literal["block","warn"] = "block"

class QCReport(BaseModel):
    version: Literal[1] = 1
    checks: list[Check]
    retry_shots: list[int] = []
    score: int = 0
    degraded_reason: str | None = None
    decided: Literal["DONE","PARTIAL","RETRY","NEEDS_HUMAN"] = "DONE"
```

**校验器是阶段的独立函数**：`def validate(out: Script) -> list[Check]`，返回 `block` 未过即失败。不写在 model 里——**数据和规则分离，才好在评测时单独替换阈值**。

---

## 5. 里程碑详表

### M0 · 服务骨架（90 min）

**验收句**：`curl -X POST /tasks` 返回 `task_id`；另一个进程把它推进到 `RUNNING` 并跑完一个 3 秒的假阶段；前端轮询能看到 `1/7`。

**前置**：无。**这一步不写任何业务逻辑。**

| 任务 | 时长 | 产出 |
|---|---|---|
| `db.py`：建表 + WAL + `busy_timeout=5000` | 20 min | 5 张表可读写 |
| `queue.py`：`enqueue / lease / ack / nack / reclaim` | 25 min | 原子抢锁单测通过 |
| `worker.py`：主循环 + 心跳线程 | 20 min | 能抢任务、能续租 |
| `main.py`：`POST /tasks`、`GET /tasks/{id}`、`GET /tasks/{id}/events` | 15 min | 3 个接口通 |
| 前端：一个输入框 + 轮询进度条 | 10 min | 能看到 1/7 |

**输入**：`{"topic": str, "partition"?: str}`
**输出**：`202 {"task_id": "t_xxx", "status": "PENDING"}`；DB `tasks` 1 行 + `stages` 7 行（均 `PENDING`）

**校验标准**（全部要绿）

```bash
curl -s -XPOST localhost:8000/tasks -d '{"topic":"测试"}' -H 'content-type: application/json' | jq -e .task_id
# 30s 内
curl -s localhost:8000/tasks/$ID | jq -e '.status=="RUNNING" or .status=="DONE"'
sqlite3 app.db "select count(*) from events where type='state_transition'"   # >= 2
# 并发安全
python scripts/two_workers.py   # 两个 worker 抢 1 个任务，只允许一个成功
```

**边界条件**

| 场景 | 期望 |
|---|---|
| 空 topic / >200 字 | 422，不建任务 |
| 重复提交同一 `Idempotency-Key` | 返回**同一个** `task_id`，不重复入队 |
| 并发 10 个 POST | 10 个任务全部入队，无锁错误 |
| worker 启动时队列为空 | 空转 1s，不 busy loop |
| 前端轮询不存在的 id | 404，不 500 |

**降级预案**：抢锁写不出来 → 先用「`BEGIN IMMEDIATE` + 单 worker」，把多 worker 推到 M4。**壳必须是通的。**

**坑**：SQLite 默认阻塞 → 必须 `PRAGMA journal_mode=WAL` + `busy_timeout`；时间统一 UTC 存 `TEXT ISO8601`。

---

### M1 · 文本链路（120 min）

**验收句**：给一个主题，产出的 `ResearchBrief / Script / Storyboard` **全部通过 pydantic 校验**，且 `total_sec ∈ [45,90]`、`3 ≤ 镜数 ≤ 10`。

**前置**：M0 绿。

| 任务 | 时长 | 产出 |
|---|---|---|
| `prompts/research.md`、`script.md`、`storyboard.md` | 30 min | 3 个 prompt 文件 |
| `stages/research.py` + 校验器 | 25 min | Brief 落盘 |
| `stages/script.py`：LLM 生成 → 校验 → **失败回灌重试 2 次** | 30 min | Script 落盘 |
| `stages/storyboard.py`：**纯代码**按句切分（不调模型） | 25 min | Storyboard 落盘 |
| golden 样例 3 条写进 `tests/fixtures/` | 10 min | 契约测试 |

**输入**：`topic`（M0 落库）→ RESEARCH 产出 `ResearchBrief` → SCRIPT 吃 Brief

**输出**：
- `artifacts/{task_id}/RESEARCH/v1/brief.json`
- `artifacts/{task_id}/SCRIPT/v1/script.json`
- `artifacts/{task_id}/STORYBOARD/v1/storyboard.json`
- DB：3 行 `stages` 置 `DONE`，`tasks.current_stage` 前进到 `ASSETS`

**校验标准**

| # | 断言 |
|---|---|
| 1 | `len(brief.refs) >= 1`，`len(brief.hooks) == 3` |
| 2 | `45 <= script.total_sec <= 90` |
| 3 | `3 <= len(storyboard.shots) <= 10` |
| 4 | `abs(sum(shot.est_sec) - script.total_sec) / script.total_sec <= 0.05` |
| 5 | 每个 `shot.segment_idx` 都能在 `script.segments` 找到 |
| 6 | 灌一个坏 JSON（缺 `hooks`）→ 触发 `E_VALIDATION` → 重试 → 第 3 次失败 → `FAILED` |

**边界条件**

| 场景 | 期望 |
|---|---|
| 模型返回带 ```json 包裹 | 剥壳后解析，成功 |
| 模型中文长度超标（`title` 35 字） | 校验失败 → 回灌"标题 ≤30 字"重生成 |
| 主题极冷门（无 refs） | `E_TOOL_EMPTY` → 用泛化关键词重试 1 次 → 仍空则 `refs=[]` + `warn`（不 block） |
| 主题含敏感词 | 模型拒答 → `NEEDS_HUMAN`，不重试 |
| 分镜切出 12 镜 | 合并相邻短镜至 ≤10，记录 `warn` 事件 |

**降级预案**：LLM 出 JSON 不稳定 → 改用 **function calling 强制 schema**（比 prompt 里写"请输出 JSON"可靠一个数量级）。

**面试可讲**：为什么 STORYBOARD 走代码而不是模型——**切分有确定算法（按句 + 时长估算），模型只会引入方差**。

---

### M2 · 素材 + 配音（150 min）

**验收句**：7 个镜头每镜都有图（或占位）和一条配音，文件落盘、可播放。

**前置**：M1 绿。

| 任务 | 时长 | 产出 |
|---|---|---|
| `tools/t2i.py`：文生图 + 超时 + 重试 + 落盘 | 30 min | 单镜出图 |
| `assets` 阶段：逐镜生成 + **占位降级**（纯色+大字） | 30 min | `Assets` 完整 |
| `tools/tts.py`：edge-tts 按镜生成 + 时长探测 | 25 min | 每镜 mp3 |
| `voice` 阶段：拼接 + 首版 srt（按字数估算） | 25 min | `Audio` |
| 失败注入：mock t2i 500 | 20 min | 走占位分支 |
| 产物校验（尺寸、可解码） | 20 min | `sim_score` 先留 `None` |

**输入**：`Storyboard`（7 镜）
**输出**：`assets/{idx}.png`、`audio/{idx}.mp3`、`audio.srt`、`assets.json`、`audio.json`

**校验标准**

| # | 断言 |
|---|---|
| 1 | `len(assets.shots) == len(storyboard.shots)` |
| 2 | 每镜 `path` 存在且 `PIL.Image.open()` 成功，`width>=720` |
| 3 | 每镜 `audio.dur_sec > 0`，`ffprobe` 可读 |
| 4 | mock 500 后 `degraded=true`，任务**不失败** |
| 5 | `sum(audio.dur_sec)` 与 `sum(est_sec)` 偏差 ≤ 15% |

**边界条件**

| 场景 | 期望 |
|---|---|
| 文生图返回 1024×1024 方图 | 统一裁/补到 16:9（`1080×608` 或 `1920×1080`） |
| t2i 429 限流 | backoff 2/4/8s，第 3 次仍失败 → 占位 |
| visual_prompt 为空或含敏感词 | 用 `narration` 前 20 字兜底 prompt |
| TTS 某镜失败 | 重试 1 次 → 该镜静音 + `warn`；**不阻断** |
| 并发 5 镜同时请求 | 信号量限 3，防打爆配额 |

**降级预案**：t2i 全部失败 → 切「纯图文卡片」（PIL 画大字 + 渐变底），保证 M3 能出片。

---

### M3 · 渲染出片（120 min）

**验收句**：`ffmpeg` 产出一条能播、有声音、有字幕的 mp4，时长 45–90s。

**前置**：M2 绿。

| 任务 | 时长 | 产出 |
|---|---|---|
| `tools/render.py`：图片 → 每镜片段（`zoompan` 缓慢推近） | 40 min | 每镜 mp4 |
| 拼接 + 混音 + 字幕烧录（先烧硬字幕） | 30 min | 成片 |
| 后处理检测：`blackdetect` / `silencedetect` / `astats` | 25 min | `Video` 字段填满 |
| 封装成 `render` 阶段 + 校验器 | 25 min | 阶段可跑 |

**输入**：`Assets` + `Audio`
**输出**：`artifacts/{task_id}/RENDER/v1/final.mp4` + `video.json`

**校验标准**

| # | 断言 |
|---|---|
| 1 | `ffprobe` 退出 0，时长 ∈ [45,90] |
| 2 | `has_audio == true`，`peak_db > -6` |
| 3 | `black_segments == 0` 且 `silence_segments == 0` |
| 4 | 浏览器能直接播放（手动点开一次） |
| 5 | 1080p、≥24fps、文件 < 60MB |

**边界条件**

| 场景 | 期望 |
|---|---|
| 音频比画面长 0.8s | 画面定格补足（`tpad`），不裁音频 |
| 音频比画面短 | 末帧定格（`tpad=stop_mode=clone`） |
| 中文路径 / 空格 | 一律用绝对路径 + 列表参数调用，**不拼 shell 字符串** |
| 字幕含 `:` `.` `'` | 用 `subtitles=filename=` 转义或改烧 `drawtext` |
| ffmpeg 非 0 退出 | 保留中间产物，`E_RENDER` 重试 1 次，再失败 → 纯图版 |

**降级预案**：字幕烧录炸 → 改成**外挂 srt**（播放器可加载），并在 README 注明。**先有片子，再谈字幕形式。**

---

### M4 · 分布式加固（120 min）

**验收句**：任务跑到一半 `kill -9` worker，30 秒内另一个 worker 接手，从**断点阶段**继续，最终产出视频，且 `events` 里有 `lease` 与 `state_transition` 记录。

**前置**：M3 绿（有 happy path 才谈得上加固）。

| 任务 | 时长 | 产出 |
|---|---|---|
| 租约续期 + 回收线程（15s 扫） | 25 min | 过期任务回 `PENDING` |
| `attempt` 计数 + 超 3 进死信 | 15 min | `FAILED` + `error` |
| 幂等键接入（`skip` 已完成的阶段） | 30 min | 重启后 `SKIPPED` |
| `scripts/chaos.py`：`kill` / `500` / `timeout` 三场景 | 30 min | 一条命令跑完 |
| 并发验证：2 worker 抢 1 任务 | 20 min | 无重复执行 |

**输入**：任意进行中的任务
**输出**：`states` 正确回收；`events` 完整；`docs/debt.md` 记录已知窗口

**校验标准**

| # | 断言 |
|---|---|
| 1 | `kill -9` 后 ≤ 45s（lease 60s + 扫描 15s）任务回到 `RUNNING` 且 `worker_id` 变了 |
| 2 | 续跑时已完成阶段 `status='SKIPPED'`，**不重复调用模型**（看 `events` 计数） |
| 3 | `attempt` 超过 3 → `FAILED`，`error` 含 `E_*` |
| 4 | 两个 worker 同时跑，同一任务只被抢一次（`worker_id` 唯一） |
| 5 | 接口持续 500 时，任务 3 次退避后进 `FAILED`，不无限重试 |

**边界条件**

| 场景 | 期望 |
|---|---|
| worker 在写 DB 时被杀 | 事务未提交 → 该阶段 `RUNNING` → 租约过期重跑（**幂等键保证不重复扣费**） |
| 时钟回拨 | 一律用 DB 侧 `datetime('now')`，不用进程本地时间 |
| 回收线程与 worker 抢同一任务 | `UPDATE ... WHERE status='PENDING'` 原子性保证只有一个成功 |
| 任务卡在 `RUNNING` 但无人持有 | 扫描兜底：`lease_until < now` 即回收 |

**降级预案**：回收线程复杂度超预期 → 只做「启动时扫一遍过期任务」，演示时用「重启 worker」代替自动回收。**演示脚本话术要改，但机制还在。**

**面试可讲**：at-least-once + 幂等 = 允许重复执行，但结果只算一次。**这是分布式最核心的一句话。**

---

### M5 · 质检 + 回退 + 字幕对齐（150 min）

**验收句**：对一条含 1 个坏镜头的任务，QC 能定位到**具体第几镜**、只重做它、其余产物复用，最终 `DONE` 或 `PARTIAL`。

**前置**：M4 绿。

| 任务 | 时长 | 产出 |
|---|---|---|
| `qc` 阶段：4 条 DoD → `Check[]` | 35 min | `QCReport` |
| 回退逻辑：`retry_shots` → 只重置该 `shots` 行 | 30 min | 精准重做 |
| 幂等复用验证：VOICE 不重跑 | 15 min | `SKIPPED` 事件 |
| ASR 对齐（`faster-whisper` 或云 ASR）替换估算 srt | 40 min | CER 可测 |
| 二次 QC 仍不过 → `PARTIAL` + `degraded_reason` | 30 min | 终态正确 |

**输入**：全部产物
**输出**：`qc.json`、可能的新 `assets`/`render`、终态

**校验标准**

| # | 断言 |
|---|---|
| 1 | 4 条 DoD 每条都有对应 `Check`，`severity=block` |
| 2 | 人为替换第 3 镜为纯黑图 → QC 报 `retry_shots=[3]` |
| 3 | 重跑后第 1、2 镜 `assets` 的 `hash` **不变**（复用） |
| 4 | `VOICE` 阶段 `status='SKIPPED'` |
| 5 | 连续 2 轮不过 → `PARTIAL`，`degraded_reason` 非空 |
| 6 | 字幕 CER < 1%（20 句抽样） |

**边界条件**

| 场景 | 期望 |
|---|---|
| QC 说"分镜与脚本不符" | `NEEDS_HUMAN`，不自动回 STORYBOARD（3 天内不做） |
| 重做 2 次仍黑图 | 用占位图 + `PARTIAL`，**不无限循环** |
| ASR 服务不可用 | 回落到估算 srt，`alignment="estimate"`，QC 标 `warn` |
| 所有镜都被判坏 | 只重做相似度最低的 2 镜（防成本爆炸） |

**面试可讲**：QC 不是"打分器"，是**路由器**——它只决定"回退到哪、退几镜、退几次"。

---

### M6 · i2v 高光镜 + 并发（120 min）

**验收句**：`kind_hint="video"` 的镜头走 i2v（异步提交 + 轮询 + 超时），与图片镜**并发**执行，总时长不超过串行的 60%。

**前置**：M5 绿。

| 任务 | 时长 | 产出 |
|---|---|---|
| `tools/i2v.py`：submit → poll → download，带指数退避 | 40 min | 单镜视频 |
| 并发调度：`asyncio.gather` + 信号量（图 3 / 视频 1） | 25 min | 并发跑 |
| 超时与降级：i2v 超时 → 该镜回落静图 | 25 min | 不阻塞 |
| 时长对齐：视频裁到 `est_sec` | 30 min | 不跑偏 |

**校验标准**

| # | 断言 |
|---|---|
| 1 | 至少 1 镜 `kind="video"`，`ffprobe` 可读 |
| 2 | 视频镜时长与 `est_sec` 偏差 ≤ 0.5s（裁剪后） |
| 3 | i2v mock 超时 → 该镜 `degraded=true`，`kind` 回落 `image`，任务继续 |
| 4 | 5 镜并发总耗时 < 串行耗时 × 0.6 |
| 5 | 轮询不超过 40 次，间隔 5s，超时即放弃 |

**边界条件**

| 场景 | 期望 |
|---|---|
| i2v 任务一直 `processing` | 5 分钟上限，超时降级 |
| 返回视频无音轨 | 正常（音轨来自 TTS） |
| 返回 4:3 或竖屏 | 统一裁切到 16:9，不拉伸 |
| 单镜失败 | 只影响该镜，不影响整条 |

**降级预案**：i2v 调试超过 1 小时 → **整条链路只保留图片镜**，README 写"i2v 已验证接口，因配额未纳入默认链路"。**诚实 > 硬塞。**

---

### M7 · 评测 + 测试 + 文档（180 min）

**验收句**：`make verify` 一条命令跑完 7 项验收；`pytest` 全绿；README 能让陌生人 3 分钟看懂。

**前置**：M6 绿。

| 任务 | 时长 | 产出 |
|---|---|---|
| `eval/golden.jsonl` 20 条（5 分区 × 4） | 25 min | 样本集 |
| `eval/run_eval.py`：批量跑 + 指标汇总 | 30 min | `report.md` |
| pytest 补到 ≥60 条（工具/状态机/loop 四闸/幂等/租约） | 45 min | 覆盖率 ≥60% |
| README（7 块）+ `docs/adr/` 2 篇 | 35 min | 文档 |
| 架构图 + 90s 演示视频 | 30 min | 可发 HR |
| `make verify` 脚本 | 15 min | 一键验收 |

**校验标准**

| # | 断言 |
|---|---|
| 1 | `pytest -q` 全绿，≥60 条，覆盖率 ≥60% |
| 2 | `python eval/run_eval.py` 输出 6 个指标，成功率 ≥80% |
| 3 | `make verify` 退出码 0 |
| 4 | README 首屏含：演示视频、指标表、架构图、3 条快速开始 |
| 5 | `git grep -n "sk-"` 为空 |

---

## 6. 边界条件总表（跨阶段，实现时逐条对照）

| 类别 | 条件 | 期望行为 | 归属 |
|---|---|---|---|
| 输入 | topic 为空/超 200 字/纯符号 | 422，不建任务 | M0 |
| 输入 | 重复 `Idempotency-Key` | 返回同一 `task_id` | M0 |
| 输入 | topic 含敏感词 | `NEEDS_HUMAN` | M1 |
| 输入 | 20 个任务同时提交 | 全部入队，队列不丢 | M0/M4 |
| 时间 | 阶段超时 | 按 3.1 表 → 重试/降级 | 全局 |
| 时间 | 任务总时长 > 20 min | 强制 `PARTIAL` | 全局 |
| 网络 | 上游 429 | backoff + jitter，≤3 次 | 全局 |
| 网络 | 断网 30s | 重试成功则继续；否则降级 | M4 |
| 资源 | 磁盘 < 200MB | 前置检查，拒绝新任务 | M0 |
| 资源 | 内存峰值 > 1.5GB | 图片流式处理，不整读 | M2 |
| 并发 | 两 worker 抢同一任务 | 只一个成功 | M0/M4 |
| 并发 | 同任务重复执行 | 幂等键保证结果一致、不重复扣费 | M4 |
| 崩溃 | 任意时刻 `kill -9` | 租约回收 + 断点续跑 | M4 |
| 崩溃 | DB 写一半 | 事务回滚，阶段保持 `RUNNING` → 重跑 | M4 |
| 数据 | 模型输出非法 JSON | 剥壳 → 校验 → 回灌重试 2 次 | M1 |
| 数据 | 产物文件缺失但 DB 标 DONE | 启动自检，回退该阶段 | M5 |
| 数据 | 字幕后端不可用 | 回落估算，`alignment=estimate` | M5 |
| 安全 | 工具返回内容含指令 | 视为数据，system 明令不得执行 | 全局 |
| 安全 | 模型试图调用未注册工具 | 白名单拦截，回灌"unknown tool" | M1 |
| 成本 | 单任务超 80¢ | `E_BUDGET` → `PARTIAL` | 全局 |
| 成本 | 同一 prompt 重复调用 | 结果缓存（磁盘），命中不计费 | M2 |

---

## 7. 每日质量门（每天收工前 5 分钟跑）

```bash
git status --short            # 无未提交的调试代码
pytest -q                     # 绿
python -m mypy app --ignore-missing-imports   # 无 error
git grep -n "print("  app/ | grep -v cli      # 为空
git grep -n "sk-"                            # 为空
curl -s localhost:8000/healthz               # ok
```

任何一条不绿 → **当天不写新功能**。

---

## 8. 风险登记册

| # | 风险 | 概率 | 影响 | 触发信号 | 预案 |
|---|---|---|---|---|---|
| R1 | ffmpeg 字幕烧录卡住 | 高 | 高 | 2h 未出片 | 改外挂 srt，先出片 |
| R2 | 文生图质量不稳定 | 高 | 中 | 人评 <3 分 | 固定 seed + 风格词模板 + 占位兜底 |
| R3 | 模型 JSON 不稳定 | 中 | 高 | 重试 2 次仍失败 | 改 function calling 强约束 |
| R4 | SQLite 锁竞争 | 中 | 中 | `database is locked` | WAL + busy_timeout=5000 + 重试 |
| R5 | i2v 调试超时 | 高 | 中 | >1h | 只保留图片镜，README 说明 |
| R6 | 范围膨胀 | 高 | 高 | 想加新功能 | 回看第 1.2 节「非目标」 |
| R7 | 预算烧超 | 低 | 中 | 单任务 >50¢ | 降分辨率/换便宜模型/加缓存 |
| R8 | 演示当天接口挂 | 中 | 高 | 调用失败 | **全链路产物缓存**，可离线重放 |

---

## 9. 时间盒与砍单规则

| 触发 | 动作 |
|---|---|
| 任一子任务超时 30 min | 执行该 M 的降级预案，记 `docs/debt.md`，前进 |
| 任一 M 超时 60 min | 砍掉该 M 的「可选项」，只保验收句 |
| D1 结束未出片 | **停一切优化**，D2 上午只做 M3 |
| D2 结束无故障恢复 | 砍 M6（i2v），D3 上午补 M4 |
| D3 14:00 仍未开始 M7 | 砍评测集到 10 条，保 README + 视频 |

**砍单优先级（从先砍到后砍）**：i2v → 并发 → LangGraph 移植 → ASR 对齐 → 评测集规模 → **故障恢复（最后才砍）**。

---

## 10. 验收脚本（写进 `Makefile`）

```make
verify: lint test eval chaos
lint:   mypy app --ignore-missing-imports && ! git grep -n "sk-"
test:   pytest -q --cov=app --cov-fail-under=60
eval:   python eval/run_eval.py --golden eval/golden.jsonl --out eval/report.md
chaos:  python scripts/chaos.py --all     # kill / 500 / timeout 三场景
demo:   python scripts/replay.py --task-id $(ID)   # 离线重放，演示兜底
```

---

## 11. 每日自检三问（写在便签上）

1. **今天有没有让「验收句」从不绿变绿？** 没有 → 今天白干。
2. **有没有引入一个"讲不清为什么"的设计？** 有 → 要么删，要么现在想清楚。
3. **如果明天就面试，我能演示哪 90 秒？** 演不出来 → 优先级错了。

---

## 附：D0 今晚的 30 分钟

1. `git init` + 建目录骨架（第 7 节 ARCHITECTURE 里的 `app/` 结构，空文件即可）
2. 写 `app/contracts.py`（第 4 节全文照抄，删掉用不上的字段）
3. 写 `tests/test_contracts.py`：3 条 golden 样例，跑通 `pytest`

**契约先落地的项目，第 2 天不会重构。**
