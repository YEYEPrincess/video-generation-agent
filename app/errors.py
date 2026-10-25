"""错误码与统一异常。

规则（PROJECT-PLAN.md 3.2）：所有异常最终必须归到这 8 个码之一。
`except Exception` 只允许出现在 worker 最外层。
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class ErrCode(str, Enum):
    E_VALIDATION = "E_VALIDATION"
    E_TOOL_TIMEOUT = "E_TOOL_TIMEOUT"
    E_TOOL_UPSTREAM = "E_TOOL_UPSTREAM"
    E_TOOL_EMPTY = "E_TOOL_EMPTY"
    E_BUDGET = "E_BUDGET"
    E_LOOP = "E_LOOP"
    E_RENDER = "E_RENDER"
    E_INTERNAL = "E_INTERNAL"


#: 默认是否可重试。E_BUDGET / E_LOOP / E_INTERNAL 属于"重试也没用"的一类。
RETRYABLE: dict[ErrCode, bool] = {
    ErrCode.E_VALIDATION: True,
    ErrCode.E_TOOL_TIMEOUT: True,
    ErrCode.E_TOOL_UPSTREAM: True,
    ErrCode.E_TOOL_EMPTY: True,
    ErrCode.E_RENDER: True,
    ErrCode.E_BUDGET: False,
    ErrCode.E_LOOP: False,
    ErrCode.E_INTERNAL: False,
}


class AppError(Exception):
    """带错误码的业务异常。runner 靠 code 决定重试还是终止。"""

    def __init__(
        self,
        code: ErrCode,
        message: str,
        *,
        retryable: bool | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"[{code.value}] {message}")
        self.code = code
        self.message = message
        self.retryable = RETRYABLE[code] if retryable is None else retryable
        self.detail = detail or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
            "detail": self.detail,
        }
