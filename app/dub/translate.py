"""翻译：一次把整段字幕翻完，而不是一句一句翻。

为什么不逐句翻译：
1. 中文句子常常"承前省略"（这、那、它），单独翻译会指代不清；
2. 整段一起翻，模型能看到上下文，人称和术语更统一；
3. 一次调用比 20 次调用便宜、也更快。

代价：模型可能少给/多给几条。所以必须**校验条数与 id 对齐**，对不上就重试一次。
这不是"防呆"，是真实会发生的事——我们实测过一次返回 11 条、输入 12 条。
"""

from __future__ import annotations

import json

from app.agent.llm import extract_json, get_llm, stage_marker
from app.agent.prompts import render
from app.dub.types import Segment
from app.errors import AppError, ErrCode
from app.settings import Settings

SYSTEM = stage_marker("TRANSLATE") + "\n你是字幕翻译，只输出 JSON，不要解释。"

#: 默认术语表：B站生态里的高频词，直译会很怪。
#: 术语表是"便宜的一致性"——比在 prompt 里反复叮嘱有效得多。
DEFAULT_GLOSSARY = {
    "B站": "Bilibili",
    "UP主": "creator",
    "老番": "classic anime",
    "新番": "new anime",
    "二创": "fan edit",
    "弹幕": "danmaku",
}


def _prompt_payload(segments: list[Segment]) -> str:
    return json.dumps(
        [{"id": s.idx, "zh": s.text, "sec": round(s.slot, 2)} for s in segments],
        ensure_ascii=False,
    )


def translate(segments: list[Segment], settings: Settings, glossary: dict[str, str] | None = None) -> list[Segment]:
    """把 `segment.translated` 填好并返回（原地修改 + 返回同一个列表）。"""
    if not segments:
        return segments

    llm = get_llm(settings)
    merged = {**DEFAULT_GLOSSARY, **(glossary or {})}
    glossary_text = "\n".join(f"- {k} → {v}" for k, v in merged.items()) or "（无）"
    last_error = ""

    for attempt in range(2):
        user = render(
            "translate",
            source=settings.dub_source_lang,
            target=settings.dub_target_lang,
            count=len(segments),
            glossary=glossary_text,
            payload=_prompt_payload(segments),
        )
        if attempt:
            user += f"\n\n【上一次不合格】{last_error}\n请严格保证 id 与条数一致。"

        result = llm.complete(system=SYSTEM, user=user, max_tokens=2000)
        try:
            raw = extract_json(result.text)
        except AppError as exc:
            last_error = exc.message
            continue

        items = raw.get("segments") or []
        mapping = {int(item["id"]): str(item.get("text") or "").strip() for item in items if "id" in item}
        if len(mapping) != len(segments):
            last_error = f"返回 {len(mapping)} 条，期望 {len(segments)} 条"
            continue

        # 长度预算检查：prompt 里写了"≤ sec×14"，但模型经常超。
        # 约束不能只写在 prompt 里，必须能检测、能带着具体意见重试。
        limits = {s.idx: max(8, int(s.slot * 14)) for s in segments}
        over = [i for i, text in mapping.items() if i in limits and len(text) > limits[i] * 1.15]
        if over and attempt == 0:
            last_error = "；".join(f"id={i} 超长（限 {limits[i]} 字符，实际 {len(mapping[i])}）" for i in over[:6])
            continue

        for seg in segments:
            text = mapping.get(seg.idx, "")
            if not text:
                last_error = f"第 {seg.idx} 条为空"
                break
            seg.translated = text
        else:
            return segments

    raise AppError(ErrCode.E_VALIDATION, f"翻译结果不符合预期: {last_error}")
