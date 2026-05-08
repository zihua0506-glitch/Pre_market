"""
common/env.py

Single source of truth for parsing the project's `.env` file. Replaces the six
near-identical implementations scattered across `strategies/*.py` and
`scripts/*.py`.

The .env format is intentionally simple — no quoting, no interpolation, no
multi-line values. Each non-comment, non-blank line is `KEY=VALUE`.

Functions:
  load_env(path) -> dict[str, str]      Parse .env into a dict.
  write_env(path, updates)              Update keys in-place, preserving
                                        comments and order.

Both helpers also fold in any matching env vars from `os.environ` (matching
prefixes specified by the caller), so production deployments that prefer
EnvironmentFile= or systemd-style overrides still work.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable


def load_env(path: Path | str, *, env_prefixes: Iterable[str] = ("POLY_", "PROXY_", "DASHBOARD_")) -> dict[str, str]:
    """Parse the .env at ``path`` into a dict.

    Lines starting with `#` and blank lines are ignored. Values are trimmed of
    surrounding whitespace; no shell quoting / interpolation is performed.

    Any environment variables whose names start with one of ``env_prefixes`` are
    folded in *after* the file (so they win on conflict). This lets systemd
    overrides or one-off `KEY=v python -m ...` invocations take precedence.
    """
    out: dict[str, str] = {}
    p = Path(path)
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            out[k.strip()] = v.strip()
    if env_prefixes:
        prefix_tuple = tuple(env_prefixes)
        out.update({k: v for k, v in os.environ.items() if k.startswith(prefix_tuple)})
    return out


def write_env(path: Path | str, updates: dict[str, str]) -> None:
    """Overwrite known keys in-place; append unknown keys at the end.

    Preserves the order, comments, and blank lines of the existing file. If the
    file doesn't exist, it's created with just the keys from ``updates``.
    """
    p = Path(path)
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    seen: set[str] = set()
    new_lines: list[str] = []
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.split("=", 1)[0].strip()
            if k in updates:
                new_lines.append(f"{k}={updates[k]}")
                seen.add(k)
                continue
        new_lines.append(line)
    for k, v in updates.items():
        if k not in seen:
            new_lines.append(f"{k}={v}")
    p.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


# Back-compat alias used by older code paths (private-name imports).
_load_env = load_env
