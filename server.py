"""V121 Cloud — baccarat decision service.

Deployable on a single Tencent Cloud CVM. Run behind nginx + HTTPS.

Hard requirements before running:
  export V121_API_KEY="$(openssl rand -hex 24)"      # required, no default
  export V121_DB_PATH="/opt/v121/v121.db"            # optional, defaults shown below
  export V121_TERMINAL_HTML="/opt/v121/terminal.html"# optional

Strategy logic mirrors the legacy V121_CLOUD_FINAL but with the correctness
fixes from prior review: env-based key, hmac.compare_digest, WAL SQLite,
per-table asyncio lock, decision_id idempotency, banker 5% commission,
TIE pushes B/P bets, phase counted by B/P only.

This is NOT a profit guarantee. Baccarat B/P is statistically near-iid;
no threshold tuning produces positive expectation against house edge.
The service is built for honest record-keeping and risk control, not
predictive alpha.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import math
import os
import secrets
import sqlite3
import time
from collections import defaultdict
from contextlib import contextmanager
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

VERSION = "V121_CLOUD_2"

API_KEY = os.environ.get("V121_API_KEY")
if not API_KEY:
    raise RuntimeError(
        "V121_API_KEY environment variable is required. "
        "Generate one with: openssl rand -hex 24"
    )

DB_PATH = os.environ.get("V121_DB_PATH", "/opt/v121/v121.db")
TERMINAL_HTML_PATH = os.environ.get("V121_TERMINAL_HTML", "/opt/v121/terminal.html")
BANKER_COMMISSION = float(os.environ.get("V121_BANKER_COMMISSION", "0.05"))

CONFIG = {
    "tau_lo": 0.44,
    "tau_hi": 0.56,
    "collect_min": 10,
    "base_bet": 0.3,
    "small_bet_size": 0.1,
    "max_bet": 0.3,
    "score_full": 78,
    "score_small": 42,
    "small_fill_lo": 0.46,
    "small_fill_hi": 0.54,
    "stop_loss": -4.0,
    "stop_win": 999.0,
    "freeze_threshold": 2,
    "freeze_duration": 3,
    "reverse_enabled": False,
    "pred_mode": "LOW_ONLY",
    "mono_block_opposite": True,
    "phase1_hands": 20,
    "phase2_hands": 40,
    "cal_shrink": 0.85,
}

# In-memory cache of table state, hydrated from DB on first access.
TABLES: dict[str, dict] = {}
TABLE_LOCKS: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

app = FastAPI(title="V121 Cloud", version=VERSION)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with _connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          REAL NOT NULL,
                table_id    TEXT NOT NULL,
                shoe_id     INTEGER NOT NULL,
                hand_no     INTEGER NOT NULL,
                outcome     TEXT NOT NULL,
                sequence    TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_events_table_shoe
                ON events(table_id, shoe_id);

            CREATE TABLE IF NOT EXISTS hands (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts            REAL NOT NULL,
                table_id      TEXT NOT NULL,
                shoe_id       INTEGER NOT NULL,
                hand_no       INTEGER NOT NULL,
                decision_id   TEXT,
                outcome       TEXT,
                result        TEXT,
                decision      TEXT,
                side          TEXT,
                bet_size      REAL,
                phase         TEXT,
                bias          REAL,
                pnl_delta     REAL,
                pnl_running   REAL,
                mode          TEXT,
                score         INTEGER,
                regime        TEXT,
                pred_score    REAL,
                mono_state    TEXT,
                signal_source TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_hands_table_shoe
                ON hands(table_id, shoe_id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_hands_decision_id
                ON hands(decision_id) WHERE decision_id IS NOT NULL;

            CREATE TABLE IF NOT EXISTS shoes (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_start     REAL NOT NULL,
                ts_end       REAL,
                table_id     TEXT NOT NULL,
                shoe_id      INTEGER NOT NULL,
                total_hands  INTEGER NOT NULL,
                wins         INTEGER NOT NULL,
                losses       INTEGER NOT NULL,
                pnl          REAL NOT NULL,
                b_count      INTEGER NOT NULL,
                p_count      INTEGER NOT NULL,
                t_count      INTEGER NOT NULL,
                sequence     TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_shoes_table
                ON shoes(table_id, shoe_id);

            CREATE TABLE IF NOT EXISTS table_state (
                table_id TEXT PRIMARY KEY,
                state    TEXT NOT NULL,
                updated  REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS pending_decisions (
                decision_id TEXT PRIMARY KEY,
                table_id    TEXT NOT NULL,
                shoe_id     INTEGER NOT NULL,
                hand_no     INTEGER NOT NULL,
                bet_size    REAL NOT NULL,
                side        TEXT NOT NULL,
                created     REAL NOT NULL,
                settled     INTEGER NOT NULL DEFAULT 0
            );
            """
        )


