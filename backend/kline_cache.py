"""
本地 SQLite 快取：K棒與資金費率。
歷史（已收盤）資料一旦抓過就不必再打 Binance；最新幾根K棒每次仍會重新抓取，
因為當根K棒在收盤前資料會持續變動。
"""
import asyncio
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple

DB_PATH = Path(__file__).parent / "data" / "market_cache.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS klines (
            symbol   TEXT NOT NULL,
            interval TEXT NOT NULL,
            time     INTEGER NOT NULL,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            PRIMARY KEY (symbol, interval, time)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS funding_rates (
            symbol TEXT NOT NULL,
            time   INTEGER NOT NULL,
            rate   REAL,
            PRIMARY KEY (symbol, time)
        )
    """)
    return conn


def _upsert_klines_sync(symbol: str, interval: str, klines: List[Dict]) -> None:
    if not klines:
        return
    conn = _connect()
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO klines (symbol, interval, time, open, high, low, close, volume) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [(symbol, interval, k["time"], k["open"], k["high"], k["low"], k["close"], k["volume"]) for k in klines],
        )
        conn.commit()
    finally:
        conn.close()


def _read_klines_sync(symbol: str, interval: str, limit: int) -> List[Dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT time, open, high, low, close, volume FROM klines "
            "WHERE symbol=? AND interval=? ORDER BY time DESC LIMIT ?",
            (symbol, interval, limit),
        ).fetchall()
    finally:
        conn.close()
    rows.reverse()
    return [{"time": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[5]} for r in rows]


def _count_and_earliest_sync(symbol: str, interval: str) -> Tuple[int, Optional[int]]:
    conn = _connect()
    try:
        count, min_t = conn.execute(
            "SELECT COUNT(*), MIN(time) FROM klines WHERE symbol=? AND interval=?",
            (symbol, interval),
        ).fetchone()
    finally:
        conn.close()
    return count or 0, min_t


def _upsert_funding_sync(symbol: str, rates: Dict[int, float]) -> None:
    if not rates:
        return
    conn = _connect()
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO funding_rates (symbol, time, rate) VALUES (?,?,?)",
            [(symbol, t, r) for t, r in rates.items()],
        )
        conn.commit()
    finally:
        conn.close()


def _read_funding_sync(symbol: str, start_time: int, end_time: int) -> Dict[int, float]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT time, rate FROM funding_rates WHERE symbol=? AND time>=? AND time<=?",
            (symbol, start_time, end_time),
        ).fetchall()
    finally:
        conn.close()
    return {r[0]: r[1] for r in rows}


async def upsert_klines(symbol: str, interval: str, klines: List[Dict]) -> None:
    await asyncio.to_thread(_upsert_klines_sync, symbol, interval, klines)


async def read_klines(symbol: str, interval: str, limit: int) -> List[Dict]:
    return await asyncio.to_thread(_read_klines_sync, symbol, interval, limit)


async def count_and_earliest(symbol: str, interval: str) -> Tuple[int, Optional[int]]:
    return await asyncio.to_thread(_count_and_earliest_sync, symbol, interval)


async def upsert_funding(symbol: str, rates: Dict[int, float]) -> None:
    await asyncio.to_thread(_upsert_funding_sync, symbol, rates)


async def read_funding(symbol: str, start_time: int, end_time: int) -> Dict[int, float]:
    return await asyncio.to_thread(_read_funding_sync, symbol, start_time, end_time)
