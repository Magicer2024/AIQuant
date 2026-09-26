"""在测试收集之前隔离数据库与网络，环境变量同时传递给子进程。"""
import os
from pathlib import Path
import sys
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TEST_ROOT = ROOT / ".pytest_cache" / "isolated"
TEST_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.update(
    AIQUANT_TESTING="1",
    AIQUANT_TEST_ROOT=str(TEST_ROOT),
    AIQUANT_DB_PATH=str(TEST_ROOT / f"collection-{uuid.uuid4().hex}.db"),
    AIQUANT_SCHEDULER_ENABLED="0",
    AIQUANT_AUTO_SYNC_ENABLED="0",
    AIQUANT_QLIB_ENABLED="0",
)
from config.settings import install_test_guards
install_test_guards()


def pytest_configure(config):
    config.option.basetemp = str(TEST_ROOT / f"run-{uuid.uuid4().hex}")
    config.addinivalue_line("markers", "browser: 独立浏览器测试作业")


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    import core.db as db
    from config import settings
    path = str(tmp_path / "isolated.db")
    monkeypatch.setenv("AIQUANT_DB_PATH", path)
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setattr(settings, "DB_PATH", path)
    db.init_db()
    from core import em_guard
    monkeypatch.setattr(em_guard, "_DB_PATH", tmp_path / "em_guard.db")
    em_guard._ensure_db()
    from config.strategy_params import invalidate_param_cache
    invalidate_param_cache()
    yield path
    invalidate_param_cache()
