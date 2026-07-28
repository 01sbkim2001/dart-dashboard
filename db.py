"""로컬 SQLite에 DART 원본 응답과 파싱된 재무제표 라인아이템을 누적 저장한다."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

DB_PATH = Path(__file__).parent / "data" / "dart.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS fetch_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    corp_code TEXT NOT NULL,
    corp_name TEXT NOT NULL,
    stock_code TEXT,
    bsns_year TEXT NOT NULL,
    reprt_code TEXT NOT NULL,
    reprt_name TEXT,
    fs_div TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    UNIQUE(corp_code, bsns_year, reprt_code, fs_div)
);

CREATE TABLE IF NOT EXISTS financial_line_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fetch_id INTEGER NOT NULL REFERENCES fetch_log(id) ON DELETE CASCADE,
    rcept_no TEXT,
    sj_div TEXT,
    sj_nm TEXT,
    account_id TEXT,
    account_nm TEXT,
    thstrm_nm TEXT,
    thstrm_amount TEXT,
    frmtrm_nm TEXT,
    frmtrm_amount TEXT,
    bfefrmtrm_nm TEXT,
    bfefrmtrm_amount TEXT,
    ord TEXT,
    currency TEXT
);
"""


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def save_fetch(
    corp_code: str,
    corp_name: str,
    stock_code: str,
    bsns_year: str,
    reprt_code: str,
    reprt_name: str,
    fs_div: str,
    raw_data: dict,
) -> int:
    """원본 응답(raw_data)과 파싱된 라인아이템을 저장한다. 같은 조회 조건이면 덮어쓴다."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM fetch_log WHERE corp_code=? AND bsns_year=? AND reprt_code=? AND fs_div=?",
            (corp_code, bsns_year, reprt_code, fs_div),
        )
        cur.execute(
            """INSERT INTO fetch_log
               (corp_code, corp_name, stock_code, bsns_year, reprt_code, reprt_name, fs_div, fetched_at, raw_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                corp_code,
                corp_name,
                stock_code,
                bsns_year,
                reprt_code,
                reprt_name,
                fs_div,
                datetime.now().isoformat(timespec="seconds"),
                json.dumps(raw_data, ensure_ascii=False),
            ),
        )
        fetch_id = cur.lastrowid

        rows = raw_data.get("list", [])
        cur.executemany(
            """INSERT INTO financial_line_items
               (fetch_id, rcept_no, sj_div, sj_nm, account_id, account_nm,
                thstrm_nm, thstrm_amount, frmtrm_nm, frmtrm_amount,
                bfefrmtrm_nm, bfefrmtrm_amount, ord, currency)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    fetch_id,
                    r.get("rcept_no"),
                    r.get("sj_div"),
                    r.get("sj_nm"),
                    r.get("account_id"),
                    r.get("account_nm"),
                    r.get("thstrm_nm"),
                    r.get("thstrm_amount"),
                    r.get("frmtrm_nm"),
                    r.get("frmtrm_amount"),
                    r.get("bfefrmtrm_nm"),
                    r.get("bfefrmtrm_amount"),
                    r.get("ord"),
                    r.get("currency"),
                )
                for r in rows
            ],
        )
        conn.commit()
        return fetch_id
    finally:
        conn.close()


def list_fetches(corp_name: str | None = None) -> pd.DataFrame:
    conn = get_connection()
    try:
        query = "SELECT id, corp_name, stock_code, bsns_year, reprt_name, fs_div, fetched_at FROM fetch_log"
        params = ()
        if corp_name:
            query += " WHERE corp_name = ?"
            params = (corp_name,)
        query += " ORDER BY bsns_year DESC, reprt_code DESC, fetched_at DESC"
        return pd.read_sql_query(query, conn, params=params)
    finally:
        conn.close()


def get_line_items(fetch_id: int) -> pd.DataFrame:
    conn = get_connection()
    try:
        return pd.read_sql_query(
            "SELECT * FROM financial_line_items WHERE fetch_id = ? ORDER BY sj_div, ord",
            conn,
            params=(fetch_id,),
        )
    finally:
        conn.close()


def get_all_line_items(corp_name: str | None = None) -> pd.DataFrame:
    """차트/추이 분석용으로 fetch_log와 join된 전체 라인아이템."""
    conn = get_connection()
    try:
        query = """
            SELECT f.corp_name, f.stock_code, f.bsns_year, f.reprt_name, f.fs_div, f.fetched_at,
                   li.sj_div, li.sj_nm, li.account_id, li.account_nm, li.thstrm_nm,
                   li.thstrm_amount, li.frmtrm_amount, li.bfefrmtrm_amount, li.ord
            FROM financial_line_items li
            JOIN fetch_log f ON f.id = li.fetch_id
        """
        params = ()
        if corp_name:
            query += " WHERE f.corp_name = ?"
            params = (corp_name,)
        return pd.read_sql_query(query, conn, params=params)
    finally:
        conn.close()


def delete_fetch(fetch_id: int) -> None:
    conn = get_connection()
    try:
        conn.execute("DELETE FROM fetch_log WHERE id = ?", (fetch_id,))
        conn.commit()
    finally:
        conn.close()
