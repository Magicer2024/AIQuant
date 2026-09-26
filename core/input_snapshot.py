"""内容寻址的运行输入：规范 JSONL、压缩去重、哈希验证与显式重放。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import sqlite3
import re
import zlib
from contextlib import contextmanager
from typing import Mapping


def canonical_json(value):
    """不序列化 NaN/Infinity；稳定排序，避免字典或数据库返回顺序影响版本。"""
    def normalize(item):
        if isinstance(item, Mapping):
            return {str(k): normalize(v) for k, v in item.items()}
        if isinstance(item, (list, tuple)):
            return [normalize(v) for v in item]
        if isinstance(item, (date, datetime)):
            return item.isoformat()
        if hasattr(item, "item"):
            return normalize(item.item())
        if isinstance(item, float) and not math.isfinite(item):
            return None
        return item
    return json.dumps(normalize(value), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


def content_hash(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class SnapshotUnavailable(ValueError):
    pass


_INPUT_TABLES = {"daily_price", "index_daily", "stock_info", "stock_lhb_detail", "strategy_rules", "market_snapshot", "market_breadth"}


def capture_inputs(conn, scan_date, codes, *, source="stock_signal", history_mode=False,
                   lookback=300, store=None, reuse_scores=True):
    """调用方同一读事务内冻结实际窗口；全市场门控单独保存，不被局部股票池替换。"""
    from datetime import timedelta
    from itertools import groupby
    store = store or SnapshotStore()
    codes = sorted(set(codes))
    schemas, blocks = {}, []
    cutoff = (date.fromisoformat(scan_date) - timedelta(days=2600)).isoformat()
    primary_cutoff = (date.fromisoformat(scan_date) - timedelta(days=700)).isoformat()

    def save_rows(table, rows, columns):
        schemas[table] = columns
        any_rows = False
        for day, group in groupby(rows, lambda r: dict(r).get("trade_date", scan_date)):
            # 每个日期分块，数据库游标流式读取，不在内存复制全市场历史。
            blocks.append(store.put(table, day, group))
            any_rows = True
        if not any_rows:
            blocks.append(store.put(table, scan_date, []))

    for table in sorted(_INPUT_TABLES - {"market_snapshot", "market_breadth"}):
        info = list(conn.execute(f'PRAGMA table_info("{table}")'))
        if not info:
            raise SnapshotUnavailable(f"输入表缺失：{table}")
        ignored = {"created_at", "updated_at"}
        if table == "daily_price" and not reuse_scores:
            ignored.update({"vol_score", "ma_score", "diverge_score", "bottom_score", "whale_score", "fusion_score"})
        columns = [(r["name"], r["type"]) for r in info if r["name"] not in ignored]
        names = ",".join(f'"{n}"' for n, _ in columns)
        # 使用 JSON 数组避免 SQLite 参数数量限制，代码全部是绑定值。
        subset = "code IN (SELECT value FROM json_each(?))"
        args = []
        where = "1=1"
        if table == "daily_price":
            where = f"{subset} AND trade_date<=?"
            args = [canonical_json(codes), scan_date]
            if source in {"stock_deep_signal", "strategy_signals"}:
                if lookback and lookback > 0:
                    sql = f"SELECT {names} FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY code ORDER BY trade_date DESC) AS input_row FROM daily_price WHERE {where}) WHERE input_row<=? ORDER BY trade_date,code"
                    args.append(lookback)
                else:
                    sql = f"SELECT {names} FROM daily_price WHERE {where} ORDER BY trade_date,code"
            else:
                if not history_mode:
                    # 与旧增量窗口、稀疏历史回退及筹码补读一致。
                    where += " AND (trade_date>=? OR code IN (SELECT code FROM daily_price WHERE trade_date BETWEEN ? AND ? GROUP BY code HAVING COUNT(*)<260))"
                    args += [cutoff, primary_cutoff, scan_date]
                sql = f"SELECT {names} FROM daily_price WHERE {where} ORDER BY trade_date,code"
        else:
            if table == "stock_info":
                where, args = subset, [canonical_json(codes)]
            elif table == "stock_lhb_detail":
                where, args = f"trade_date=? AND {subset}", [scan_date, canonical_json(codes)]
            elif table == "index_daily":
                where, args = "trade_date<=? AND trade_date>=?", [scan_date, primary_cutoff]
            elif table == "strategy_rules":
                where = "is_active=1"
            order = "trade_date,code" if "trade_date" in dict(columns) else ("code" if "code" in dict(columns) else "id")
            sql = f"SELECT {names} FROM {table} WHERE {where} ORDER BY {order}"
        save_rows(table, conn.execute(sql, args), columns)
    # 市场宽度必须保存全市场原始输入，局部运行不能把自选涨跌当成大盘。
    save_rows("market_breadth", conn.execute(
        "SELECT code,trade_date,close,pct_change FROM daily_price "
        "WHERE trade_date BETWEEN date(?,'-30 days') AND ? ORDER BY trade_date,code",
        (scan_date, scan_date)),
        [("code", "TEXT"), ("trade_date", "TEXT"), ("close", "REAL"), ("pct_change", "REAL")])
    row = conn.execute("SELECT AVG(pct_change) AS avg_pct,COUNT(*) AS stocks FROM daily_price WHERE trade_date=?", (scan_date,)).fetchone()
    save_rows("market_snapshot", [{"trade_date": scan_date, **dict(row)}],
              [("trade_date", "TEXT"), ("avg_pct", "REAL"), ("stocks", "INTEGER")])
    return {"format": "jsonl-gzip-v1", "blocks": blocks, "schemas": schemas,
            "source": source, "history_mode": history_mode, "lookback": lookback}


class SnapshotStore:
    def __init__(self, root=None):
        from config.settings import BASE_DIR, TESTING
        if root is None and TESTING:
            from core.db import DB_PATH
            root = Path(DB_PATH).parent / "data_snapshots"
        self.root = Path(root or Path(BASE_DIR) / "data_snapshots")

    def _path(self, digest):
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise SnapshotUnavailable("非法快照哈希")
        return self.root / digest[:2] / f"{digest}.jsonl.gz"

    def put(self, dataset, data_date, rows):
        lines = sorted(canonical_json(dict(row)) for row in rows)
        raw = ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        target = self._path(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            fd, name = tempfile.mkstemp(dir=target.parent, suffix=".pending")
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(gzip.compress(raw, mtime=0))
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(name, target)  # 独占发布；并发相同内容不覆盖旧块
                except FileExistsError:
                    pass
            finally:
                Path(name).unlink(missing_ok=True)
        descriptor = {"dataset": dataset, "date": data_date, "sha256": digest, "rows": len(lines)}
        self.read(descriptor)
        return descriptor

    def read(self, descriptor):
        path = self._path(descriptor["sha256"])
        try:
            raw = gzip.decompress(path.read_bytes())
        except (OSError, EOFError, zlib.error) as exc:
            raise SnapshotUnavailable(f"快照缺失或损坏：{descriptor['sha256']}") from exc
        if hashlib.sha256(raw).hexdigest() != descriptor["sha256"]:
            raise SnapshotUnavailable("快照内容校验失败，不允许回退到当前数据")
        rows = [json.loads(line) for line in raw.splitlines()]
        if len(rows) != descriptor["rows"]:
            raise SnapshotUnavailable("快照行数校验失败")
        return rows

    def verify(self, manifest):
        if (manifest.get("format") != "jsonl-gzip-v1"
                or not isinstance(manifest.get("blocks"), list) or not manifest["blocks"]):
            raise SnapshotUnavailable("缺少可重放的输入清单")
        for descriptor in manifest["blocks"]:
            self.read(descriptor)


@contextmanager
def open_snapshot(manifest, store=None):
    """只由内容块重建临时只读输入库；不执行清单携带的 SQL，不回退当前行情。"""
    store = store or SnapshotStore()
    store.verify(manifest)
    schemas = manifest.get("schemas") or {}
    if set(schemas) != _INPUT_TABLES:
        raise SnapshotUnavailable("输入表结构不完整")
    if {b.get("dataset") for b in manifest["blocks"]} != set(schemas):
        raise SnapshotUnavailable("输入数据集内容块不完整")
    store.root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="input-", dir=store.root) as directory:
        conn = sqlite3.connect(str(Path(directory) / "input.db"))
        conn.row_factory = sqlite3.Row
        try:
            for table, columns in schemas.items():
                definitions = []
                for name, kind in columns:
                    if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", name) or kind.upper() not in {"TEXT", "INTEGER", "REAL", "NUMERIC", "BLOB", ""}:
                        raise SnapshotUnavailable("快照列定义非法")
                    definitions.append(f'"{name}" {kind}')
                conn.execute(f'CREATE TABLE "{table}" ({",".join(definitions)})')
            for block in manifest["blocks"]:
                table = block["dataset"]
                if table not in schemas:
                    raise SnapshotUnavailable("未知输入数据集")
                names = [c[0] for c in schemas[table]]
                rows = store.read(block)
                if any(set(r) != set(names) for r in rows):
                    raise SnapshotUnavailable("快照行与列定义不一致")
                conn.executemany(f'INSERT INTO "{table}" VALUES ({",".join("?" for _ in names)})',
                                 [[r[n] for n in names] for r in rows])
            conn.execute("CREATE UNIQUE INDEX idx_input_price ON daily_price(code,trade_date)")
            conn.execute("CREATE INDEX idx_input_index ON index_daily(code,trade_date)")
            conn.execute("CREATE INDEX idx_input_info ON stock_info(code)")
            conn.commit()
            conn.execute("PRAGMA query_only=ON")
            yield conn
        finally:
            conn.close()


def code_version(root=None):
    from config.settings import BASE_DIR
    root = Path(root or BASE_DIR)
    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.DEVNULL)
    try:
        commit = git("rev-parse", "HEAD").decode().strip()
        digest = hashlib.sha256(git("diff", "HEAD", "--binary"))
        for name in sorted(git("ls-files", "--others", "--exclude-standard", "-z").split(b"\0")):
            if not name:
                continue
            path = root / os.fsdecode(name)
            if path.suffix in {".py", ".html", ".css", ".js", ".toml", ".yml"}:
                digest.update(name)
                digest.update(path.read_bytes())
        return {"commit": commit, "workspace_sha256": digest.hexdigest()}
    except (OSError, subprocess.CalledProcessError):
        raise SnapshotUnavailable("无法确定代码版本；请提供显式版本快照")


@dataclass(frozen=True)
class RunContext:
    """JSON 字符串作为深冻结边界；属性返回副本，不能改变运行内配置。"""
    scan_date: str
    as_of: str
    run_type: str
    scope: str
    scope_json: str
    params_json: str
    code_version_json: str
    manifest_json: str
    quality_json: str

    @property
    def parameters(self):
        return json.loads(self.params_json)

    @property
    def key(self):
        # 完成状态与错误是运行结果，不得参与输入身份，否则失败重试会静默换身份。
        return content_hash({k: v for k, v in self.__dict__.items() if k != "quality_json"})

    @classmethod
    def create(cls, *, scan_date, as_of, scope, codes, parameters, manifest,
               version=None, quality=None, run_type="live"):
        date.fromisoformat(scan_date)
        date.fromisoformat(as_of[:10])
        if as_of[:10] > scan_date or run_type not in {"live", "replay", "legacy_import"}:
            raise ValueError("运行日期或类型不合法")
        if scope not in {"all", "watchlist", "single", "deep", "rules", "legacy"}:
            raise ValueError("必须明确运行范围")
        if run_type == "live" and scan_date != date.today().isoformat():
            run_type = "replay"
        manifest = json.loads(canonical_json(manifest))
        if isinstance(manifest.get("blocks"), list):
            manifest["blocks"] = sorted(manifest["blocks"], key=canonical_json)
        return cls(scan_date, as_of, run_type, scope, canonical_json(sorted(set(codes))),
                   canonical_json(parameters), canonical_json(version or code_version()),
                   canonical_json(manifest), canonical_json(quality or {}))
