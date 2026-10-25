"""七个阶段：research / script / storyboard / assets / voice / render / qc。

导入本包时自动注册默认 handler —— M0 是占位实现（fake），
M1 起在这里替换为真实模块，runner 不需要改动。
"""

from app.stages import fake  # noqa: F401  —— 注册 M0 占位 handler，有副作用
