"""模型客户端：JSON 抠取、Mock、真实客户端的错误归类。"""

from __future__ import annotations

import json

import httpx
import pytest

from app.agent.llm import MockLLM, OpenAICompatLLM, extract_json
from app.errors import AppError, ErrCode
from app.settings import Settings


def _settings(**kw) -> Settings:
    base = dict(
        _env_file=None,
        llm_base_url="https://api.example.com/v1",
        llm_api_key="test-key",
        llm_model="test-model",
    )
    base.update(kw)
    return Settings(**base)


class TestExtractJson:
    def test_plain(self):
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_fenced_block(self):
        assert extract_json('```json\n{"a": 2}\n```') == {"a": 2}

    def test_with_chatter(self):
        assert extract_json('好的，这是结果：{"a": 3} 希望有帮助') == {"a": 3}

    def test_nested_braces(self):
        assert extract_json('{"a": {"b": [1, 2]}}')["a"]["b"] == [1, 2]

    def test_no_json_raises(self):
        with pytest.raises(AppError) as exc:
            extract_json("我不知道")
        assert exc.value.code is ErrCode.E_VALIDATION
        assert exc.value.retryable is True


class TestMockLLM:
    def test_returns_stage_payload(self):
        llm = MockLLM()
        res = llm.complete(system="[STAGE:RESEARCH]\n系统提示", user="主题")
        assert json.loads(res.text)["hooks"]
        assert llm.calls == ["RESEARCH"]

    def test_script_payload_has_segments(self):
        res = MockLLM().complete(system="[STAGE:SCRIPT]", user="x")
        assert len(json.loads(res.text)["segments"]) >= 3


class TestOpenAICompat:
    def test_success_and_cost(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body["response_format"] == {"type": "json_object"}
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": '{"ok": true}'}}],
                    "usage": {"total_tokens": 120},
                },
            )

        llm = OpenAICompatLLM(_settings(llm_price_cents_per_1k=100.0), client=httpx.Client(transport=httpx.MockTransport(handler)))
        res = llm.complete(system="s", user="u")
        assert res.text == '{"ok": true}'
        assert res.tokens == 120
        assert res.cost_cents == 12  # 120/1000 * 100

    def test_5xx_is_retryable_upstream(self):
        llm = OpenAICompatLLM(
            _settings(),
            client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503, text="boom"))),
        )
        with pytest.raises(AppError) as exc:
            llm.complete(system="s", user="u")
        assert exc.value.code is ErrCode.E_TOOL_UPSTREAM
        assert exc.value.retryable is True

    def test_4xx_is_not_retryable(self):
        llm = OpenAICompatLLM(
            _settings(),
            client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401, text="bad key"))),
        )
        with pytest.raises(AppError) as exc:
            llm.complete(system="s", user="u")
        assert exc.value.code is ErrCode.E_TOOL_UPSTREAM
        assert exc.value.retryable is False

    def test_timeout_maps_to_tool_timeout(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow", request=request)

        llm = OpenAICompatLLM(_settings(), client=httpx.Client(transport=httpx.MockTransport(handler)))
        with pytest.raises(AppError) as exc:
            llm.complete(system="s", user="u")
        assert exc.value.code is ErrCode.E_TOOL_TIMEOUT