@contextmanager
def db():
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def require_auth(x_api_key: Optional[str] = Header(None)) -> None:
    expected = API_KEY.encode()
    presented = (x_api_key or "").encode()
    # compare_digest requires equal length; pad to avoid early-exit timing
    if len(expected) != len(presented) or not hmac.compare_digest(expected, presented):
        raise HTTPException(status_code=403, detail="forbidden")


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def new_table(table_id: str) -> dict:
    return {
        "table_id": table_id,
        "shoe_id": int(time.time() * 1000),
        "ts_start": time.time(),
        "sequence": [],
        "hand_no": 0,
        "bp_hand_no": 0,
        "wins": 0,
        "losses": 0,
        "shoe_pnl": 0.0,
        "net_pnl": 0.0,
        "b_count": 0,
        "p_count": 0,
        "t_count": 0,
        "freeze": 0,
        "loss_streak": 0,
        "consec_errors": 0,
        "consec_wins": 0,
        "peak_pnl": 0.0,
        "max_dd": 0.0,
        "last_outcome": "",
        "last_decision": {},
    }


def _load_state_from_db(table_id: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT state FROM table_state WHERE table_id = ?", (table_id,)
        ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["state"])
    except Exception:
        return None


def _persist_state(s: dict) -> None:
    payload = json.dumps(s, ensure_ascii=False)
    with db() as conn:
        conn.execute(
            "INSERT INTO table_state(table_id, state, updated) VALUES(?, ?, ?) "
            "ON CONFLICT(table_id) DO UPDATE SET state=excluded.state, updated=excluded.updated",
            (s["table_id"], payload, time.time()),
        )


def get_table(table_id: str) -> dict:
    if table_id in TABLES:
        return TABLES[table_id]
    loaded = _load_state_from_db(table_id)
    TABLES[table_id] = loaded if loaded else new_table(table_id)
    return TABLES[table_id]


# ---------------------------------------------------------------------------
# Strategy primitives (pure functions of state)
# ---------------------------------------------------------------------------

def phase_of(bp_hand_no: int) -> str:
    if bp_hand_no <= CONFIG["phase1_hands"]:
        return "EXPLORE"
    if bp_hand_no <= CONFIG["phase2_hands"]:
        return "ANALYZE"
    return "HARVEST"


def bp_only(seq: list[str]) -> list[str]:
    return [x for x in seq if x in ("B", "P")]


def calc_bias(seq: list[str]) -> float:
    arr = bp_only(seq)[-20:]
    if len(arr) < 8:
        return 0.5
    return arr.count("B") / len(arr)


def calc_regime(seq: list[str]) -> str:
    arr = bp_only(seq)[-18:]
    if len(arr) < 12:
        return "MIXED"
    b_rate = arr.count("B") / len(arr)
    sw = sum(1 for i in range(len(arr) - 1) if arr[i] != arr[i + 1])
    sw_rate = sw / max(1, len(arr) - 1)
    if b_rate >= 0.68:
        return "TREND_B"
    if b_rate <= 0.32:
        return "TREND_P"
    if sw_rate >= 0.66:
        return "OSC"
    if 0.42 <= b_rate <= 0.58:
        return "CHAOS"
    return "MIXED"


