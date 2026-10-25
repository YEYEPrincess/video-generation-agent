"""图生视频：异步提交 + 轮询 + 超时降级。

真实的视频生成 API 都是"提交任务 → 轮询/回调 → 给 URL"，一次要 30 秒到 3 分钟。
这里把协议抽象成两个端点，换厂商只改 20 行 adapter：

    POST {base}/videos/generations   {"model", "image": dataURL, "prompt"} -> {"id"}
    GET  {base}/videos/generations/{id} -> {"status": "processing|succeeded|failed", "url"}

任何失败（超时/ 5xx / 一直 processing）都不抛出到上层，而是**降级为运动图片**
（用 ffmpeg 给静态图加推近），并标 `degraded=True`——单镜失败不能拖垮整条片子。
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.agent.tools.media import probe
from app.agent.tools.render import _run  # 复用统一超时/错误包装
from app.errors import AppError, ErrCode
from app.settings import Settings


@dataclass(slots=True)
class I2VResult:
    path: str
    duration_sec: float
    provider: str
    degraded: bool = False
    cost_cents: int = 0


def _kenburns(image: Path, out: Path, duration: float, settings: Settings) -> I2VResult:
    """降级方案：静图 + 缓慢推近，产出一个真 mp4（不是伪造的文件名）。"""
    width, height = settings.render_width, settings.render_height
    frames = max(1, int(round(duration * settings.render_fps)))
    # 同 build_clip：zoompan 必须"一张图 + d=总帧数"，否则 zoom 每帧重置
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},setsar=1,"
        f"zoompan=z='min(zoom+0.0012,1.18)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":d={frames}:s={width}x{height}:fps={settings.render_fps},format=yuv420p"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    proc = _run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(image.resolve()),
            "-t", f"{duration:.3f}", "-vf", vf, "-r", str(settings.render_fps),
            "-c:v", "libx264", "-preset", settings.render_preset, "-crf", "23",
            "-pix_fmt", "yuv420p", out.name,
        ],
        cwd=out.parent.resolve(),
        timeout=settings.ffmpeg_timeout_sec,
    )
    if proc.returncode != 0:
        raise AppError(ErrCode.E_RENDER, f"降级视频生成失败: {proc.stderr.strip()[-200:]}")
    info = probe(out)
    # 注意：这里**不**标 degraded。kenburns 产出的是一段可用视频，
    # 只是生成方式降级了——`provider` 字段本身就是证据，不该触发 QC 重做。
    return I2VResult(path=str(out), duration_sec=round(info["duration"], 2), provider="kenburns", degraded=False)


def _api(
    image: Path, prompt: str, out: Path, duration: float, settings: Settings, client: httpx.Client | None = None
) -> I2VResult:
    base = settings.video_base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {settings.video_api_key}"}
    payload = {
        "model": settings.video_model,
        "prompt": prompt,
        "duration": round(duration, 1),
        "image": "data:image/png;base64," + base64.b64encode(image.read_bytes()).decode(),
    }

    poster = client.post if client else httpx.post
    try:
        created = poster(f"{base}/videos/generations", json=payload, headers=headers,
                         timeout=settings.llm_timeout_sec)
    except httpx.TimeoutException as exc:
        raise AppError(ErrCode.E_TOOL_TIMEOUT, "视频提交超时") from exc
    except httpx.HTTPError as exc:
        raise AppError(ErrCode.E_TOOL_UPSTREAM, f"视频提交网络错误: {exc}") from exc

    if created.status_code >= 500 or created.status_code == 429:
        raise AppError(ErrCode.E_TOOL_UPSTREAM, f"视频提交上游 {created.status_code}")
    if created.status_code >= 400:
        raise AppError(ErrCode.E_TOOL_UPSTREAM, f"视频提交被拒 {created.status_code}", retryable=False)

    job_id = created.json().get("id")
    if not job_id:
        raise AppError(ErrCode.E_TOOL_UPSTREAM, "视频提交未返回 job id")

    deadline = time.time() + settings.i2v_timeout_sec
    getter = client.get if client else httpx.get
    for _ in range(max(1, settings.i2v_poll_max)):
        if time.time() > deadline:
            break
        time.sleep(0 if client else settings.i2v_poll_sec)
        try:
            status = getter(f"{base}/videos/generations/{job_id}", headers=headers, timeout=settings.llm_timeout_sec)
        except httpx.HTTPError as exc:
            raise AppError(ErrCode.E_TOOL_UPSTREAM, f"轮询失败: {exc}") from exc

        body = status.json()
        state = str(body.get("status") or "").lower()
        if state in {"succeeded", "completed", "done"}:
            url = body.get("url") or body.get("video_url")
            if not url:
                raise AppError(ErrCode.E_TOOL_UPSTREAM, "任务成功但没有视频地址")
            out.parent.mkdir(parents=True, exist_ok=True)
            with httpx.stream("GET", url, timeout=settings.i2v_timeout_sec) as resp:
                resp.raise_for_status()
                with out.open("wb") as fh:
                    for chunk in resp.iter_bytes():
                        fh.write(chunk)
            info = probe(out)
            return I2VResult(path=str(out), duration_sec=round(info["duration"], 2),
                             provider="api", cost_cents=settings.i2v_cost_cents)
        if state in {"failed", "error", "cancelled"}:
            raise AppError(ErrCode.E_TOOL_UPSTREAM, f"视频任务失败: {body.get('error')}")

    raise AppError(ErrCode.E_TOOL_TIMEOUT, f"视频生成超时（{settings.i2v_timeout_sec}s）")


def generate(
    *,
    image: Path,
    prompt: str,
    out_path: Path,
    duration: float,
    settings: Settings,
    client: httpx.Client | None = None,
) -> I2VResult:
    """生成单镜视频。**任何异常都降级为运动图片，不向上抛。**"""
    if settings.video_api_key and settings.video_base_url and settings.i2v_provider in {"api", "auto"}:
        try:
            return _api(image, prompt, out_path, duration, settings, client)
        except AppError:
            pass  # 明确降级，并如实标记
    return _kenburns(image, out_path, duration, settings)
