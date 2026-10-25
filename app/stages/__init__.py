"""七个阶段：research / script / storyboard / assets / voice / render / qc。

注册顺序很重要：先注册全部占位实现（fake），再用真实实现覆盖已完成的阶段。
M1 覆盖前三个，M2 覆盖 ASSETS/VOICE，M3 覆盖 RENDER，M5 覆盖 QC。
"""

from app.stages import fake  # noqa: F401  —— 兜底：所有阶段先有占位实现
from app.stages import research, script, storyboard  # noqa: F401  —— M1：覆盖前三个阶段