def calc_pred(seq: list[str]) -> tuple[float, float]:
    arr = bp_only(seq)[-24:]
    if len(arr) < 8:
        return 0.5, 1.0
    b = arr.count("B")
    n = len(arr)
    p_raw = (b + 1) / (n + 2)
    p_cal = 0.5 + (p_raw - 0.5) * CONFIG["cal_shrink"]
    edge = abs(p_cal - 0.5) * 2
    pred_score = max(0.0, min(1.0, 1.0 - edge))
    return p_cal, pred_score


def mono_state_of(seq: list[str], side: str) -> str:
    arr = bp_only(seq)[-24:]
    if len(arr) < 16 or side not in ("B", "P"):
        return "MONO_INACTIVE"
    b_rate = arr.count("B") / len(arr)
    if b_rate >= 0.62:
        dom = "B"
    elif b_rate <= 0.38:
        dom = "P"
    else:
        return "MONO_INACTIVE"
    return "MONO_SAME" if side == dom else "MONO_OPPOSITE"


def score_calc(p_cal: float, ph: str, bias: float, regime: str,
               pred_score: float, mono: str) -> int:
    edge = abs(p_cal - 0.5) * 2
    sc = edge * 40
    sc += {"EXPLORE": 10, "ANALYZE": 25, "HARVEST": 40}.get(ph, 0)
    sc += abs(bias - 0.5) * 2 * 18
    sc += {"CHAOS": 8, "OSC": 6, "TREND_B": -4, "TREND_P": -4}.get(regime, 0)
    if mono == "MONO_SAME":
        sc += 6
    elif mono == "MONO_OPPOSITE":
        sc -= 30
    if pred_score < 0.50:
        sc += (0.50 - pred_score) * 30
    return max(0, min(100, int(sc)))


