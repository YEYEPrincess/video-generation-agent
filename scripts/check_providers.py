"""检查 provider 配置是否真的可用（比跑一整条任务快 30 倍）。

    python scripts/check_providers.py          # 文本 + 图像
    python scripts/check_providers.py --llm    # 只查文本（几乎不花钱）
    python scripts/check_providers.py --image  # 只查图像（会真实生成 1 张）

它检查四件事：
1. key 有没有填；
2. 网络能不能通、模型名对不对（真正发一次请求）；
3. 返回是不是 OpenAI 形状（`data[0]` / `choices[0]`）；
4. 出错时把厂商返回的原文打出来——不要猜。
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.agent.llm import extract_json, get_llm, stage_marker  # noqa: E402
from app.agent.tools.images import _generate_via_api  # noqa: E402
from app.errors import AppError  # noqa: E402
from app.settings import Settings  # noqa: E402


def mask(value: str) -> str:
    if not value:
        return "（未填）"
    return f"{value[:4]}***{value[-4:]}" if len(value) > 8 else "***"


def check_llm(settings: Settings) -> bool:
    print("\n=== 1. 文本模型 ===")
    print(f"  base_url : {settings.llm_base_url or '（未填）'}")
    print(f"  model    : {settings.llm_model or '（未填）'}")
    print(f"  key      : {mask(settings.llm_api_key)}")
    if not settings.llm_api_key:
        print("  [SKIP] 没有 key，当前会用 MockLLM（全主题同一份文案）")
        return False

    llm = get_llm(settings)
    print(f"  实际实现 : {llm.name}")
    try:
        result = llm.complete(
            system=stage_marker("RESEARCH") + "\n你是测试助手，只输出 JSON。",
            user='只输出这段 JSON，不要任何解释：{"ok": true, "msg": "hello"}',
            max_tokens=60,
            temperature=0,
        )
    except AppError as exc:
        print(f"  [FAIL] {exc.code.value}: {exc.message}")
        if exc.detail.get("body"):
            print(f"         厂商原文: {str(exc.detail['body'])[:300]}")
        if exc.code.value == "E_TOOL_UPSTREAM" and not exc.retryable:
            print("         提示: 401 多为 key 错；404 多为 base_url 少/多 /v1；400 可能是模型名或 response_format")
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"  [FAIL] 未归类异常 {type(exc).__name__}: {exc}")
        return False

    print(f"  返回     : {result.text[:120]!r}")
    print(f"  tokens   : {result.tokens}  cost: {result.cost_cents}c")
    try:
        parsed = extract_json(result.text)
        print(f"  JSON 解析: OK -> {parsed}")
    except AppError as exc:
        print(f"  JSON 解析: FAIL（{exc.message}）→ 把 LLM_JSON_MODE 设为 false 再试")
        return False
    return True


def check_image(settings: Settings) -> bool:
    print("\n=== 2. 文生图 ===")
    print(f"  base_url : {settings.img_base_url or '（未填）'}")
    print(f"  model    : {settings.img_model}")
    print(f"  size     : {settings.img_size}")
    print(f"  key      : {mask(settings.img_api_key)}")
    if not settings.img_api_key:
        print("  [SKIP] 没有 key，当前会用本地占位图（编号 + PLACEHOLDER）")
        return False

    out = Path(tempfile.mkdtemp()) / "probe.png"
    try:
        result = _generate_via_api("一只坐在窗台上的橘猫，扁平插画，暖光", out, settings)
    except AppError as exc:
        print(f"  [FAIL] {exc.code.value}: {exc.message}")
        if exc.detail.get("body"):
            print(f"         厂商原文: {str(exc.detail['body'])[:300]}")
        print("         常见原因：不支持该 size / 模型名不对 / 账户未开通该模型")
        return False
    except KeyError as exc:
        print(f"  [FAIL] 返回结构不是 OpenAI 形状（缺 {exc}）")
        print("         代码只认 data[0].url 或 data[0].b64_json")
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"  [FAIL] {type(exc).__name__}: {exc}")
        return False

    size = out.stat().st_size if out.exists() else 0
    print(f"  生成成功 : {result.width}x{result.height}, {size // 1024}KB, provider={result.provider}")
    return size > 1000


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", action="store_true")
    ap.add_argument("--image", action="store_true")
    args = ap.parse_args()
    only_llm = args.llm and not args.image
    only_image = args.image and not args.llm

    settings = Settings()
    print(f"读取 .env: {Path('.env').resolve()}  (存在={Path('.env').exists()})")

    ok_llm = check_llm(settings) if not only_image else None
    ok_image = check_image(settings) if not only_llm else None

    print("\n=== 结论 ===")
    if ok_llm is not None:
        print(f"  文本模型: {'PASS' if ok_llm else 'NOT READY'}")
    if ok_image is not None:
        print(f"  文生图  : {'PASS' if ok_image else 'NOT READY'}")
    if (ok_llm is None or ok_llm) and (ok_image is None or ok_image):
        print("  两个都通过，可以跑整条任务了")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
