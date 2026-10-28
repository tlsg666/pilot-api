"""让测试用独立的 SQLite 文件，绝不动到 platform.db。"""
import os
import tempfile
from pathlib import Path

_test_db = Path(tempfile.gettempdir()) / "platform-test.db"
os.environ.setdefault("PLATFORM_DB", str(_test_db))
for suffix in ("", "-wal", "-shm"):
    p = Path(str(_test_db) + suffix)
    if p.exists():
        p.unlink()