def decide(state: dict) -> dict:
    seq = state["sequence"]
    bp_n = state["bp_hand_no"]
    ph = phase_of(bp_n)
    bias = calc_bias(seq)
    regime = calc_regime(seq)

    base = {
        "version": VERSION,
        "phase": ph,
        "bias": bias,
        "hand_no": state["hand_no"],
        "bp_hand_no": bp_n,
        "shoe_pnl": state["shoe_pnl"],
        "consec_errors": state["consec_errors"],
        "consec_wins": state["consec_wins"],
        "mode": "PRUNED",
        "regime": regime,
    }

    def wait(mode: str, msg: str, extra: Optional[dict] = None) -> dict:
        out = {
            **base,
            "decision": "WAIT",
            "side": "",
            "confidence": 0,
            "bet_size": 0,
            "mode": mode,
            "message": msg,
            "score": 0,
            "pred_score": 1,
            "mono_state": "MONO_INACTIVE",
            "signal_source": "NONE",
        }
        if extra:
            out.update(extra)
        return out

    if state["freeze"] > 0:
        return wait("FREEZE", f"frozen {state['freeze']}")

    if state["shoe_pnl"] <= CONFIG["stop_loss"]:
        return wait("STOP_LOSS", "stop-loss tripped")

    arr = bp_only(seq)
    if len(arr) < CONFIG["collect_min"]:
        return wait("COLLECTING", f"collecting {len(arr)}/{CONFIG['collect_min']}")

    p_cal, pred_score = calc_pred(seq)
    side = ""
    confidence = 0.0
    source = "NONE"
    if p_cal >= CONFIG["tau_hi"]:
        side, confidence, source = "B", p_cal, "PREDICT"
    elif p_cal <= CONFIG["tau_lo"]:
        side, confidence, source = "P", 1 - p_cal, "PREDICT"
    elif CONFIG["small_fill_lo"] <= p_cal <= CONFIG["small_fill_hi"]:
        side = "B" if p_cal >= 0.5 else "P"
        confidence = 0.5
        source = "SMALL_BET"

    if not side:
        return wait("NORMAL", "no signal", {"pred_score": pred_score})

    mono = mono_state_of(seq, side)

    if CONFIG["mono_block_opposite"] and mono == "MONO_OPPOSITE":
        return wait("MONO_BLOCK", "MONO_OPPOSITE blocked",
                    {"pred_score": pred_score, "mono_state": mono,
                     "signal_source": "MONO_BLOCK"})

    if CONFIG["pred_mode"] == "LOW_ONLY" and pred_score >= 0.50:
        return wait("PRED_MODE_BLOCK", f"LOW_ONLY blocked {pred_score:.3f}",
                    {"pred_score": pred_score, "mono_state": mono,
                     "signal_source": "PRED_MODE_BLOCK"})

    sc = score_calc(p_cal, ph, bias, regime, pred_score, mono)

    if ph == "EXPLORE":
        return {
            **base,
            "decision": "SMALL",
            "side": side,
            "confidence": confidence,
            "bet_size": CONFIG["small_bet_size"],
            "mode": "EXPLORE",
            "message": "explore small bet",
            "score": sc,
            "pred_score": pred_score,
            "mono_state": mono,
            "signal_source": source,
        }

    if sc >= CONFIG["score_full"]:
        decision = "FULL"
        bet = CONFIG["base_bet"]
    elif sc >= CONFIG["score_small"]:
        decision = "SMALL"
        bet = CONFIG["small_bet_size"]
    else:
        return wait("LOW_SCORE", f"score insufficient {sc}",
                    {"score": sc, "pred_score": pred_score,
                     "mono_state": mono, "signal_source": source})

    bet = min(bet, CONFIG["max_bet"])

    return {
        **base,
        "decision": decision,
        "side": side,
        "confidence": confidence,
        "bet_size": bet,
        "mode": source,
        "message": f"{regime} {mono}",
        "score": sc,
        "pred_score": pred_score,
        "mono_state": mono,
        "signal_source": source,
    }


def wilson(wins: int, total: int) -> tuple[float, float]:
    if total <= 0:
        return 0.0, 0.0
    z = 1.96
    p = wins / total
    n = total
    a = p + z * z / (2 * n)
    b = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    c = 1 + z * z / n
    return (a - b) / c, (a + b) / c


# ---------------------------------------------------------------------------
# Persistence helpers (called inside per-table lock)
# ---------------------------------------------------------------------------

def save_event(s: dict, outcome: str) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO events(ts, table_id, shoe_id, hand_no, outcome, sequence) "
            "VALUES(?,?,?,?,?,?)",
            (time.time(), s["table_id"], s["shoe_id"], s["hand_no"], outcome,
             "".join(s["sequence"])),
        )


def save_hand(s: dict, outcome: str, result: str, d: dict,
              decision_id: Optional[str], pnl_delta: float) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO hands(ts, table_id, shoe_id, hand_no, decision_id, "
            "outcome, result, decision, side, bet_size, phase, bias, "
            "pnl_delta, pnl_running, mode, score, regime, pred_score, "
            "mono_state, signal_source) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                time.time(), s["table_id"], s["shoe_id"], s["hand_no"],
                decision_id, outcome, result,
                d.get("decision", "WAIT"), d.get("side", ""),
                float(d.get("bet_size", 0)), d.get("phase", ""),
                float(d.get("bias", 0.5)), float(pnl_delta),
                float(s["shoe_pnl"]), d.get("mode", ""),
                int(d.get("score", 0)), d.get("regime", ""),
                float(d.get("pred_score", 0)), d.get("mono_state", ""),
                d.get("signal_source", ""),
            ),
        )


