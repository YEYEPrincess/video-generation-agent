"""Prompt 渲染：模板全部放 `app/prompts/*.md`，代码里不拼长字符串。

用 `string.Template`（`$var`），因为 prompt 里会大量出现 JSON 的 `{}`，
用 `str.format` 会被大括号转义搞死。
"""

from __future__ import annotations

from pathlib import Path
from string import Template

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"
_cache: dict[str, Template] = {}


def load(name: str) -> Template:
    if name not in _cache:
        path = PROMPT_DIR / f"{name}.md"
        if not path.exists():
            raise FileNotFoundError(f"prompt not found: {path}")
        _cache[name] = Template(path.read_text(encoding="utf-8"))
    return _cache[name]


def render(name: str, **kwargs: object) -> str:
    return load(name).substitute(**{k: str(v) for k, v in kwargs.items()})
