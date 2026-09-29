"""Versioned URLs for the app's own static files.

Templates load scripts and the stylesheet as `{{ asset('model.js') }}` -> `/static/model.js?v=<hash>`.
The hash is the file's contents, so a deploy that changes a file changes its address and browsers
fetch the new copy instead of running a cached old one (seen 29 Sep 2026: a stale model.js beside a
new shell.js). The hash is recomputed only when the file's size or modification time changes, so a
local edit shows up without a restart.
"""
import hashlib
from functools import lru_cache
from pathlib import Path


def make_asset_url(static_dir: Path):
    root = Path(static_dir).resolve()

    @lru_cache(maxsize=256)
    def _version(path: str, mtime_ns: int, size: int) -> str:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:10]

    def asset_url(name: str) -> str:
        path = (root / name).resolve()
        if root not in path.parents:
            raise ValueError(f"not a static file: {name}")
        st = path.stat()                                    # FileNotFoundError for a missing file: a template bug
        return f"/static/{name}?v={_version(str(path), st.st_mtime_ns, st.st_size)}"

    return asset_url
