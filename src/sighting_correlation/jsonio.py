"""确定性的 JSON 输入输出。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable


class JsonDataError(ValueError):
    """JSON 文件缺失或损坏。"""


def load_json(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
        return json.loads(text)
    except OSError as exc:
        raise JsonDataError(f"无法读取 {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise JsonDataError(f"{path} 不是有效 JSON: {exc.msg}") from exc


def canonical_json(value: object) -> str:
    """生成跨平台一致的紧凑 JSON 文本。"""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def content_digest(values: Iterable[object]) -> str:
    """按输入顺序计算规范化内容摘要。"""

    digest = hashlib.sha256()
    for value in values:
        digest.update(canonical_json(value).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()
