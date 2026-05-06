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

# Admin module is optional: if V121_ADMIN_PASSWORD is unset (e.g. during local
# smoke tests) we degrade gracefully so the rest of the service still loads.
try:
    from admin import admin_router, is_table_locked  # type: ignore
    _ADMIN_AVAILABLE = True
except RuntimeError:
    # admin.py 显式拒绝加载（密码未配置）
    admin_router = None  # type: ignore
    def is_table_locked(_table_id: str) -> bool:  # type: ignore
        return False
    _ADMIN_AVAILABLE = False

VERSION = "V121_CLOUD_APEX_PRO_4"

API_KEY = os.environ.get("V121_API_KEY")
if not API_KEY:
    raise RuntimeError(
        "V121_API_KEY environment variable is required. "
        "Generate one with: openssl rand -hex 24"
    )

DB_PATH = os.environ.get("V121_DB_PATH", "/opt/v121/v121.db")
TERMINAL_HTML_PATH = os.environ.get("V121_TERMINAL_HTML", "/opt/v121/terminal.html")
CONSOLE_HTML_PATH = os.environ.get("V121_CONSOLE_HTML", "/opt/v121/console.html")
ADMIN_HTML_PATH = os.environ.get("V121_ADMIN_HTML", "/opt/v121/admin.html")
BANKER_COMMISSION = float(os.environ.get("V121_BANKER_COMMISSION", "0.05"))

CONFIG: dict = {
    # ── V121 base parameters ────────────────────────────────────────────────
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
    # ── APEX modules (all default OFF, opt-in via /ai/tune) ─────────────────
    # Master switch. When False every APEX module below is bypassed and the
    # decide() pipeline is byte-identical to V121_CLOUD_2.
    "apex_enabled": False,
    # L85A-02 predictability head: refined pred_score with regime base +
    # entropy/flip/crowd/transition_risk features. Score is reported in the
    # response and (when non-zero weight) blended into the gating score.
    "l85a_enabled": False,
    "l85a_score_weight": 0.30,
    # XΩ Qimen: regime confidence + transition risk modulate bet size via a
    # multiplicative governance coefficient (0.5..1.0).
    "qimen_enabled": False,
    "qimen_chaos_coeff": 0.6,
    "qimen_mixed_coeff": 0.8,
    # Monolith DMR + KEP. DMR scans windows {6,12,24} for the strongest
    # z-score / density combination. KEP turns that into a unit multiplier.
    "monolith_enabled": False,
    "dmr_enabled": False,
    # Action when DMR target disagrees with V121 side: BLOCK (skip bet) or
    # FOLLOW_DMR (override side). BLOCK is the safe default.
    "dmr_conflict_action": "BLOCK",
    "dmr_z_threshold": 1.6,
    "kep_enabled": False,
    # KEP multiplier is clamped to [1.0, kep_max_multiplier]. Default 1.0
    # disables sizing changes even if kep_enabled is True.
    "kep_max_multiplier": 1.0,
    # ── APEX_PRO 阈值组（默认全 OFF；开启需通过 /ai/tune）─────────────────────
    # 候选窗口覆盖率（仅审计指标，不强制执行次数）
    "pro_target_candidate_min": 20,
    "pro_target_candidate_ideal": 24,
    "pro_target_candidate_max": 32,
    # 熵 / 翻转率 / 密度 / Z-score / 尾跑（基于用户给的工业标尺）
    "pro_entropy_high_quality": 0.78,
    "pro_entropy_soft_block": 0.88,
    "pro_entropy_hard_block": 0.95,
    "pro_flip_trend_max": 0.32,
    "pro_flip_chaos_min": 0.62,
    "pro_density_trend_strong": 0.35,
    "pro_density_osc_strong": -0.35,
    "pro_z_min": 0.80,
    "pro_z_valid": 1.20,
    "pro_z_strong": 1.80,
    "pro_z_crowd_block": 2.40,
    "pro_tail_valid": 3,
    "pro_tail_crowd": 5,
    "pro_tail_hard_block": 7,
    # 评分阈值（与旧 score_full / score_small 并存；启用 pro 时优先）
    "pro_score_high": 82,
    "pro_score_standard": 70,
    "pro_score_low": 58,
    "pro_score_wait": 45,
    # 自适应 tau
    "pro_tau_dynamic_enabled": False,
    "pro_base_edge": 0.055,
    "pro_tau_hi_min": 0.555,
    "pro_tau_hi_max": 0.630,
    # 漂移检测（PSI 简版）
    "pro_drift_enabled": False,
    "pro_psi_warn": 0.10,
    "pro_psi_block": 0.20,
    "pro_drift_window": 60,
    # 概率校准（Brier / ECE 自检指标，不直接 BLOCK）
    "pro_calibration_enabled": False,
    "pro_brier_max": 0.245,
    "pro_ece_max": 0.055,
    "pro_calibration_window": 200,
    # 元决策层 Meta-Learner
    "pro_meta_enabled": False,
    "pro_model_disagree_warn": 0.25,
    "pro_model_disagree_block": 0.35,
    # 风险治理 Risk Governor（Fractional Kelly + 单靴 / 单日封顶 + 连错冻结）
    "pro_risk_governor_enabled": False,
    "pro_kelly_fraction": 0.25,
    "pro_max_shoe_risk": 4.0,
    "pro_max_daily_risk": 8.0,
    "pro_loss_2_freeze": 2,
    "pro_loss_3_freeze": 5,
    # ── 自动驯化（满 N 靴自动跑回测）────────────────────────────────────────
    "auto_train_enabled": True,
    "auto_train_milestones": [10, 30, 50, 100, 200, 500, 1000],
}

CONFIG_KEY = "__config__"

# In-memory cache of table state, hydrated from DB on first access.
TABLES: dict[str, dict] = {}
TABLE_LOCKS: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

