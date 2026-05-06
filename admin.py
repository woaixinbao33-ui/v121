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
import json
import math
import os
import sqlite3
import time
import traceback
import zipfile

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


# ---------------------------------------------------------------------------
# 回测 + 自动驯化（核心：扫 hands 表，按信号源/体制/可预测分桶/Mono 分组）
# ---------------------------------------------------------------------------

def _wilson(wins: int, total: int) -> tuple[float, float]:
    if total <= 0:
        return 0.0, 0.0
    z = 1.96
    p = wins / total
    a = p + z * z / (2 * total)
    b = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    c = 1 + z * z / total
    return (a - b) / c, (a + b) / c


def _pred_bucket(p) -> str:
    if p is None:
        return "未知"
    try:
        v = float(p)
    except Exception:
        return "未知"
    if v < 0.35:
        return "<0.35"
    if v < 0.50:
        return "0.35-0.50"
    if v < 0.72:
        return "0.50-0.72"
    return ">=0.72"


def compute_backtest(table_id: str | None = None,
                     since_ts: float | None = None) -> dict:
    """扫描 hands 表，产出与截图格式一致的回测结果 dict。

    参数：
        table_id: 仅统计该桌；None=全部
        since_ts: 仅统计 >= 该时间戳；None=全历史
    """
    conn = _db()
    where = "result IN ('WIN','LOSS','TIE')"
    args: list = []
    if table_id:
        where += " AND table_id = ?"
        args.append(table_id)
    if since_ts is not None:
        where += " AND ts >= ?"
        args.append(float(since_ts))
    rows = conn.execute(
        f"SELECT * FROM hands WHERE {where} ORDER BY id ASC", tuple(args)
    ).fetchall()

    # 总手数（用于下注率分母 = 包含和牌的全部手数）
    where_all = "1=1"
    args_all: list = []
    if table_id:
        where_all += " AND table_id = ?"
        args_all.append(table_id)
    if since_ts is not None:
        where_all += " AND ts >= ?"
        args_all.append(float(since_ts))
    n_hands_total = conn.execute(
        f"SELECT COUNT(*) FROM hands WHERE {where_all}", tuple(args_all)
    ).fetchone()[0]

    # 鞋数（distinct shoe_id）
    n_shoes = conn.execute(
        f"SELECT COUNT(DISTINCT shoe_id) FROM hands WHERE {where_all}",
        tuple(args_all),
    ).fetchone()[0]
    conn.close()

    # 仅算 WIN/LOSS（TIE 推注 PnL=0，但计入注次以反映实际下注次数）
    bet_rows = [r for r in rows if r["result"] in ("WIN", "LOSS")
                and (r["bet_size"] or 0) > 0]
    n_bets = len(bet_rows)
    wins = sum(1 for r in bet_rows if r["result"] == "WIN")
    losses = sum(1 for r in bet_rows if r["result"] == "LOSS")
    pnl = sum((r["pnl_delta"] or 0.0) for r in bet_rows)
    win_rate = wins / n_bets if n_bets else 0.0
    wl, wu = _wilson(wins, n_bets)

    # 最大回撤
    cum, peak, max_dd = 0.0, 0.0, 0.0
    for r in bet_rows:
        cum += r["pnl_delta"] or 0.0
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd

    def group_by(field: str, bucket_fn=None) -> list[dict]:
        groups: dict[str, dict] = {}
        for r in bet_rows:
            raw = r[field] if field in r.keys() else None
            key = bucket_fn(raw) if bucket_fn else (raw or "未知")
            g = groups.setdefault(str(key), {"bets": 0, "wins": 0,
                                             "losses": 0, "pnl": 0.0})
            g["bets"] += 1
            if r["result"] == "WIN":
                g["wins"] += 1
            elif r["result"] == "LOSS":
                g["losses"] += 1
            g["pnl"] += r["pnl_delta"] or 0.0
        out = []
        for k, v in sorted(groups.items(), key=lambda x: -x[1]["bets"]):
            wr = v["wins"] / v["bets"] if v["bets"] else 0.0
            out.append({
                "label": k,
                "bets": v["bets"],
                "wins": v["wins"],
                "losses": v["losses"],
                "wr": round(wr, 4),
                "pnl": round(v["pnl"], 4),
                "verdict": "盈" if v["pnl"] > 0 else ("亏" if v["pnl"] < 0 else "平"),
            })
        return out

    return {
        "version": os.environ.get("V121_VERSION", "V121_CLOUD_APEX_PRO_4"),
        "computed_at": time.time(),
        "filter": {"table_id": table_id, "since_ts": since_ts},
        "overall": {
            "shoes": n_shoes,
            "hands_total": n_hands_total,
            "bets": n_bets,
            "wins": wins,
            "losses": losses,
            "win_rate": round(win_rate, 4),
            "wilson_lower": round(wl, 4),
            "wilson_upper": round(wu, 4),
            "pnl": round(pnl, 4),
            "pnl_per_shoe": round(pnl / n_shoes, 4) if n_shoes else 0.0,
            "bets_per_shoe": round(n_bets / n_shoes, 2) if n_shoes else 0.0,
            "bet_rate": round(n_bets / n_hands_total, 4) if n_hands_total else 0.0,
            "max_drawdown": round(max_dd, 4),
        },
        "source_stats": group_by("signal_source"),
        "regime_stats": group_by("regime"),
        "pred_stats": group_by("pred_score", _pred_bucket),
        "mono_stats": group_by("mono_state"),
    }


