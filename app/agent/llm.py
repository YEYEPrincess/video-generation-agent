"""模型客户端：一个协议、两个实现（真实 / Mock）。

设计要点：
- 只依赖 OpenAI 兼容的 `/chat/completions`，换厂商只改 base_url + model；
- 错误一律归到 8 个错误码（超时 / 上游 / 校验），**不在客户端重试**——
  重试策略统一由 runner 管，避免两层退避叠乘；
- 无 key 时自动回落 MockLLM，保证测试与离线开发永远能跑。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.errors import AppError, ErrCode
from app.settings import Settings


@dataclass(slots=True)
class LLMResult:
    text: str
    tokens: int = 0
    cost_cents: int = 0
    model: str = ""


class LLMClient(Protocol):
    name: str

    def complete(
        self,
        *,
        system: str,
        user: str,
        json_mode: bool = True,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResult: ...


# --------------------------------------------------------------------- 解析
def extract_json(text: str) -> dict:
    """从模型输出里抠出 JSON。容忍 ```json 包裹、前后寒暄。"""
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(raw[start : end + 1])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError as exc:
            raise AppError(
                ErrCode.E_VALIDATION, f"JSON 解析失败: {exc}", detail={"snippet": raw[:200]}
            ) from exc
    raise AppError(ErrCode.E_VALIDATION, "模型输出里没有 JSON", detail={"snippet": raw[:200]})


def stage_marker(stage: str) -> str:
    """system prompt 里的阶段标记，MockLLM 靠它选择预置响应。"""
    return f"[STAGE:{stage}]"


# --------------------------------------------------------------------- 真实实现
class OpenAICompatLLM:
    name = "openai-compat"

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.s = settings
        self._client = client

    def _post(self, payload: dict) -> httpx.Response:
        url = self.s.llm_base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {self.s.llm_api_key}", "Content-Type": "application/json"}
        try:
            if self._client is not None:
                return self._client.post(url, json=payload, headers=headers, timeout=self.s.llm_timeout_sec)
            return httpx.post(url, json=payload, headers=headers, timeout=self.s.llm_timeout_sec)
        except httpx.TimeoutException as exc:
            raise AppError(ErrCode.E_TOOL_TIMEOUT, f"LLM 超时（{self.s.llm_timeout_sec}s）") from exc
        except httpx.HTTPError as exc:
            raise AppError(ErrCode.E_TOOL_UPSTREAM, f"LLM 网络错误: {exc}") from exc

    def complete(
        self,
        *,
        system: str,
        user: str,
        json_mode: bool = True,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResult:
        payload: dict = {
            "model": self.s.llm_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.s.llm_temperature if temperature is None else temperature,
            "max_tokens": self.s.llm_max_tokens if max_tokens is None else max_tokens,
        }
        if json_mode and self.s.llm_json_mode:
            payload["response_format"] = {"type": "json_object"}

        resp = self._post(payload)
        if resp.status_code == 429 or resp.status_code >= 500:
            raise AppError(
                ErrCode.E_TOOL_UPSTREAM, f"LLM 上游 {resp.status_code}", detail={"body": resp.text[:300]}
            )
        if resp.status_code >= 400:
            raise AppError(
                ErrCode.E_TOOL_UPSTREAM,
                f"LLM 请求被拒 {resp.status_code}（检查 key/model/base_url）",
                retryable=False,
                detail={"body": resp.text[:300]},
            )

        data = resp.json()
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError) as exc:
            raise AppError(ErrCode.E_TOOL_UPSTREAM, "LLM 响应结构异常", detail={"body": str(data)[:300]}) from exc

        usage = data.get("usage") or {}
        tokens = int(usage.get("total_tokens") or 0)
        cost = int(round(tokens / 1000 * self.s.llm_price_cents_per_1k))
        return LLMResult(text=text, tokens=tokens, cost_cents=cost, model=self.s.llm_model)


# --------------------------------------------------------------------- Mock
MOCK_BY_STAGE: dict[str, dict] = {
    "RESEARCH": {
        "partition": "知识",
        "angle": "从新观众与平台算法的双重视角，解释老番为什么重新流行",
        "hooks": [
            "你的首页最近是不是也被老番占领了",
            "十年前的分镜，为什么现在看反而更舒服",
            "谁在把老动画重新推到你面前",
        ],
        "refs": [
            {"title": "老番考古为什么突然火了", "ref": "BV1xx411c7mD", "why": "同类选题的播放数据参考"}
        ],
        "keywords": ["老番", "考古", "怀旧", "二创", "弹幕"],
    },
    "SCRIPT": {
        "title": "老番为什么又被翻出来了",
        "hook": "你的首页，最近是不是也被十年前的老番占领了",
        "segments": [
            {"text": "最近打开B站，你会发现十年前的番剧剪辑又回到了首页，播放量还在往上走。"},
            {"text": "这不是简单的怀旧滤镜，而是一批二十岁上下的新观众，把老片子当成了新的下饭剧。"},
            {"text": "对他们来说，画质粗糙反而提供了安全感，节奏慢下来才看得清人物之间的关系。"},
            {"text": "平台也在推波助澜，老番的版权成本更低、二创素材更多，算法更容易找到它的受众。"},
            {"text": "于是弹幕变成了新的客厅，一群人在同一分钟里，为同一句台词打出相同的问号。"},
            {"text": "所以真正被重看的不是动画，而是那个还愿意花四十分钟看完一集的自己。"},
        ],
    },
}


class MockLLM:
    """确定性模型：无 key 时的默认实现，也是所有单测的基础。"""

    name = "mock"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def complete(
        self,
        *,
        system: str,
        user: str,
        json_mode: bool = True,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResult:
        match = re.search(r"\[STAGE:([A-Z_]+)\]", system)
        stage = match.group(1) if match else "UNKNOWN"
        self.calls.append(stage)
        payload = MOCK_BY_STAGE.get(stage, {"note": f"no mock for {stage}"})
        text = json.dumps(payload, ensure_ascii=False)
        return LLMResult(text=text, tokens=len(text) // 2, cost_cents=0, model="mock")


def get_llm(settings: Settings) -> LLMClient:
    """有 key 走真实模型，否则回落 Mock —— 保证任何环境都能跑。"""
    if settings.mock_llm or not settings.llm_api_key or not settings.llm_base_url or not settings.llm_model:
        return MockLLM()
    return OpenAICompatLLM(settings)
