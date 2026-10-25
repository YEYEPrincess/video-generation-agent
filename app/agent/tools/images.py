"""文生图：有 key 走 API，没 key 走本地占位图。

为什么占位图要"看起来还行"：M3 出片时它就是画面本身。
纯灰块会让整条视频像 PPT，带渐变和排版的占位至少能看。
"""

from __future__ import annotations

import base64
import hashlib
import math
from dataclasses import dataclass
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from app.errors import AppError, ErrCode
from app.settings import Settings

FONT_CANDIDATES = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
)


@dataclass(slots=True)
class ImageResult:
    path: str
    width: int
    height: int
    provider: str
    cost_cents: int = 0
    degraded: bool = False


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)


def _wrap(text: str, per_line: int) -> list[str]:
    clean = "".join(ch for ch in text if ch.strip())
    return [clean[i : i + per_line] for i in range(0, len(clean), per_line)][:3]


def _palette(seed: str) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    digest = hashlib.sha1(seed.encode("utf-8")).digest()
    hue = digest[0] / 255.0
    base = (
        int(28 + 30 * math.sin(6.28 * hue)),
        int(34 + 30 * math.sin(6.28 * (hue + 0.33))),
        int(52 + 34 * math.sin(6.28 * (hue + 0.66))),
    )
    accent = (
        min(255, base[0] + 150),
        min(255, base[1] + 96),
        min(255, base[2] + 60),
    )
    return base, accent


def make_placeholder(prompt: str, narration: str, out_path: Path, size: tuple[int, int], index: int) -> ImageResult:
    """本地占位图：渐变底 + 大编号 + 旁白字幕块。"""
    width, height = size
    base, accent = _palette(prompt + narration)
    image = Image.new("RGB", (width, height), base)
    draw = ImageDraw.Draw(image)

    # 对角渐变
    for y in range(height):
        ratio = y / max(height - 1, 1)
        color = tuple(int(base[i] + (accent[i] - base[i]) * ratio * 0.55) for i in range(3))
        draw.line([(0, y), (width, y)], fill=color)

    # 柔光斑
    glow = Image.new("RGB", (width, height), base)
    gdraw = ImageDraw.Draw(glow)
    radius = int(min(width, height) * 0.45)
    gdraw.ellipse(
        [int(width * 0.55) - radius, int(height * 0.2) - radius, int(width * 0.55) + radius, int(height * 0.2) + radius],
        fill=(min(255, accent[0] + 30), min(255, accent[1] + 20), min(255, accent[2] + 10)),
    )
    glow = glow.filter(ImageFilter.GaussianBlur(radius // 3))
    image = Image.blend(image, glow, 0.35)
    draw = ImageDraw.Draw(image)

    # 编号
    draw.text((int(width * 0.06), int(height * 0.08)), f"{index + 1:02d}", font=_font(int(height * 0.22)),
              fill=(255, 255, 255))
    draw.text((int(width * 0.06), int(height * 0.33)), "PLACEHOLDER", font=_font(int(height * 0.035)),
              fill=(255, 255, 255))

    # 旁白块
    lines = _wrap(narration, 18)
    top = int(height * 0.62)
    for i, line in enumerate(lines):
        draw.text((int(width * 0.06), top + i * int(height * 0.075)), line, font=_font(int(height * 0.058)),
                  fill=(248, 248, 252))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(out_path, format="PNG")
    return ImageResult(path=str(out_path), width=width, height=height, provider="placeholder")


def _generate_via_api(prompt: str, out_path: Path, settings: Settings) -> ImageResult:
    payload = {"model": settings.img_model, "prompt": prompt, "size": settings.img_size, "n": 1}
    try:
        resp = httpx.post(
            settings.img_base_url.rstrip("/") + "/images/generations",
            json=payload,
            headers={"Authorization": f"Bearer {settings.img_api_key}"},
            timeout=settings.img_timeout_sec,
        )
    except httpx.TimeoutException as exc:
        raise AppError(ErrCode.E_TOOL_TIMEOUT, f"文生图超时（{settings.img_timeout_sec}s）") from exc
    except httpx.HTTPError as exc:
        raise AppError(ErrCode.E_TOOL_UPSTREAM, f"文生图网络错误: {exc}") from exc

    if resp.status_code == 429 or resp.status_code >= 500:
        raise AppError(ErrCode.E_TOOL_UPSTREAM, f"文生图上游 {resp.status_code}")
    if resp.status_code >= 400:
        raise AppError(ErrCode.E_TOOL_UPSTREAM, f"文生图请求被拒 {resp.status_code}", retryable=False)

    data = resp.json()["data"][0]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if data.get("b64_json"):
        out_path.write_bytes(base64.b64decode(data["b64_json"]))
    else:
        with httpx.stream("GET", data["url"], timeout=settings.img_timeout_sec) as r:
            r.raise_for_status()
            with out_path.open("wb") as fh:
                for chunk in r.iter_bytes():
                    fh.write(chunk)

    with Image.open(out_path) as img:
        width, height = img.size
    return ImageResult(
        path=str(out_path), width=width, height=height, provider="api", cost_cents=settings.img_cost_cents
    )


def generate(
    *,
    prompt: str,
    narration: str,
    out_path: Path,
    size: tuple[int, int],
    index: int,
    settings: Settings,
) -> ImageResult:
    """生成单镜画面。任何失败都降级为占位图，绝不让整条任务挂掉。"""
    if settings.img_provider in {"api", "auto"} and settings.img_api_key and settings.img_base_url:
        try:
            return _generate_via_api(prompt, out_path, settings)
        except AppError:
            if settings.img_provider == "api":
                # 显式要求走 API 时，失败标记为降级但依然出图
                pass
            result = make_placeholder(prompt, narration, out_path, size, index)
            result.degraded = True
            return result
    return make_placeholder(prompt, narration, out_path, size, index)
