# 真实模型联调记录（DeepSeek + CogView）

| 项 | 内容 |
|---|---|
| 日期 | 2026-10-26 |
| 文本模型 | DeepSeek `deepseek-chat`（OpenAI 兼容，支持 json_object） |
| 文生图 | 智谱 `cogview-3-flash`，`1344x768` |
| 结论 | ✅ **端到端跑通**：DONE / 47.27s 成片 / 1080p / QC 95 |
| 代价 | 期间发现并修了 4 个真实问题（见 §3） |

![真实模型成片抽帧](../assets/real-frame.png)

---

## 1. 配置

`.env`（不进仓库）：

```ini
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
IMG_BASE_URL=https://open.bigmodel.cn/api/paas/v4
IMG_MODEL=cogview-3-flash
IMG_SIZE=1344x768
ASSETS_CONCURRENCY=1     # 关键：免费档扛不住并发
IMG_RETRY=3
```

`scripts/check_providers.py` 先做连通性自检（真实发一次请求）：

```
文本模型: PASS   返回 {"ok": true, "msg": "hello"} / 73 tokens / JSON 可解析
文生图  : PASS   1344x768 / 92KB / provider=api
```

---

## 2. 三次运行的过程

| 次 | 结果 | 耗时 | 现象 |
|---|---|---|---|
| #1 | PARTIAL | 139.8s | 6 镜里 3 镜回落占位图；QC 报 8 段静音 |
| #2 | DONE | 401.8s | 7 镜里 5 镜 429 → QC 判 RETRY → 回退重做 → 多跑两轮渲染 |
| #3 | **DONE** | **137.6s** | 0 降级，5 张图全部 API 生成，QC 95 |

第 3 次的分阶段耗时（ms）：

```
RESEARCH  3000 | SCRIPT 2272 | STORYBOARD 4 | ASSETS 50620 | VOICE 11358 | RENDER 70180 | QC 75
```

**真实模型带来的可见变化**：

- `RESEARCH.angle` 从 Mock 的固定句变成"年轻人重看老动画不是怀旧，是在逃离新番的信息过载"
- 脚本 5 段 / 47s（Mock 恒定 6 段 / 45s），时长分布终于开始浮动
- QC 仍 95 分，唯一 warn 是"第 0 镜回落运动图片"（没有视频 API key）

---

## 3. 四个真实问题（这才是接真模型的价值）

### 问题 1：CogView 免费档 429，3 并发直接打回一半

- **现象**：#2 里 5/7 张图返回 `429`，全部降级成占位图。
- **根因**：`assets_concurrency=3` + 免费档的 QPS/并发限制；而 `images.generate` **没有自己的重试**（当时只有阶段级重试）。
- **修复**：① `ASSETS_CONCURRENCY=1`；② 单图重试 3 次；③ 读 `Retry-After` 头，按厂商要求等待，而不是硬编码退避。
- **教训**：**"并发"是一个需要被验证的假设**。在本机 mock 下并发 3 是最优，接真厂商就是灾难。

### 问题 2：8 段"静音"其实是中文句号的自然停顿

- **现象**：#1 QC 报 `no_silence FAIL: 8 段`，但音频里明明一直有人说话。
- **根因**：`silencedetect` 阈值设成 `d=0.5s`。Mock 文本没有标点，没暴露；真实脚本每句带「。」，edge-tts 在句号处停 0.5–0.8s，全被判成缺陷。
- **修复**：阈值改 `d=1.5s`（1.5 秒以上才算"死气"），噪声门限 `-38dB`。
- **教训**：**检测阈值必须用真实数据校准**。0.5s 在合成数据上"很严格"，在真实语音上就是误杀。

### 问题 3：图片失败的原因被吞掉了

- **现象**：#1 只知道"3 张是占位图"，不知道为什么。
- **根因**：`generate()` 捕获 `AppError` 后直接回落占位图，错误信息丢弃。
- **修复**：`ImageResult.error` + `events` 里记 `image_failures` + `meta.image_failures`。
- **效果**：一眼看到 `E_TOOL_UPSTREAM: 文生图上游 429`，直接定位到限流。

### 问题 4：任务正常收尾却报"租约丢失"

- **现象**：每次 DONE 之后都有一条 `lease` warn：`{"event": "lease_lost"}`。
- **根因**：QC 判完 DONE 后，`run_task` 的循环回到开头，发现状态已终态，仍走了"非 RUNNING 就报丢失"的分支。
- **修复**：先判断终态直接返回，再判断租约归属。
- **教训**：**正常路径上的告警会训练人忽略告警**，比没有告警更糟。

---

## 4. 观察与已知限制

| 项 | 说明 |
|---|---|
| 水印 | CogView 生成图右下角有「AI生成」水印，属于厂商行为，不抹除 |
| 字幕换行 | 少数行会在「，」处断行（libass 自动折行），观感小瑕疵 |
| 耗时 | ASSETS 50.6s（5 张串行）+ RENDER 70.2s，占 88%；接真图后总时长从 28s 涨到 138s |
| 成本 | DeepSeek 约 2k tokens/条（不足 1 分钱）；CogView-3-Flash 免费 |

---

## 5. 下一步

1. **用真模型重跑 20 条评测** 并替换 README 指标表（现在 README 上仍是 Mock 数据）；
2. 给 `ASSETS` 加"失败重排"：429 的镜头退避后重试，仍失败才占位（目前是重试 3 次固定间隔）；
3. 若智谱付费档可用，把并发调回 2–3，观察 429 是否消失。