def save_shoe(s: dict) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO shoes(ts_start, ts_end, table_id, shoe_id, total_hands, "
            "wins, losses, pnl, b_count, p_count, t_count, sequence) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                s["ts_start"], time.time(), s["table_id"], s["shoe_id"],
                s["hand_no"], s["wins"], s["losses"], s["shoe_pnl"],
                s["b_count"], s["p_count"], s["t_count"],
                "".join(s["sequence"]),
            ),
        )


def issue_decision_id(s: dict, d: dict) -> Optional[str]:
    if d.get("decision") in (None, "", "WAIT"):
        return None
    decision_id = secrets.token_hex(12)
    with db() as conn:
        conn.execute(
            "INSERT INTO pending_decisions(decision_id, table_id, shoe_id, "
            "hand_no, bet_size, side, created, settled) "
            "VALUES(?,?,?,?,?,?,?,0)",
            (decision_id, s["table_id"], s["shoe_id"], s["hand_no"],
             float(d.get("bet_size", 0)), d.get("side", ""), time.time()),
        )
    return decision_id


def claim_decision(decision_id: str, table_id: str) -> dict:
    """Atomically mark a pending decision as settled. Returns its row."""
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM pending_decisions WHERE decision_id = ?",
            (decision_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="unknown decision_id")
        if row["table_id"] != table_id:
            raise HTTPException(status_code=409, detail="decision_id table mismatch")
        if row["settled"]:
            raise HTTPException(status_code=409, detail="decision_id already settled")
        conn.execute(
            "UPDATE pending_decisions SET settled = 1 WHERE decision_id = ?",
            (decision_id,),
        )
        return dict(row)


# ---------------------------------------------------------------------------
# API models
# ---------------------------------------------------------------------------

class OutcomeReq(BaseModel):
    table_id: str
    outcome: str = Field(..., pattern="^[BPTbpt]$")


class SettleReq(BaseModel):
    table_id: str
    decision_id: str
    result: str = Field(..., pattern="^(WIN|LOSS|TIE|win|loss|tie)$")


class NewShoeReq(BaseModel):
    table_id: str


class RollbackReq(BaseModel):
    table_id: str


class HBReq(BaseModel):
    table_id: str


class TuneReq(BaseModel):
    tau_lo: Optional[float] = Field(None, gt=0, lt=1)
    tau_hi: Optional[float] = Field(None, gt=0, lt=1)
    collect_min: Optional[int] = Field(None, ge=0, le=200)
    base_bet: Optional[float] = Field(None, ge=0, le=10)
    small_bet_size: Optional[float] = Field(None, ge=0, le=10)
    max_bet: Optional[float] = Field(None, ge=0, le=10)
    score_full: Optional[int] = Field(None, ge=0, le=100)
    score_small: Optional[int] = Field(None, ge=0, le=100)
    small_fill_lo: Optional[float] = Field(None, gt=0, lt=1)
    small_fill_hi: Optional[float] = Field(None, gt=0, lt=1)
    stop_loss: Optional[float] = None
    stop_win: Optional[float] = None
    pred_mode: Optional[str] = Field(None, pattern="^(LOW_ONLY|OFF)$")
    mono_block_opposite: Optional[bool] = None


# ---------------------------------------------------------------------------
# Mutators (require lock + state persist)
# ---------------------------------------------------------------------------

def _apply_outcome(s: dict, outcome: str) -> dict:
    s["sequence"].append(outcome)
    s["hand_no"] += 1
    s["last_outcome"] = outcome
    if outcome == "B":
        s["b_count"] += 1
        s["bp_hand_no"] += 1
    elif outcome == "P":
        s["p_count"] += 1
        s["bp_hand_no"] += 1
    else:
        s["t_count"] += 1
    save_event(s, outcome)
    d = decide(s)
    decision_id = issue_decision_id(s, d)
    if decision_id:
        d = {**d, "decision_id": decision_id}
    s["last_decision"] = d
    save_hand(s, outcome, "", d, decision_id, 0.0)
    _persist_state(s)
    return d