def format_backtest_report(r: dict, title: str = "BASE_CONFIG 回测结果") -> str:
    """把回测结果格式化为截图里的 Pythonista 风格文本。"""
    o = r["overall"]
    bar = "=" * 60
    sep = "-" * 60
    lines = [
        bar, title, bar,
        f"靴:{o['shoes']} 手:{o['hands_total']} 注:{o['bets']} "
        f"胜:{o['wins']} 负:{o['losses']}",
        f"胜率:{o['win_rate']:.4f}   "
        f"Wilson下界:{o['wilson_lower']:.4f}   Wilson上界:{o['wilson_upper']:.4f}",
        f"总PnL:{o['pnl']:.4f}   每靴PnL:{o['pnl_per_shoe']:.4f}   "
        f"每靴下注:{o['bets_per_shoe']}   下注率:{o['bet_rate']:.4f}   "
        f"最大回撤:{o['max_drawdown']:.4f}",
        "",
    ]

    def render_group(name: str, group: list[dict]) -> list[str]:
        out = [sep, f"{name}", sep]
        if not group:
            out.append("  (空)")
            return out
        for g in group:
            out.append(f"{g['label']:<20s}      bets={g['bets']:<6d} wins={g['wins']:<6d}")
            out.append(f"  wr={g['wr']:<7.4f}  pnl={g['pnl']:<7.2f}    {g['verdict']}")
        out.append("")
        return out

    lines += render_group("信号来源 source_stats", r["source_stats"])
    lines += render_group("体制 regime_stats", r["regime_stats"])
    lines += render_group("可预测分 pred_stats", r["pred_stats"])
    lines += render_group("Monolith mono_stats", r["mono_stats"])
    lines += [bar, "运行完成。请把以上完整输出复制发给 AI 大模型。", bar]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# BP 训练数据导出（供本地 Pythonista / AI 大模型分析）
# ---------------------------------------------------------------------------

def _bp_only(seq: str) -> str:
    """从序列字符串中过滤出仅含 B/P 的部分。"""
    return "".join(c for c in (seq or "").upper() if c in ("B", "P"))