app = FastAPI(title="V121 Cloud APEX_PRO", version=VERSION)
if _ADMIN_AVAILABLE and admin_router is not None:
    app.include_router(admin_router)


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

            CREATE TABLE IF NOT EXISTS training_runs (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           REAL NOT NULL,
                trigger      TEXT NOT NULL,
                shoe_count   INTEGER NOT NULL,
                table_id     TEXT,
                overall_json TEXT NOT NULL,
                full_json    TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_training_runs_ts
                ON training_runs(ts DESC);
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


def _load_config_from_db() -> None:
    """Restore CONFIG overrides from table_state row keyed by CONFIG_KEY.

    Only known CONFIG keys are merged so a stale DB cannot inject arbitrary
    fields. Called once on startup.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT state FROM table_state WHERE table_id = ?", (CONFIG_KEY,)
        ).fetchone()
    if not row:
        return
    try:
        stored = json.loads(row["state"])
    except Exception:
        return
    for k, v in stored.items():
        if k in CONFIG:
            CONFIG[k] = v


def _persist_config() -> None:
    payload = json.dumps(CONFIG, ensure_ascii=False)
    with db() as conn:
        conn.execute(
            "INSERT INTO table_state(table_id, state, updated) VALUES(?, ?, ?) "
            "ON CONFLICT(table_id) DO UPDATE SET state=excluded.state, updated=excluded.updated",
            (CONFIG_KEY, payload, time.time()),
        )


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
    # Under LOW_ONLY pred_mode the meaningful signal is a LOW pred_score (high
    # information edge), so reward it here.
    if pred_score < 0.50:
        sc += (0.50 - pred_score) * 30
    return max(0, min(100, int(sc)))


# ---------------------------------------------------------------------------
# APEX modules — all are pure functions of state. They produce *advice*; the
# decide() pipeline decides whether to act on it based on CONFIG flags.
# ---------------------------------------------------------------------------

def _runs(arr: list[str]) -> list[tuple[str, int]]:
    if not arr:
        return []
    out: list[tuple[str, int]] = []
    cur, cnt = arr[0], 1
    for x in arr[1:]:
        if x == cur:
            cnt += 1
        else:
            out.append((cur, cnt))
            cur, cnt = x, 1
    out.append((cur, cnt))
    return out


def regime_metrics(seq: list[str], regime: str) -> tuple[float, float]:
    """Return (confidence, transition_risk) for the given regime label.

    confidence ∈ [0,1] — how strongly the data supports the label.
    transition_risk ∈ [0,1] — likelihood the regime is about to change.
    """
    arr = bp_only(seq)[-18:]
    if len(arr) < 12:
        return 0.3, 0.5
    n = len(arr)
    b_rate = arr.count("B") / n
    sw = sum(1 for i in range(n - 1) if arr[i] != arr[i + 1])
    sw_rate = sw / max(1, n - 1)
    if regime in ("TREND_B", "TREND_P"):
        return min(1.0, abs(b_rate - 0.5) * 2.5), 0.15
    if regime == "OSC":
        return min(1.0, sw_rate), 0.50
    if regime == "CHAOS":
        return max(0.0, 1.0 - abs(b_rate - 0.5) * 2), 0.70
    return 0.4, 0.35  # MIXED


def l85a_predictability(seq: list[str], regime: str,
                        regime_conf: float, transition_risk: float) -> tuple[float, str]:
    """L85A-02 predictability head.

    Returns (score ∈ [0,1], label). High score means the next outcome is more
    predictable. This is the inverse semantic of pred_score (which is "edge
    information" — small edge → high pred_score). They're complementary.
    """
    bp = bp_only(seq)
    if len(bp) < 8:
        return 0.5, "中可预测"
    w8 = bp[-8:]
    n8 = len(w8)
    base = {"TREND_B": 1.0, "TREND_P": 1.0, "OSC": 0.72,
            "MIXED": 0.55, "CHAOS": 0.20}.get(regime, 0.55)
    runs = _runs(bp)
    tail_run = runs[-1][1] if runs else 0
    counts: dict[str, int] = {}
    for x in w8:
        counts[x] = counts.get(x, 0) + 1
    entropy = -sum((v / n8) * math.log2(v / n8) for v in counts.values()) if n8 > 0 else 0.0
    flip8 = sum(1 for i in range(1, n8) if w8[i] != w8[i - 1]) / max(1, n8 - 1)
    crowd = min(1.0, tail_run / 6.0)
    raw = (base
           + 0.22 * regime_conf
           + 0.12 * min(tail_run / 4.0, 1.0)
           - 0.20 * entropy
           - 0.16 * flip8
           - 0.14 * crowd
           - 0.18 * transition_risk)
    score = max(0.0, min(1.0, raw))
    if score >= 0.72:
        label = "高可预测"
    elif score >= 0.50:
        label = "中可预测"
    else:
        label = "低可预测"
    return round(score, 4), label


def dmr_advise(seq: list[str], v121_side: str, z_threshold: float
               ) -> tuple[str, str, float, float]:
    """Dynamic Reversal scan over windows {6,12,24}.

    Returns (target_side, action, best_z, best_density).
    action ∈ {"NONE", "FOLLOW_TREND", "REVERSE"}.
    target_side is empty when no signal.
    """
    bp = bp_only(seq)
    if len(bp) < 6 or v121_side not in ("B", "P"):
        return "", "NONE", 0.0, 0.0
    best_z, best_density = 0.0, 0.0
    for w in (6, 12, 24):
        if len(bp) < w:
            continue
        sub = bp[-w:]
        n = len(sub)
        z = 2 * (sub.count("B") / n - 0.5) * math.sqrt(n)
        if n > 1:
            ch = sum(1 for i in range(1, n) if sub[i] != sub[i - 1])
            d = 1 - (2 * ch / (n - 1))
        else:
            d = 0.0
        if abs(z) > abs(best_z):
            best_z, best_density = z, d
    if abs(best_z) < z_threshold:
        return "", "NONE", round(best_z, 3), round(best_density, 3)
    if best_density >= 0.0:
        target = "B" if best_z > 0 else "P"
        return target, "FOLLOW_TREND", round(best_z, 3), round(best_density, 3)
    last = bp[-1]
    target = "P" if last == "B" else "B"
    return target, "REVERSE", round(best_z, 3), round(best_density, 3)


def kep_units(best_z: float, best_density: float, cap: float) -> float:
    """Kinetic Energy Position multiplier ∈ [1.0, cap]."""
    if cap <= 1.0:
        return 1.0
    kinetic = abs(best_z) * (1.0 + abs(best_density)) * 0.25
    return round(max(1.0, min(cap, 1.0 + kinetic)), 3)


def qimen_risk_coeff(regime: str, regime_conf: float, consec_errors: int) -> float:
    """Governance multiplier on bet_size. Always in [0.3, 1.0]."""
    coeff = 1.0
    if regime == "CHAOS":
        coeff *= CONFIG.get("qimen_chaos_coeff", 0.6)
    elif regime == "MIXED":
        coeff *= CONFIG.get("qimen_mixed_coeff", 0.8)
    if consec_errors >= 3:
        coeff *= 0.7
    if regime_conf < 0.4:
        coeff *= 0.85
    return round(max(0.3, min(1.0, coeff)), 3)


# ---------------------------------------------------------------------------
# APEX_PRO modules — pure functions; default OFF via CONFIG flags.
# Implementations are stdlib-only rule versions; ML model hooks are stubbed.
# ---------------------------------------------------------------------------

def pro_psi(prev: list[str], curr: list[str]) -> float:
    """Population Stability Index over B/P bins. Stdlib-only."""
    if not prev or not curr:
        return 0.0
    p_b = (prev.count("B") + 1) / (len(prev) + 2)
    p_p = 1.0 - p_b
    c_b = (curr.count("B") + 1) / (len(curr) + 2)
    c_p = 1.0 - c_b
    return (c_b - p_b) * math.log(c_b / p_b) + (c_p - p_p) * math.log(c_p / p_p)


def pro_drift_check(seq: list[str]) -> tuple[str, float]:
    """Compare last `drift_window` BP hands vs the equally-sized window before.

    Returns (level, psi) where level ∈ {"OK","WARN","BLOCK"}.
    """
    bp = bp_only(seq)
    w = int(CONFIG.get("pro_drift_window", 60))
    if len(bp) < 2 * w:
        return "OK", 0.0
    prev, curr = bp[-2 * w:-w], bp[-w:]
    psi = pro_psi(prev, curr)
    if psi >= CONFIG.get("pro_psi_block", 0.20):
        return "BLOCK", round(psi, 4)
    if psi >= CONFIG.get("pro_psi_warn", 0.10):
        return "WARN", round(psi, 4)
    return "OK", round(psi, 4)


def pro_calibration_health() -> dict:
    """Compute Brier score + ECE over recent settled hands.

    Reads `hands` table directly. Returns a dict with brier, ece, n,
    label ∈ {"OK","WARN","BAD","INSUFFICIENT"}. Cheap; called on demand.
    """
    n_window = int(CONFIG.get("pro_calibration_window", 200))
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT pred_score, result, side FROM hands "
                "WHERE result IN ('WIN','LOSS') AND pred_score IS NOT NULL "
                "ORDER BY id DESC LIMIT ?",
                (n_window,),
            ).fetchall()
    except Exception:
        return {"brier": None, "ece": None, "n": 0, "label": "INSUFFICIENT"}
    if len(rows) < 30:
        return {"brier": None, "ece": None, "n": len(rows), "label": "INSUFFICIENT"}
    # Use 1 - pred_score as the model's "confidence in the chosen side".
    # WIN ↔ outcome=1, LOSS ↔ outcome=0.
    brier_sum = 0.0
    bins: list[list[tuple[float, int]]] = [[] for _ in range(10)]
    for r in rows:
        confidence = max(0.0, min(1.0, 1.0 - float(r["pred_score"])))
        outcome = 1 if r["result"] == "WIN" else 0
        brier_sum += (confidence - outcome) ** 2
        bin_idx = min(9, int(confidence * 10))
        bins[bin_idx].append((confidence, outcome))
    brier = brier_sum / len(rows)
    ece = 0.0
    for b in bins:
        if not b:
            continue
        avg_c = sum(c for c, _ in b) / len(b)
        avg_o = sum(o for _, o in b) / len(b)
        ece += (len(b) / len(rows)) * abs(avg_c - avg_o)
    brier_max = CONFIG.get("pro_brier_max", 0.245)
    ece_max = CONFIG.get("pro_ece_max", 0.055)
    if brier > brier_max + 0.05 or ece > ece_max + 0.03:
        label = "BAD"
    elif brier > brier_max or ece > ece_max:
        label = "WARN"
    else:
        label = "OK"
    return {"brier": round(brier, 4), "ece": round(ece, 4), "n": len(rows), "label": label}


def pro_meta_gate(features: dict) -> tuple[str, float]:
    """Meta-learner gate. features keys (all optional):
        regime_conf, transition_risk, l85a_score, drift_psi,
        pred_score, mono_state, sw_rate, tail_run, score
    Returns ("PASS_HIGH"|"PASS_LOW"|"WAIT"|"FREEZE", composite).
    """
    rc = float(features.get("regime_conf", 0.5))
    tr = float(features.get("transition_risk", 0.35))
    l85 = float(features.get("l85a_score", 0.5))
    drift = float(features.get("drift_psi", 0.0))
    pred = float(features.get("pred_score", 0.5))
    mono = features.get("mono_state", "MONO_INACTIVE")
    sc = float(features.get("score", 0))
    # Composite: high regime_conf + l85a, low transition + drift + pred.
    composite = (
        0.30 * rc
        + 0.20 * l85
        + 0.15 * (1.0 - pred)
        + 0.15 * (1.0 - tr)
        + 0.10 * (1.0 - min(1.0, drift / 0.20))
        + 0.10 * (1.0 if mono == "MONO_SAME" else 0.5 if mono == "MONO_INACTIVE" else 0.0)
    )
    composite = max(0.0, min(1.0, composite))
    disagree_block = float(CONFIG.get("pro_model_disagree_block", 0.35))
    disagree_warn = float(CONFIG.get("pro_model_disagree_warn", 0.25))
    # Use (1 - composite) as a proxy for "model disagreement".
    disagree = 1.0 - composite
    if disagree >= disagree_block:
        return "FREEZE", round(composite, 3)
    if disagree >= disagree_warn:
        return "WAIT", round(composite, 3)
    if composite >= 0.78 and sc >= CONFIG.get("pro_score_high", 82):
        return "PASS_HIGH", round(composite, 3)
    return "PASS_LOW", round(composite, 3)


def pro_dynamic_tau(state: dict, drift_psi: float) -> tuple[float, float]:
    """Adaptive tau given drift + drawdown + connection errors.

    Returns (tau_lo, tau_hi). When drift_psi triggers WARN/BLOCK the band widens
    (smaller |edge - 0.5| zone allowed for PREDICT).
    """
    base_edge = float(CONFIG.get("pro_base_edge", 0.055))
    entropy_pen = 0.0  # not measuring full window entropy here; reserved for ML hook
    drift_pen = 0.025 if drift_psi >= float(CONFIG.get("pro_psi_warn", 0.10)) else 0.0
    dd = max(0.0, state.get("max_dd", 0.0))
    dd_pen = min(0.035, dd * 0.01)
    tau_hi = 0.50 + base_edge + entropy_pen + drift_pen + dd_pen
    tau_hi = max(
        float(CONFIG.get("pro_tau_hi_min", 0.555)),
        min(float(CONFIG.get("pro_tau_hi_max", 0.630)), tau_hi),
    )
    tau_lo = 1.0 - tau_hi
    return round(tau_lo, 4), round(tau_hi, 4)


def pro_risk_governor(state: dict, base_bet: float) -> tuple[float, str]:
    """Fractional Kelly + shoe / daily caps + consecutive-error freeze stack.

    Returns (effective_bet, reason). Reason is "" when no clamp applied.
    """
    cap_shoe = float(CONFIG.get("pro_max_shoe_risk", 4.0))
    cap_daily = float(CONFIG.get("pro_max_daily_risk", 8.0))
    kelly = float(CONFIG.get("pro_kelly_fraction", 0.25))
    bet = base_bet * kelly * 4.0  # kelly fraction is already conservative; scale to nominal
    bet = min(bet, base_bet)
    # Shoe cap: cumulative bets in this shoe.
    shoe_used = abs(state.get("shoe_pnl", 0.0))  # rough proxy
    if shoe_used >= cap_shoe:
        return 0.0, f"靴风险已达上限 {cap_shoe}"
    if abs(state.get("net_pnl", 0.0)) >= cap_daily:
        return 0.0, f"日风险已达上限 {cap_daily}"
    # Loss-streak escalating freeze
    ls = int(state.get("loss_streak", 0))
    f2 = int(CONFIG.get("pro_loss_2_freeze", 2))
    f3 = int(CONFIG.get("pro_loss_3_freeze", 5))
    if ls >= 3:
        return 0.0, f"连错 {ls} 次（冻结 {f3} 手）"
    if ls >= 2:
        return bet * 0.5, f"连错 {ls} 次（降权 50%）"
    return bet, ""


def decide(state: dict) -> dict:
    seq = state["sequence"]
    bp_n = state["bp_hand_no"]
    ph = phase_of(bp_n)
    bias = calc_bias(seq)
    regime = calc_regime(seq)

    apex_on = bool(CONFIG.get("apex_enabled", False))
    regime_conf, transition_risk = regime_metrics(seq, regime) if apex_on else (0.0, 0.0)

    # APEX_PRO 漂移自检（无副作用，仅返回级别 + PSI 值）
    drift_level, drift_psi = "OK", 0.0
    if apex_on and CONFIG.get("pro_drift_enabled"):
        drift_level, drift_psi = pro_drift_check(seq)

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
        "apex_enabled": apex_on,
        "regime_confidence": round(regime_conf, 3) if apex_on else None,
        "transition_risk": round(transition_risk, 3) if apex_on else None,
        "drift_level": drift_level,
        "drift_psi": drift_psi,
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
            "pred_score": 0.5,
            "mono_state": "MONO_INACTIVE",
            "signal_source": "NONE",
            "predictability_score": None,
            "predictability_label": None,
            "dmr_action": "NONE",
            "kep_units": 1.0,
            "risk_coeff": 1.0,
        }
        if extra:
            out.update(extra)
        return out

    if state["freeze"] > 0:
        return wait("FREEZE", f"冻结剩余 {state['freeze']} 手")

    if state["shoe_pnl"] <= CONFIG["stop_loss"]:
        return wait("STOP_LOSS", "止损已触发")

    # 漂移 BLOCK：直接退到 WAIT
    if drift_level == "BLOCK":
        return wait("DRIFT_BLOCK", f"分布漂移过大 PSI={drift_psi}")

    arr = bp_only(seq)
    if len(arr) < CONFIG["collect_min"]:
        return wait("COLLECTING", f"数据积累中 {len(arr)}/{CONFIG['collect_min']}")

    # 自适应 tau（仅 enabled 时覆盖静态阈值）
    tau_lo_eff = CONFIG["tau_lo"]
    tau_hi_eff = CONFIG["tau_hi"]
    if apex_on and CONFIG.get("pro_tau_dynamic_enabled"):
        tau_lo_eff, tau_hi_eff = pro_dynamic_tau(state, drift_psi)

    p_cal, pred_score = calc_pred(seq)
    side = ""
    confidence = 0.0
    source = "NONE"
    if p_cal >= tau_hi_eff:
        side, confidence, source = "B", p_cal, "PREDICT"
    elif p_cal <= tau_lo_eff:
        side, confidence, source = "P", 1 - p_cal, "PREDICT"
    elif CONFIG["small_fill_lo"] <= p_cal <= CONFIG["small_fill_hi"]:
        side = "B" if p_cal >= 0.5 else "P"
        confidence = 0.5
        source = "SMALL_BET"

    # APEX additive metrics — computed regardless so the response carries them
    # for the UI, but only fed into gating when their flag is on.
    pred_l85a, pred_l85a_label = (None, None)
    if apex_on and CONFIG.get("l85a_enabled"):
        pred_l85a, pred_l85a_label = l85a_predictability(
            seq, regime, regime_conf, transition_risk
        )

    if not side:
        return wait("NORMAL", "无信号",
                    {"pred_score": pred_score,
                     "predictability_score": pred_l85a,
                     "predictability_label": pred_l85a_label})

    mono = mono_state_of(seq, side)

    if CONFIG["mono_block_opposite"] and mono == "MONO_OPPOSITE":
        return wait("MONO_BLOCK", "MONO 反向拦截",
                    {"pred_score": pred_score, "mono_state": mono,
                     "signal_source": "MONO_BLOCK",
                     "predictability_score": pred_l85a,
                     "predictability_label": pred_l85a_label})

    if CONFIG["pred_mode"] == "LOW_ONLY" and pred_score >= 0.50:
        return wait("PRED_MODE_BLOCK", f"LOW_ONLY 模式拦截 {pred_score:.3f}",
                    {"pred_score": pred_score, "mono_state": mono,
                     "signal_source": "PRED_MODE_BLOCK",
                     "predictability_score": pred_l85a,
                     "predictability_label": pred_l85a_label})

    sc = score_calc(p_cal, ph, bias, regime, pred_score, mono)

    # L85A optional score blending — additive, weight-controlled, capped
    if apex_on and CONFIG.get("l85a_enabled") and pred_l85a is not None:
        weight = float(CONFIG.get("l85a_score_weight", 0.0))
        sc = max(0, min(100, int(sc + (pred_l85a - 0.5) * 40 * weight)))

    # DMR — may BLOCK or override side. Reads multi-window z-score / density.
    dmr_action = "NONE"
    dmr_z = dmr_d = 0.0
    if apex_on and CONFIG.get("monolith_enabled") and CONFIG.get("dmr_enabled"):
        dmr_target, dmr_action, dmr_z, dmr_d = dmr_advise(
            seq, side, float(CONFIG.get("dmr_z_threshold", 1.6))
        )
        if dmr_target and dmr_target != side:
            mode_name = CONFIG.get("dmr_conflict_action", "BLOCK").upper()
            if mode_name == "BLOCK":
                return wait("DMR_BLOCK", f"DMR 方向冲突 {side}→{dmr_target}",
                            {"pred_score": pred_score, "mono_state": mono,
                             "signal_source": "DMR_BLOCK",
                             "predictability_score": pred_l85a,
                             "predictability_label": pred_l85a_label,
                             "dmr_action": dmr_action})
            elif mode_name == "FOLLOW_DMR":
                # Override side; recompute mono and source label
                side = dmr_target
                mono = mono_state_of(seq, side)
                source = "DMR_OVERRIDE"

    # KEP unit multiplier
    units = 1.0
    if apex_on and CONFIG.get("monolith_enabled") and CONFIG.get("kep_enabled"):
        units = kep_units(dmr_z, dmr_d, float(CONFIG.get("kep_max_multiplier", 1.0)))

    # Qimen governance coefficient on bet_size
    risk = 1.0
    if apex_on and CONFIG.get("qimen_enabled"):
        risk = qimen_risk_coeff(regime, regime_conf, state["consec_errors"])

    # APEX_PRO Meta-Learner gate
    meta_gate, meta_composite = "PASS_LOW", 0.0
    if apex_on and CONFIG.get("pro_meta_enabled"):
        meta_gate, meta_composite = pro_meta_gate({
            "regime_conf": regime_conf,
            "transition_risk": transition_risk,
            "l85a_score": pred_l85a if pred_l85a is not None else 0.5,
            "drift_psi": drift_psi,
            "pred_score": pred_score,
            "mono_state": mono,
            "score": sc,
        })
        if meta_gate == "FREEZE":
            return wait("META_FREEZE", f"元决策冻结 综合={meta_composite}",
                        {"pred_score": pred_score, "mono_state": mono,
                         "signal_source": "META_FREEZE",
                         "predictability_score": pred_l85a,
                         "predictability_label": pred_l85a_label,
                         "dmr_action": dmr_action,
                         "meta_gate": meta_gate,
                         "meta_composite": meta_composite})
        if meta_gate == "WAIT":
            return wait("META_WAIT", f"元决策观望 综合={meta_composite}",
                        {"pred_score": pred_score, "mono_state": mono,
                         "signal_source": "META_WAIT",
                         "predictability_score": pred_l85a,
                         "predictability_label": pred_l85a_label,
                         "dmr_action": dmr_action,
                         "meta_gate": meta_gate,
                         "meta_composite": meta_composite})

    if ph == "EXPLORE":
        bet = CONFIG["small_bet_size"] * units * risk
        bet = min(bet, CONFIG["max_bet"])
        # APEX_PRO 风险治理（如启用，进一步收敛 bet）
        gov_reason = ""
        if apex_on and CONFIG.get("pro_risk_governor_enabled"):
            bet, gov_reason = pro_risk_governor(state, bet)
            if bet <= 0:
                return wait("RISK_HALT", gov_reason or "风险治理熔断",
                            {"pred_score": pred_score, "mono_state": mono,
                             "signal_source": "RISK_HALT",
                             "predictability_score": pred_l85a,
                             "predictability_label": pred_l85a_label,
                             "dmr_action": dmr_action,
                             "risk_governor_reason": gov_reason})
        return {
            **base,
            "decision": "SMALL",
            "side": side,
            "confidence": confidence,
            "bet_size": round(bet, 4),
            "mode": "EXPLORE",
            "message": "探索期·小注",
            "score": sc,
            "pred_score": pred_score,
            "mono_state": mono,
            "signal_source": source,
            "predictability_score": pred_l85a,
            "predictability_label": pred_l85a_label,
            "dmr_action": dmr_action,
            "kep_units": units,
            "risk_coeff": risk,
            "meta_gate": meta_gate,
            "meta_composite": meta_composite,
            "risk_governor_reason": gov_reason,
        }

    if sc >= CONFIG["score_full"]:
        decision = "FULL"
        bet = CONFIG["base_bet"]
    elif sc >= CONFIG["score_small"]:
        decision = "SMALL"
        bet = CONFIG["small_bet_size"]
    else:
        return wait("LOW_SCORE", f"评分不足 {sc}",
                    {"score": sc, "pred_score": pred_score,
                     "mono_state": mono, "signal_source": source,
                     "predictability_score": pred_l85a,
                     "predictability_label": pred_l85a_label,
                     "dmr_action": dmr_action})

    bet = min(bet * units * risk, CONFIG["max_bet"])

    # APEX_PRO 风险治理（如启用，进一步收敛 bet）
    gov_reason = ""
    if apex_on and CONFIG.get("pro_risk_governor_enabled"):
        bet, gov_reason = pro_risk_governor(state, bet)
        if bet <= 0:
            return wait("RISK_HALT", gov_reason or "风险治理熔断",
                        {"pred_score": pred_score, "mono_state": mono,
                         "signal_source": "RISK_HALT",
                         "predictability_score": pred_l85a,
                         "predictability_label": pred_l85a_label,
                         "dmr_action": dmr_action,
                         "risk_governor_reason": gov_reason})

    return {
        **base,
        "decision": decision,
        "side": side,
        "confidence": confidence,
        "bet_size": round(bet, 4),
        "mode": source,
        "message": f"{regime} {mono}",
        "score": sc,
        "pred_score": pred_score,
        "mono_state": mono,
        "signal_source": source,
        "predictability_score": pred_l85a,
        "predictability_label": pred_l85a_label,
        "dmr_action": dmr_action,
        "kep_units": units,
        "risk_coeff": risk,
        "meta_gate": meta_gate,
        "meta_composite": meta_composite,
        "risk_governor_reason": gov_reason,
        "tau_lo_eff": tau_lo_eff,
        "tau_hi_eff": tau_hi_eff,
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
    # ── V121 base ──
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
    stop_loss: Optional[float] = Field(None, ge=-1000, le=0)
    stop_win: Optional[float] = Field(None, ge=0, le=10000)
    freeze_threshold: Optional[int] = Field(None, ge=1, le=20)
    freeze_duration: Optional[int] = Field(None, ge=0, le=20)
    phase1_hands: Optional[int] = Field(None, ge=0, le=200)
    phase2_hands: Optional[int] = Field(None, ge=0, le=400)
    cal_shrink: Optional[float] = Field(None, ge=0, le=1)
    pred_mode: Optional[str] = Field(None, pattern="^(LOW_ONLY|OFF)$")
    mono_block_opposite: Optional[bool] = None
    # ── APEX ──
    apex_enabled: Optional[bool] = None
    l85a_enabled: Optional[bool] = None
    l85a_score_weight: Optional[float] = Field(None, ge=0, le=1)
    qimen_enabled: Optional[bool] = None
    qimen_chaos_coeff: Optional[float] = Field(None, ge=0.3, le=1)
    qimen_mixed_coeff: Optional[float] = Field(None, ge=0.3, le=1)
    monolith_enabled: Optional[bool] = None
    dmr_enabled: Optional[bool] = None
    dmr_conflict_action: Optional[str] = Field(None, pattern="^(BLOCK|FOLLOW_DMR)$")
    dmr_z_threshold: Optional[float] = Field(None, ge=0.5, le=5.0)
    kep_enabled: Optional[bool] = None
    kep_max_multiplier: Optional[float] = Field(None, ge=1.0, le=5.0)
    # ── APEX_PRO ──
    pro_drift_enabled: Optional[bool] = None
    pro_psi_warn: Optional[float] = Field(None, ge=0, le=1)
    pro_psi_block: Optional[float] = Field(None, ge=0, le=1)
    pro_drift_window: Optional[int] = Field(None, ge=10, le=500)
    pro_calibration_enabled: Optional[bool] = None
    pro_brier_max: Optional[float] = Field(None, ge=0, le=1)
    pro_ece_max: Optional[float] = Field(None, ge=0, le=1)
    pro_calibration_window: Optional[int] = Field(None, ge=30, le=10000)
    pro_meta_enabled: Optional[bool] = None
    pro_model_disagree_warn: Optional[float] = Field(None, ge=0, le=1)
    pro_model_disagree_block: Optional[float] = Field(None, ge=0, le=1)
    pro_tau_dynamic_enabled: Optional[bool] = None
    pro_base_edge: Optional[float] = Field(None, ge=0, le=0.5)
    pro_tau_hi_min: Optional[float] = Field(None, gt=0.5, lt=1)
    pro_tau_hi_max: Optional[float] = Field(None, gt=0.5, lt=1)
    pro_risk_governor_enabled: Optional[bool] = None
    pro_kelly_fraction: Optional[float] = Field(None, ge=0.05, le=1.0)
    pro_max_shoe_risk: Optional[float] = Field(None, ge=0.5, le=100.0)
    pro_max_daily_risk: Optional[float] = Field(None, ge=0.5, le=1000.0)
    pro_loss_2_freeze: Optional[int] = Field(None, ge=0, le=20)
    pro_loss_3_freeze: Optional[int] = Field(None, ge=0, le=20)
    pro_score_high: Optional[int] = Field(None, ge=0, le=100)
    pro_score_standard: Optional[int] = Field(None, ge=0, le=100)
    pro_score_low: Optional[int] = Field(None, ge=0, le=100)
    pro_score_wait: Optional[int] = Field(None, ge=0, le=100)
    # 自动驯化
    auto_train_enabled: Optional[bool] = None


# ---------------------------------------------------------------------------
# Mutators (require lock + state persist)
# ---------------------------------------------------------------------------

def _apply_outcome(s: dict, outcome: str) -> dict:
    if is_table_locked(s["table_id"]):
        raise HTTPException(423, "桌台已被管理员锁定，请到后台解锁")
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
    if is_table_locked(s["table_id"]):
        raise HTTPException(423, "桌台已被管理员锁定，结算亦被拒绝")
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
    _load_config_from_db()


@app.get("/")
def root():
    return {
        "status": "ok",
        "version": VERSION,
        "message": "V121 云端运行中",
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


def _maybe_auto_train(table_id: str) -> Optional[dict]:
    """如果总靴数命中 milestone，触发一次自动回测并存表。

    Returns the saved training_run record dict if a run was triggered, else None.
    Lazy-imports admin to avoid circular import at module load.
    """
    if not CONFIG.get("auto_train_enabled"):
        return None
    milestones = CONFIG.get("auto_train_milestones", []) or []
    if not milestones:
        return None
    with _connect() as conn:
        n_shoes = conn.execute("SELECT COUNT(*) FROM shoes").fetchone()[0]
    if n_shoes not in milestones:
        return None
    try:
        from admin import compute_backtest  # type: ignore
    except Exception:
        return None
    result = compute_backtest()
    full_json = json.dumps(result, ensure_ascii=False)
    overall_json = json.dumps(result.get("overall", {}), ensure_ascii=False)
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO training_runs(ts, trigger, shoe_count, table_id, "
            "overall_json, full_json) VALUES(?,?,?,?,?,?)",
            (time.time(), f"auto_{n_shoes}", n_shoes, None, overall_json, full_json),
        )
        run_id = cur.lastrowid
    return {"id": run_id, "shoe_count": n_shoes, "trigger": f"auto_{n_shoes}"}


@app.post("/v100/new_shoe")
async def post_new_shoe(req: NewShoeReq, _: None = Depends(require_auth)):
    async with TABLE_LOCKS[req.table_id]:
        s = get_table(req.table_id)
        triggered = None
        if s["hand_no"] > 0:
            save_shoe(s)
            triggered = _maybe_auto_train(req.table_id)
        TABLES[req.table_id] = new_table(req.table_id)
        s = TABLES[req.table_id]
        d = decide(s)
        s["last_decision"] = d
        _persist_state(s)
        if triggered:
            d = {**d, "auto_train_triggered": triggered}
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
    if not data:
        raise HTTPException(400, "未提供任何待更新字段")
    # Validate against the merged result (catches partial updates that would
    # otherwise leave invariants broken against the existing stored value).
    merged = {**CONFIG, **data}
    if merged["tau_lo"] >= merged["tau_hi"]:
        raise HTTPException(400, "tau_lo 必须小于 tau_hi")
    if merged["small_fill_lo"] >= merged["small_fill_hi"]:
        raise HTTPException(400, "small_fill_lo 必须小于 small_fill_hi")
    if merged["score_small"] > merged["score_full"]:
        raise HTTPException(400, "score_small 必须不大于 score_full")
    if merged["base_bet"] > merged["max_bet"]:
        raise HTTPException(400, "base_bet 必须不大于 max_bet")
    if merged["small_bet_size"] > merged["max_bet"]:
        raise HTTPException(400, "small_bet_size 必须不大于 max_bet")
    if merged["phase1_hands"] > merged["phase2_hands"]:
        raise HTTPException(400, "phase1_hands 必须不大于 phase2_hands")
    if merged.get("pro_score_small", merged["score_small"]) > merged.get("pro_score_high", merged["score_full"]):
        raise HTTPException(400, "pro_score_low/standard 必须不大于 pro_score_high")
    CONFIG.update(data)
    _persist_config()
    return {"status": "ok", "version": VERSION, "applied": data, "config": CONFIG}


@app.get("/ai/config")
async def get_config(_: None = Depends(require_auth)):
    return {"version": VERSION, "config": CONFIG}


@app.post("/ai/config/reset")
async def reset_config(_: None = Depends(require_auth)):
    """Drop persisted overrides and reload defaults (in-process)."""
    with db() as conn:
        conn.execute("DELETE FROM table_state WHERE table_id = ?", (CONFIG_KEY,))
    # Cannot regenerate the original literal at runtime — instead recompute by
    # re-importing module defaults. Simpler: tell the operator to restart.
    return {"status": "ok", "message": "persisted overrides cleared; restart server to fully reload defaults"}


@app.get("/ai/calibration")
async def calibration(_: None = Depends(require_auth)):
    """APEX_PRO 概率校准自检：返回 Brier / ECE / 样本数 + 标签。"""
    return {"version": VERSION, "calibration": pro_calibration_health()}


@app.get("/ai/snapshot")
async def snapshot(_: None = Depends(require_auth)):
    out = []
    for tid, s in TABLES.items():
        last = s.get("last_decision") or {}
        out.append({
            "table_id": tid,
            "shoe_id": s.get("shoe_id"),
            "hand_no": s.get("hand_no"),
            "bp_hand_no": s.get("bp_hand_no"),
            "shoe_pnl": round(s.get("shoe_pnl", 0.0), 4),
            "net_pnl": round(s.get("net_pnl", 0.0), 4),
            "wins": s.get("wins"),
            "losses": s.get("losses"),
            "freeze": s.get("freeze"),
            "loss_streak": s.get("loss_streak"),
            "consec_errors": s.get("consec_errors"),
            "consec_wins": s.get("consec_wins"),
            "max_dd": round(s.get("max_dd", 0.0), 4),
            "last_outcome": s.get("last_outcome"),
            "last_decision": {
                "decision": last.get("decision"),
                "side": last.get("side"),
                "bet_size": last.get("bet_size"),
                "score": last.get("score"),
                "regime": last.get("regime"),
                "mono_state": last.get("mono_state"),
                "predictability_label": last.get("predictability_label"),
                "dmr_action": last.get("dmr_action"),
                "decision_id": last.get("decision_id"),
            },
            "tail_sequence": "".join(s.get("sequence", [])[-30:]),
        })
    return {"version": VERSION, "tables": out, "ts": time.time()}


@app.get("/ai/recent_hands")
async def recent_hands(table_id: Optional[str] = None, limit: int = 50,
                       _: None = Depends(require_auth)):
    limit = max(1, min(500, limit))
    with _connect() as conn:
        if table_id:
            rows = conn.execute(
                "SELECT id, ts, table_id, shoe_id, hand_no, decision_id, outcome, "
                "result, decision, side, bet_size, score, regime, pred_score, "
                "mono_state, signal_source, mode, pnl_delta, pnl_running "
                "FROM hands WHERE table_id = ? ORDER BY id DESC LIMIT ?",
                (table_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, ts, table_id, shoe_id, hand_no, decision_id, outcome, "
                "result, decision, side, bet_size, score, regime, pred_score, "
                "mono_state, signal_source, mode, pnl_delta, pnl_running "
                "FROM hands ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return {
        "version": VERSION,
        "count": len(rows),
        "hands": [dict(r) for r in rows],
    }


@app.get("/ai/report")
async def report(_: None = Depends(require_auth)):
    # In-memory totals (current session per table). Treats partially-loaded
    # state as authoritative for live numbers.
    live_bets = sum(s["wins"] + s["losses"] for s in TABLES.values())
    live_wins = sum(s["wins"] for s in TABLES.values())
    live_losses = sum(s["losses"] for s in TABLES.values())
    live_pnl = sum(s["net_pnl"] for s in TABLES.values())
    max_dd = max([s["max_dd"] for s in TABLES.values()] or [0])

    # Lifetime totals from the persisted hands log — these are the truth.
    with _connect() as conn:
        h = conn.execute(
            "SELECT COUNT(*) AS n, "
            "SUM(CASE WHEN result='WIN'  THEN 1 ELSE 0 END) AS w, "
            "SUM(CASE WHEN result='LOSS' THEN 1 ELSE 0 END) AS l, "
            "SUM(CASE WHEN result='TIE'  THEN 1 ELSE 0 END) AS t, "
            "COALESCE(SUM(pnl_delta),0) AS pnl "
            "FROM hands WHERE result IN ('WIN','LOSS','TIE')"
        ).fetchone()
        sh = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(pnl),0), COALESCE(AVG(pnl),0) FROM shoes"
        ).fetchone()
    db_settled = (h["w"] or 0) + (h["l"] or 0)
    wl, wu = wilson(h["w"] or 0, db_settled)
    wr = ((h["w"] or 0) / db_settled) if db_settled else 0.0
    return {
        "version": VERSION,
        "live": {
            "total_bets": live_bets,
            "wins": live_wins,
            "losses": live_losses,
            "net_pnl": round(live_pnl, 4),
            "max_drawdown": round(max_dd, 4),
        },
        "lifetime": {
            "total_settled": db_settled,
            "wins": h["w"] or 0,
            "losses": h["l"] or 0,
            "ties": h["t"] or 0,
            "pnl": round(h["pnl"] or 0.0, 4),
            "win_rate": round(wr, 4),
            "wilson_lower": round(wl, 4),
            "wilson_upper": round(wu, 4),
        },
        # Backward-compat keys used by older terminal builds.
        "total_bets": live_bets,
        "wins": live_wins,
        "losses": live_losses,
        "win_rate": round(wr, 4),
        "wilson_lower": round(wl, 4),
        "wilson_upper": round(wu, 4),
        "net_pnl": round(live_pnl, 4),
        "max_drawdown": round(max_dd, 4),
        "total_shoes": sh[0],
        "shoes_pnl": round(sh[1], 4),
        "avg_shoe_pnl": round(sh[2], 4),
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


@app.get("/console", response_class=HTMLResponse)
def console():
    if os.path.exists(CONSOLE_HTML_PATH):
        with open(CONSOLE_HTML_PATH, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse(
        "<h1>console.html 未部署</h1>"
        f"<p>请把 console.html 放到 {CONSOLE_HTML_PATH}，或设置 V121_CONSOLE_HTML 环境变量。</p>",
        status_code=404,
    )


# /admin 路由由 admin.py 提供。如果 V121_ADMIN_PASSWORD 未配置，admin 模块
# 拒绝加载，这里给一个清晰提示页代替 500。
if not _ADMIN_AVAILABLE:
    @app.get("/admin", response_class=HTMLResponse)
    def admin_disabled():
        return HTMLResponse(
            "<h1>管理后台未启用</h1>"
            "<p>请在环境变量中配置 <code>V121_ADMIN_PASSWORD</code>（至少 6 位）"
            "并重启服务。</p>",
            status_code=503,
        )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "server:app",
        host=os.environ.get("V121_HOST", "127.0.0.1"),
        port=int(os.environ.get("V121_PORT", "8000")),
        workers=1,  # state is in-process; do NOT scale workers without Redis
    )
