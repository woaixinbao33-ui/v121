"""V121 Cloud — admin router (5 桌锁定 / CSV 导出 / 限流登录).

This module is intentionally decoupled from server.py to avoid circular imports.
server.py imports `admin_router` and `is_table_locked`; admin.py imports nothing
from server.py.

Hardenings vs. the original draft:
1. Password is env-required (no `"000000"` default; min 6 chars).
2. Sessions are GC'd on every check (no unbounded memory growth).
3. Per-IP failure window resets after 5 minutes (no permanent lock-out).
4. JSONResponse(content=..., status_code=...) ordering fixed.
5. CSV filenames carry today's date so repeated exports don't collide.
6. `unlock_all` emergency route to recover from "all 5 tables locked".
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import os
import sqlite3
import time
import traceback

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

admin_router = APIRouter()

_LOCKED_TABLES: set[str] = set()
_ADMIN_SESSIONS: dict[str, float] = {}
_FAIL_COUNT: dict[str, tuple[int, float]] = {}

DB_PATH = os.environ.get("V121_DB_PATH", "/opt/v121/v121.db")
ADMIN_HTML_PATH = os.environ.get("V121_ADMIN_HTML", "/opt/v121/admin.html")

ADMIN_PWD = os.environ.get("V121_ADMIN_PASSWORD")
if not ADMIN_PWD or len(ADMIN_PWD) < 6:
    raise RuntimeError(
        "V121_ADMIN_PASSWORD 环境变量必填且至少 6 位。"
        "示例：export V121_ADMIN_PASSWORD=\"$(openssl rand -hex 6)\""
    )

SESSION_TTL = 3600 * 8  # 8 小时
FAIL_WINDOW = 300       # 5 分钟
FAIL_LIMIT = 5
KNOWN_TABLES = ("T1", "T2", "T3", "T4", "T5")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_table_locked(table_id: str) -> bool:
    """Read-only probe used by server.py mutators."""
    return table_id in _LOCKED_TABLES


def _gc_sessions(now: float) -> None:
    expired = [t for t, exp in _ADMIN_SESSIONS.items() if exp <= now]
    for t in expired:
        _ADMIN_SESSIONS.pop(t, None)


def _gc_fail_counts(now: float) -> None:
    expired = [ip for ip, (_, first) in _FAIL_COUNT.items() if now - first >= FAIL_WINDOW * 2]
    for ip in expired:
        _FAIL_COUNT.pop(ip, None)


def _new_token(ip: str) -> str:
    return hashlib.sha256(
        f"{ip}{time.time()}{os.urandom(16).hex()}".encode()
    ).hexdigest()


def _check(req: Request) -> None:
    now = time.time()
    _gc_sessions(now)
    t = req.cookies.get("adm_token", "")
    if not t or _ADMIN_SESSIONS.get(t, 0) <= now:
        raise HTTPException(401, "未登录或会话已过期")


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def _row_get(row: sqlite3.Row, *keys, default=None):
    """Return the first non-null value among `keys` (schema-tolerant)."""
    available = set(row.keys())
    for k in keys:
        if k in available and row[k] is not None:
            return row[k]
    return default


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@admin_router.post("/admin/login")
async def admin_login(req: Request) -> Response:
    ip = req.client.host if req.client else "unknown"
    now = time.time()
    _gc_fail_counts(now)
    cnt, first = _FAIL_COUNT.get(ip, (0, now))
    # Reset window if the first failure was over FAIL_WINDOW ago.
    if now - first >= FAIL_WINDOW:
        cnt, first = 0, now
    if cnt >= FAIL_LIMIT:
        raise HTTPException(429, f"失败次数过多，{FAIL_WINDOW // 60} 分钟后再试")
    try:
        body = await req.json()
    except Exception:
        body = {}
    pwd = (body.get("password") or "").strip()
    expected = ADMIN_PWD.encode()
    presented = pwd.encode()
    if len(expected) != len(presented) or not hmac.compare_digest(expected, presented):
        _FAIL_COUNT[ip] = (cnt + 1, first)
        raise HTTPException(403, "密码错误")
    _FAIL_COUNT.pop(ip, None)
    token = _new_token(ip)
    _ADMIN_SESSIONS[token] = now + SESSION_TTL
    resp = Response('{"ok":true}', media_type="application/json")
    resp.set_cookie(
        "adm_token", token,
        httponly=True, max_age=SESSION_TTL, samesite="lax", path="/admin"
    )
    return resp


@admin_router.post("/admin/logout")
async def admin_logout(req: Request) -> Response:
    _ADMIN_SESSIONS.pop(req.cookies.get("adm_token", ""), None)
    resp = Response('{"ok":true}', media_type="application/json")
    resp.delete_cookie("adm_token", path="/admin")
    return resp


# ---------------------------------------------------------------------------
# Data routes
# ---------------------------------------------------------------------------

@admin_router.get("/admin/api/overview")
async def admin_overview(req: Request):
    try:
        _check(req)
        conn = _db()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(hands)").fetchall()}
        ts_col = "ts" if "ts" in cols else "id"
        pnl_col = "pnl_delta" if "pnl_delta" in cols else (
            "pnl" if "pnl" in cols else None
        )
        today = time.strftime("%Y-%m-%d")
        if pnl_col:
            pnl_sum = (
                f"SUM(CASE WHEN date({ts_col},'unixepoch','localtime')=? "
                f"THEN {pnl_col} ELSE 0 END)"
            )
            args = (today, today)
        else:
            pnl_sum = "0"
            args = (today,)
        q = (
            "SELECT table_id, COUNT(*) AS total, "
            f"SUM(CASE WHEN date({ts_col},'unixepoch','localtime')=? THEN 1 ELSE 0 END) AS th, "
            f"{pnl_sum} AS tp, "
            f"MAX({ts_col}) AS last_ts "
            "FROM hands GROUP BY table_id"
        )
        rows = conn.execute(q, args).fetchall()
        conn.close()
        tables = []
        for r in rows:
            tables.append({
                "table_id": r["table_id"],
                "locked": r["table_id"] in _LOCKED_TABLES,
                "today_hands": r["th"] or 0,
                "today_pnl": round(r["tp"] or 0.0, 2),
                "last_active": r["last_ts"],
                "total_hands": r["total"],
            })
        seen = {t["table_id"] for t in tables}
        for tid in KNOWN_TABLES:
            if tid not in seen:
                tables.append({
                    "table_id": tid,
                    "locked": tid in _LOCKED_TABLES,
                    "today_hands": 0,
                    "today_pnl": 0,
                    "last_active": None,
                    "total_hands": 0,
                })
        tables.sort(key=lambda x: x["table_id"])
        return {"tables": tables, "ts": time.time()}
    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse(
            content={"error": str(e), "trace": traceback.format_exc()},
            status_code=500,
        )


@admin_router.get("/admin/api/table/{table_id}")
async def admin_table_detail(table_id: str, req: Request):
    try:
        _check(req)
        if table_id not in KNOWN_TABLES and not table_id.startswith("T"):
            raise HTTPException(400, "桌台ID不合法")
        conn = _db()
        rows = conn.execute(
            "SELECT * FROM hands WHERE table_id = ? ORDER BY id DESC LIMIT 100",
            (table_id,),
        ).fetchall()
        conn.close()
        result = []
        for r in rows:
            result.append({
                "outcome": _row_get(r, "outcome", default="") or "",
                "result": _row_get(r, "result", default="") or "",
                "decision": _row_get(r, "decision", default="") or "",
                "bet_side": _row_get(r, "side", "bet_side", default="") or "",
                "pnl": round(_row_get(r, "pnl_delta", "pnl", default=0) or 0, 2),
                "p_cal": round(_row_get(r, "pred_score", "p_cal", default=0.5) or 0.5, 3),
                "ts": _row_get(r, "ts", default=0),
                "shoe_id": _row_get(r, "shoe_id", default=0),
                "regime": _row_get(r, "regime", default="") or "",
                "score": _row_get(r, "score", default=0) or 0,
            })
        return {
            "table_id": table_id,
            "locked": table_id in _LOCKED_TABLES,
            "hands": result,
        }
    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse(
            content={"error": str(e), "trace": traceback.format_exc()},
            status_code=500,
        )


@admin_router.post("/admin/api/lock/{table_id}")
async def admin_lock(table_id: str, req: Request):
    _check(req)
    _LOCKED_TABLES.add(table_id)
    return {"ok": True, "locked": True, "table_id": table_id}


@admin_router.post("/admin/api/unlock/{table_id}")
async def admin_unlock(table_id: str, req: Request):
    _check(req)
    _LOCKED_TABLES.discard(table_id)
    return {"ok": True, "locked": False, "table_id": table_id}


@admin_router.post("/admin/api/unlock_all")
async def admin_unlock_all(req: Request):
    """应急路由：一键解锁全部桌台，避免 5 桌都被锁导致自锁。"""
    _check(req)
    cleared = sorted(_LOCKED_TABLES)
    _LOCKED_TABLES.clear()
    return {"ok": True, "cleared": cleared}


@admin_router.get("/admin/api/locked")
async def admin_locked(req: Request):
    _check(req)
    return {"locked": sorted(_LOCKED_TABLES)}


@admin_router.get("/admin/api/export/{table_id}")
async def admin_export(table_id: str, req: Request):
    _check(req)
    conn = _db()
    rows = conn.execute(
        "SELECT * FROM hands WHERE table_id = ? ORDER BY id ASC",
        (table_id,),
    ).fetchall()
    conn.close()
    today = time.strftime("%Y%m%d")
    filename = f"{table_id}_hands_{today}.csv"
    if not rows:
        return StreamingResponse(
            io.BytesIO("无数据\n".encode("utf-8-sig")),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )
    keys = list(rows[0].keys())
    buf = io.StringIO()
    # ﻿ BOM 让 Excel 正确识别 UTF-8 中文。
    buf.write("﻿")
    writer = csv.writer(buf)
    writer.writerow(keys)
    for r in rows:
        writer.writerow(["" if r[k] is None else r[k] for k in keys])
    payload = buf.getvalue().encode("utf-8")
    return StreamingResponse(
        io.BytesIO(payload),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@admin_router.get("/admin", response_class=HTMLResponse)
async def admin_page():
    if os.path.exists(ADMIN_HTML_PATH):
        with open(ADMIN_HTML_PATH, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse(
        "<h1>admin.html 未部署</h1>"
        f"<p>请把 admin.html 放到 {ADMIN_HTML_PATH}，或设置 V121_ADMIN_HTML。</p>",
        status_code=404,
    )