def _apply_settlement(s: dict, decision_id: str, result: str) -> dict:
    pending = claim_decision(decision_id, s["table_id"])
    bet = float(pending["bet_size"])
    side = pending["side"]
    delta = 0.0

    if result == "WIN":
        s["wins"] += 1
        s["consec_wins"] += 1
        s["consec_errors"] = 0
        s["loss_streak"] = 0
        if side == "B":
            delta = bet * (1 - BANKER_COMMISSION)
        else:
            delta = bet
    elif result == "LOSS":
        s["losses"] += 1
        s["consec_wins"] = 0
        s["consec_errors"] += 1
        s["loss_streak"] += 1
        delta = -bet
        if s["loss_streak"] >= CONFIG["freeze_threshold"]:
            s["freeze"] = CONFIG["freeze_duration"]
    else:  # TIE — B/P bets push: no PnL, neither win nor loss
        delta = 0.0

    s["shoe_pnl"] += delta
    s["net_pnl"] += delta
    if s["net_pnl"] > s["peak_pnl"]:
        s["peak_pnl"] = s["net_pnl"]
    dd = s["peak_pnl"] - s["net_pnl"]
    if dd > s["max_dd"]:
        s["max_dd"] = dd
    if s["freeze"] > 0 and result != "LOSS":
        s["freeze"] = max(0, s["freeze"] - 1)

    d = decide(s)
    new_decision_id = issue_decision_id(s, d)
    if new_decision_id:
        d = {**d, "decision_id": new_decision_id}
    s["last_decision"] = d
    save_hand(s, s.get("last_outcome", ""), result, d, new_decision_id, delta)
    _persist_state(s)
    return d


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.get("/")
def root():
    return {
        "status": "ok",
        "version": VERSION,
        "message": "V121 Cloud running",
    }


@app.get("/healthz")
def healthz():
    return {"ok": True, "version": VERSION, "ts": time.time()}


@app.post("/v100/heartbeat")
async def heartbeat(req: HBReq, _: None = Depends(require_auth)):
    s = get_table(req.table_id)
    return {
        "status": "ok",
        "version": VERSION,
        "table_id": req.table_id,
        "shoe_id": s["shoe_id"],
        "hand_no": s["hand_no"],
        "shoe_pnl": s["shoe_pnl"],
        "freeze": s["freeze"],
        "consec_errors": s["consec_errors"],
        "consec_wins": s["consec_wins"],
    }


@app.post("/v100/outcome")
async def post_outcome(req: OutcomeReq, _: None = Depends(require_auth)):
    async with TABLE_LOCKS[req.table_id]:
        s = get_table(req.table_id)
        return _apply_outcome(s, req.outcome.upper())


@app.post("/v100/settle")
async def post_settle(req: SettleReq, _: None = Depends(require_auth)):
    async with TABLE_LOCKS[req.table_id]:
        s = get_table(req.table_id)
        return _apply_settlement(s, req.decision_id, req.result.upper())


@app.post("/v100/new_shoe")
async def post_new_shoe(req: NewShoeReq, _: None = Depends(require_auth)):
    async with TABLE_LOCKS[req.table_id]:
        s = get_table(req.table_id)
        if s["hand_no"] > 0:
            save_shoe(s)
        TABLES[req.table_id] = new_table(req.table_id)
        s = TABLES[req.table_id]
        d = decide(s)
        s["last_decision"] = d
        _persist_state(s)
        return d