def _shoes_rows(table_id: str | None = None) -> list[sqlite3.Row]:
    conn = _db()
    if table_id:
        rows = conn.execute(
            "SELECT id, table_id, shoe_id, ts_start, total_hands, "
            "b_count, p_count, t_count, sequence "
            "FROM shoes WHERE table_id = ? ORDER BY id ASC",
            (table_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, table_id, shoe_id, ts_start, total_hands, "
            "b_count, p_count, t_count, sequence "
            "FROM shoes ORDER BY id ASC",
        ).fetchall()
    conn.close()
    return rows


def _bp_training_text(rows: list[sqlite3.Row], title_extra: str = "") -> str:
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    head = "V121 BP 训练数据" + (f" — 台桌: {title_extra}" if title_extra else "")
    lines = [
        f"# {head}",
        f"# 导出时间: {now_str}",
        f"# 总靴数: {len(rows)}",
        f"# 格式: 序号.台桌.日期.BP序列（已过滤和牌 T）",
        "# " + "─" * 50,
    ]
    for idx, r in enumerate(rows, 1):
        bp = _bp_only(r["sequence"] or "")
        date_str = time.strftime("%Y-%m-%d", time.localtime(r["ts_start"] or 0))
        lines.append(f"{idx:04d}.{r['table_id']}.{date_str}.{bp}")
    return "\n".join(lines) + "\n"


def _bp_training_tsv(rows: list[sqlite3.Row]) -> str:
    header = "\t".join([
        "序号", "台桌", "鞋ID", "日期", "总手数",
        "庄数", "闲数", "和数", "BP序列",
    ])
    out = ["﻿" + header]  # UTF-8 BOM
    for idx, r in enumerate(rows, 1):
        bp = _bp_only(r["sequence"] or "")
        date_str = time.strftime("%Y-%m-%d", time.localtime(r["ts_start"] or 0))
        out.append("\t".join([
            str(idx), r["table_id"], str(r["shoe_id"]), date_str,
            str(r["total_hands"]), str(r["b_count"]),
            str(r["p_count"]), str(r["t_count"]), bp,
        ]))
    return "\n".join(out) + "\n"


@admin_router.get("/admin/api/export_bp_training")
async def admin_export_bp_full(req: Request):
    _check(req)
    rows = _shoes_rows()
    content = _bp_training_text(rows)
    filename = f"bp_training_{time.strftime('%Y%m%d_%H%M%S')}.txt"
    return StreamingResponse(
        io.BytesIO(content.encode("utf-8")),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@admin_router.get("/admin/api/export_bp_training/{table_id}")
async def admin_export_bp_table(table_id: str, req: Request):
    _check(req)
    rows = _shoes_rows(table_id)
    content = _bp_training_text(rows, title_extra=table_id)
    filename = f"bp_{table_id}_{time.strftime('%Y%m%d_%H%M%S')}.txt"
    return StreamingResponse(
        io.BytesIO(content.encode("utf-8")),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@admin_router.get("/admin/api/export_bp_tsv")
async def admin_export_bp_tsv(req: Request):
    _check(req)
    rows = _shoes_rows()
    content = _bp_training_tsv(rows)
    filename = f"bp_training_{time.strftime('%Y%m%d_%H%M%S')}.tsv"
    return StreamingResponse(
        io.BytesIO(content.encode("utf-8")),
        media_type="text/tab-separated-values; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ---------------------------------------------------------------------------
# 回测路由 + 训练记录
# ---------------------------------------------------------------------------

@admin_router.get("/admin/api/backtest")
async def admin_backtest(req: Request, table_id: str | None = None,
                         since_ts: float | None = None):
    _check(req)
    return compute_backtest(table_id=table_id, since_ts=since_ts)


@admin_router.get("/admin/api/backtest/report")
async def admin_backtest_report(req: Request, table_id: str | None = None):
    _check(req)
    r = compute_backtest(table_id=table_id)
    title = "BASE_CONFIG 回测结果"
    if table_id:
        title += f" — {table_id}"
    text = format_backtest_report(r, title=title)
    filename = f"backtest_report_{time.strftime('%Y%m%d_%H%M%S')}.txt"
    return StreamingResponse(
        io.BytesIO(text.encode("utf-8")),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@admin_router.post("/admin/api/training_run")
async def admin_training_run(req: Request):
    """手动触发一次回测并存表，返回 run_id 与 overall 摘要。"""
    _check(req)
    try:
        body = await req.json()
    except Exception:
        body = {}
    table_id = body.get("table_id")
    r = compute_backtest(table_id=table_id)
    overall_json = json.dumps(r["overall"], ensure_ascii=False)
    full_json = json.dumps(r, ensure_ascii=False)
    conn = _db()
    cur = conn.execute(
        "INSERT INTO training_runs(ts, trigger, shoe_count, table_id, "
        "overall_json, full_json) VALUES(?,?,?,?,?,?)",
        (time.time(), "manual", r["overall"]["shoes"], table_id,
         overall_json, full_json),
    )
    conn.commit()
    run_id = cur.lastrowid
    conn.close()
    return {"ok": True, "run_id": run_id, "overall": r["overall"]}


@admin_router.get("/admin/api/training_runs")
async def admin_training_runs_list(req: Request, limit: int = 30):
    _check(req)
    limit = max(1, min(200, int(limit)))
    conn = _db()
    rows = conn.execute(
        "SELECT id, ts, trigger, shoe_count, table_id, overall_json "
        "FROM training_runs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        try:
            overall = json.loads(r["overall_json"])
        except Exception:
            overall = {}
        out.append({
            "id": r["id"], "ts": r["ts"], "trigger": r["trigger"],
            "shoe_count": r["shoe_count"], "table_id": r["table_id"],
            "overall": overall,
        })
    return {"runs": out, "count": len(out)}


@admin_router.get("/admin/api/training_runs/{run_id}")
async def admin_training_run_detail(run_id: int, req: Request):
    _check(req)
    conn = _db()
    row = conn.execute(
        "SELECT * FROM training_runs WHERE id = ?", (run_id,)
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "训练记录不存在")
    try:
        full = json.loads(row["full_json"])
    except Exception:
        full = {}
    return {
        "id": row["id"], "ts": row["ts"], "trigger": row["trigger"],
        "shoe_count": row["shoe_count"], "table_id": row["table_id"],
        "result": full,
    }


# ---------------------------------------------------------------------------
# 训练包：把 JSON / 文本 / CSV / 配置快照 / AI 提示词模板 打成 ZIP
# ---------------------------------------------------------------------------

_AI_PROMPT_TEMPLATE = """\
# V121 百家乐决策系统 · AI 优化建议请求

请你扮演职业级量化决策系统的领域巨擘，基于附带的真实回测数据为我提出
**具体的、可粘贴执行的 CONFIG 调参建议**。

## 我的目标

1. 在不破坏长期 EV 与可审计性的前提下，提高高质量候选窗口的命中率。
2. 收敛最大回撤，控制单靴 / 单日风险暴露上限。
3. 识别哪些 signal_source / regime / pred_score 桶 / mono 状态长期亏损，建议封禁或降权。

## 你拿到的文件

- `overall.json`     — 总体回测（靴/手/注/胜/负/胜率/Wilson 区间/PnL/最大回撤）
- `by_source.json`   — 按 signal_source 分组的 bets / wins / wr / pnl
- `by_regime.json`   — 按 regime 分组（TREND_B/P / OSC / CHAOS / MIXED）
- `by_pred_bucket.json` — 按 pred_score 分桶（<0.35 / 0.35-0.50 / 0.50-0.72 / >=0.72）
- `by_mono.json`     — MONO_SAME / MONO_OPPOSITE / MONO_INACTIVE
- `bp_training.txt`  — 全靴 BP 序列（每行 序号.台桌.日期.序列）
- `bp_training.tsv`  — 同上，带元数据列（适合 pandas / Excel）
- `recent_hands.csv` — 最近若干手原始流水
- `config.json`      — 当前生产环境的全部 CONFIG
- `backtest_report.txt` — 上面 4 个 stats 的人类可读汇总（与终端截图同格式）

## 我需要你输出（请按这个结构）

### 1. 一句话诊断
当前系统最大的结构性问题是什么？

### 2. 必改清单（对应 /ai/tune 的 POST body）
```
{
  "<config_key_1>": <new_value>,
  ...
}
```
每个键给出：旧值、新值、为什么改（基于哪条数据）。

### 3. 应急封禁（哪些 signal_source / regime / pred 桶 应当列入黑名单？）
对每条给出 wr、pnl、Wilson 上界，论证「长期亏损」非「短期方差」。

### 4. 风险治理建议
- pro_kelly_fraction 是否应该收紧？
- pro_max_shoe_risk / pro_max_daily_risk 当前是否过松？
- 是否建议开启 pro_drift_enabled / pro_meta_enabled？

### 5. 不能改的红线
明确哪些不能调（比如 BANKER_COMMISSION 是赌场规则，不是参数）。

## 重要原则

- B/P 数学期望恒小于 0。任何阈值组合都不会改变赌场边际，**不要承诺胜率提升**。
- 你的建议是在重新分配方差与暴露，目标是「同样亏损下更小的回撤」「同样下注次数下更高的命中率」。
- 如果数据样本太小（注次 < 200），明确说「样本不足以下结论」而不是给建议。

请给出建议。
"""


def make_training_bundle(table_id: str | None = None) -> bytes:
    """打包 ZIP，返回 bytes。所有内容 UTF-8 编码，带 BOM 的 CSV/TSV 让 Excel 可读。"""
    bt = compute_backtest(table_id=table_id)
    rows = _shoes_rows(table_id=table_id)

    # 最近 hands 流水（最多 500 行）
    conn = _db()
    where = "1=1"
    args: list = []
    if table_id:
        where += " AND table_id = ?"
        args.append(table_id)
    hand_rows = conn.execute(
        f"SELECT * FROM hands WHERE {where} ORDER BY id DESC LIMIT 500",
        tuple(args),
    ).fetchall()
    conn.close()
    hand_rows = list(reversed(hand_rows))  # 时间正序

    # CSV with BOM
    csv_buf = io.StringIO()
    csv_buf.write("﻿")
    if hand_rows:
        keys = list(hand_rows[0].keys())
        writer = csv.writer(csv_buf)
        writer.writerow(keys)
        for r in hand_rows:
            writer.writerow(["" if r[k] is None else r[k] for k in keys])
    else:
        csv_buf.write("空\n")

    # CONFIG 快照（lazy 取）
    try:
        from server import CONFIG as _SERVER_CONFIG  # type: ignore
        config_snap = json.dumps(_SERVER_CONFIG, ensure_ascii=False, indent=2)
    except Exception:
        config_snap = json.dumps({"note": "CONFIG 不可读取，请手动从 /ai/config 抓取"},
                                 ensure_ascii=False, indent=2)

    # ZIP 打包
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("overall.json",
                   json.dumps(bt["overall"], ensure_ascii=False, indent=2))
        z.writestr("by_source.json",
                   json.dumps(bt["source_stats"], ensure_ascii=False, indent=2))
        z.writestr("by_regime.json",
                   json.dumps(bt["regime_stats"], ensure_ascii=False, indent=2))
        z.writestr("by_pred_bucket.json",
                   json.dumps(bt["pred_stats"], ensure_ascii=False, indent=2))
        z.writestr("by_mono.json",
                   json.dumps(bt["mono_stats"], ensure_ascii=False, indent=2))
        z.writestr("backtest_full.json",
                   json.dumps(bt, ensure_ascii=False, indent=2))
        title = "BASE_CONFIG 回测结果"
        if table_id:
            title += f" — {table_id}"
        z.writestr("backtest_report.txt", format_backtest_report(bt, title))
        z.writestr("bp_training.txt", _bp_training_text(rows))
        z.writestr("bp_training.tsv", _bp_training_tsv(rows))
        z.writestr("recent_hands.csv", csv_buf.getvalue())
        z.writestr("config.json", config_snap)
        z.writestr("ai_prompt.md", _AI_PROMPT_TEMPLATE)
        z.writestr("README.txt", _BUNDLE_README.format(
            table_id=table_id or "全部",
            ts=time.strftime("%Y-%m-%d %H:%M:%S"),
            n_shoes=bt["overall"]["shoes"],
            n_bets=bt["overall"]["bets"],
        ))
    zip_buf.seek(0)
    return zip_buf.getvalue()


_BUNDLE_README = """\
V121 训练驯化数据包
========================================
导出时间: {ts}
台桌过滤: {table_id}
鞋数: {n_shoes}   下注数: {n_bets}

文件清单:
  overall.json          总体回测（含 Wilson 置信区间）
  by_source.json        信号来源分组（PREDICT / SMALL_BET / ...）
  by_regime.json        体制分组（TREND_B/P / OSC / CHAOS / MIXED）
  by_pred_bucket.json   可预测分桶（<0.35 / 0.35-0.50 / 0.50-0.72 / >=0.72）
  by_mono.json          Monolith 状态（SAME / OPPOSITE / INACTIVE）
  backtest_full.json    上述 5 项的完整结构
  backtest_report.txt   人类可读汇总（与手机终端截图同格式）
  bp_training.txt       全靴 BP 序列（每行: 序号.台桌.日期.BPBPB...）
  bp_training.tsv       同上 + 元数据列（pandas/Excel 可读）
  recent_hands.csv      最近 500 手原始流水
  config.json           当前 CONFIG 快照
  ai_prompt.md          发给 ChatGPT/Claude 的标准提问模板

把这个包发给 AI 大模型（ChatGPT/Claude/Gemini）的步骤:
  1. 解压本 ZIP
  2. 打开 ai_prompt.md，复制全部内容到 AI 对话框
  3. 上传以下文件作为附件: overall.json, by_source.json, by_regime.json,
     by_pred_bucket.json, by_mono.json, backtest_report.txt, config.json
  4. 等待 AI 输出建议清单
  5. 在 V121 调参控制台 /console 把建议的 CONFIG 改动逐项填入并保存

注意:
  - B/P 数学期望恒小于 0。任何 AI 建议都不会改变赌场边际。
  - AI 给的建议是用来「重新分配方差」的，不要追求胜率本身。
  - 样本数 < 200 注时，AI 建议大概率是噪声，建议忽略。
"""


@admin_router.get("/admin/api/training_bundle")
async def admin_training_bundle(req: Request, table_id: str | None = None):
    """下载训练驯化数据包（ZIP），可选 ?table_id=T1 仅打包单桌。"""
    _check(req)
    payload = make_training_bundle(table_id=table_id)
    suffix = f"_{table_id}" if table_id else ""
    filename = f"v121_training_bundle{suffix}_{time.strftime('%Y%m%d_%H%M%S')}.zip"
    return StreamingResponse(
        io.BytesIO(payload),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


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
