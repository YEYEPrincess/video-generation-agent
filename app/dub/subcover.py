"""字幕区域检测 + 覆盖（译制的"字幕擦除"最小实现）。

问题：源视频的字幕是**烧进画面**的（hardcoded subtitle），不是外挂轨。
直接叠加英文字幕 = 双语叠在一起，不能看。

三条路，按成本排序：
1. **遮挡重绘**（本文件）：找到字幕所在横带 → 盖一条深色带 → 在带上烧新字幕。
   成本最低、效果稳定；缺点是盖住了一部分画面。
2. **inpainting 修复**：`cv2.inpaint` 把字抹掉再补。字幕带 1000×120 像素，
   这么大区域补出来是糊的，观感反而更差。
3. **生成式修复**：ProPainter 一类视频修复模型，效果最好但要 GPU，不在本次范围。

检测方法（踩过坑之后的版本）：
第一版用 Canny 边缘密度，结果锁到了画面里的高对比物体上，字幕只盖住一半。
字幕的真实特征是**纯白字 + 黑描边**，所以更稳的做法是统计"每行有多少像素接近纯白"，
取底部区域里密度显著高于背景的行段。
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from app.errors import AppError, ErrCode

WHITE = 205          # 纯白字阈值
MIN_ROW_DENSITY = 0.015


def detect_band(video: Path, samples: int = 8) -> tuple[int, int]:
    """返回 (y, height)：字幕带的位置与高度。检测不到时给底部保守带。"""
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise AppError(ErrCode.E_RENDER, f"打不开视频: {video}", retryable=False)

    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        if height <= 0:
            raise AppError(ErrCode.E_RENDER, "拿不到视频尺寸", retryable=False)

        start_row = int(height * 0.55)          # 字幕几乎不会出现在画面上半部分
        acc = np.zeros(height, dtype=np.float32)
        taken = 0
        for i in range(samples):
            if total > samples:
                cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * (i + 0.5) / samples))
            ok, frame = cap.read()
            if not ok:
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            acc += (gray > WHITE).mean(axis=1)
            taken += 1

        if taken == 0:
            return int(height * 0.70), int(height * 0.16)

        density = acc / taken
        # 只在"最密的那一段"画带子：字幕是画面里白像素最集中的横条。
        # 用固定高度的滑动窗口找峰值，比"取所有高密度行"稳得多——
        # 后者会被画面里的白色物体带跑（我们踩过）。
        win = max(int(height * 0.16), 60)
        kernel = np.ones(win, dtype=np.float32)
        rolling = np.convolve(density, kernel, mode="same")
        rolling[: start_row] = 0          # 只在下半部分找
        y0 = int(np.argmax(rolling))
        y0 = max(int(height * 0.50), min(y0, height - win - 1))
        return y0, win
    finally:
        cap.release()


def cover_filter(y: int, height: int, alpha: float = 0.86) -> str:
    """ffmpeg 的 drawbox：盖一条深色带，把原字幕压在下面。"""
    return f"drawbox=x=0:y={y}:w=iw:h={height}:color=black@{alpha}:t=fill"