@app.post("/v100/rollback")
async def post_rollback(req: RollbackReq, _: None = Depends(require_auth)):
    async with TABLE_LOCKS[req.table_id]:
        s = get_table(req.table_id)
        if not s["sequence"]:
            return {"status": "noop", "hand_no": 0, "sequence": ""}
        last = s["sequence"].pop()
        s["hand_no"] = max(0, s["hand_no"] - 1)
        if last == "B":
            s["b_count"] = max(0, s["b_count"] - 1)
            s["bp_hand_no"] = max(0, s["bp_hand_no"] - 1)
        elif last == "P":
            s["p_count"] = max(0, s["p_count"] - 1)
            s["bp_hand_no"] = max(0, s["bp_hand_no"] - 1)
        elif last == "T":
            s["t_count"] = max(0, s["t_count"] - 1)
        d = decide(s)
        s["last_decision"] = d
        _persist_state(s)
        return {
            "status": "ok",
            "hand_no": s["hand_no"],
            "sequence": "".join(s["sequence"]),
            "decision": d,
        }


@app.post("/ai/tune")
async def tune(req: TuneReq, _: None = Depends(require_auth)):
    data = req.model_dump(exclude_none=True)
    if "tau_lo" in data and "tau_hi" in data and data["tau_lo"] >= data["tau_hi"]:
        raise HTTPException(400, "tau_lo must be < tau_hi")
    if "score_small" in data and "score_full" in data \
            and data["score_small"] > data["score_full"]:
        raise HTTPException(400, "score_small must be <= score_full")
    CONFIG.update(data)
    return {"status": "ok", "version": VERSION, "config": CONFIG}


@app.get("/ai/report")
async def report(_: None = Depends(require_auth)):
    total_bets = sum(s["wins"] + s["losses"] for s in TABLES.values())
    wins = sum(s["wins"] for s in TABLES.values())
    losses = sum(s["losses"] for s in TABLES.values())
    net_pnl = sum(s["net_pnl"] for s in TABLES.values())
    wr = wins / total_bets if total_bets else 0.0
    wl, wu = wilson(wins, total_bets)

    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(pnl),0), COALESCE(AVG(pnl),0) FROM shoes"
        ).fetchone()

    return {
        "version": VERSION,
        "total_bets": total_bets,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wr, 4),
        "wilson_lower": round(wl, 4),
        "wilson_upper": round(wu, 4),
        "net_pnl": round(net_pnl, 4),
        "max_drawdown": round(max([s["max_dd"] for s in TABLES.values()] or [0]), 4),
        "total_shoes": row[0],
        "shoes_pnl": round(row[1], 4),
        "avg_shoe_pnl": round(row[2], 4),
        "config": CONFIG,
    }


@app.get("/ai/shoes")
async def shoes(_: None = Depends(require_auth)):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, table_id, shoe_id, total_hands, wins, losses, pnl, "
            "b_count, p_count, t_count, sequence FROM shoes "
            "ORDER BY id DESC LIMIT 50"
        ).fetchall()
    return {
        "version": VERSION,
        "shoes": [
            {
                "id": r["id"], "table_id": r["table_id"], "shoe_id": r["shoe_id"],
                "hands": r["total_hands"], "wins": r["wins"], "losses": r["losses"],
                "pnl": round(r["pnl"], 4), "b_count": r["b_count"],
                "p_count": r["p_count"], "t_count": r["t_count"],
                "sequence": r["sequence"],
            }
            for r in rows
        ],
    }


@app.get("/ai/export_shoes")
async def export_shoes(_: None = Depends(require_auth)):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, sequence FROM shoes ORDER BY id ASC"
        ).fetchall()
    text = "\n".join(f"{r['id']}.{r['sequence']}" for r in rows)
    return {"version": VERSION, "count": len(rows), "text": text}


@app.get("/terminal", response_class=HTMLResponse)
def terminal():
    if os.path.exists(TERMINAL_HTML_PATH):
        with open(TERMINAL_HTML_PATH, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse(
        "<h1>terminal.html missing</h1>"
        f"<p>Place terminal.html at {TERMINAL_HTML_PATH} or set V121_TERMINAL_HTML.</p>",
        status_code=404,
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "server:app",
        host=os.environ.get("V121_HOST", "127.0.0.1"),
        port=int(os.environ.get("V121_PORT", "8000")),
        workers=1,  # state is in-process; do NOT scale workers without Redis
    )
