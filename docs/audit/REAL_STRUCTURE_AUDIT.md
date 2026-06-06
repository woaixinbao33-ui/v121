# REAL_STRUCTURE_AUDIT — v121 真实结构审计（带真实行号）

> 对象：`woaixinbao33-ui/v121`。本报告只描述**实际存在**的代码，不引用任何 v999 假想结构。

## 1. 文件清单（git ls-files）

```
README.md          部署/运维指南，含安全自检清单
gitignore          忽略规则（文件名缺点号，应为 .gitignore — 见 BUG-01）
requirements.txt   fastapi==0.115.5 / uvicorn[standard]==0.32.1 / pydantic==2.10.3
server.py          876 行，单进程 FastAPI 决策服务
terminal.html      241 行，单页移动终端（登录 + 录入 + 结算 + 报告）
```

无 `deploy/` 目录，但 `README.md:64` / `README.md:81` 引用 `src/deploy/v121.service` 与 `src/deploy/nginx.conf` —— **这两个文件在仓库中不存在**（见 BUG-02 / GAP）。

## 2. server.py 真实锚点

### 2.1 配置与常量
- `server.py:39` `VERSION = "V121_CLOUD_2"`
- `server.py:41-46` `API_KEY` 从环境变量读取，缺失即 `raise RuntimeError`（无默认 key — 安全做法 ✅）
- `server.py:48-50` `DB_PATH` / `TERMINAL_HTML_PATH` / `BANKER_COMMISSION`（庄家 5% 抽水）
- `server.py:52-73` `CONFIG`（全局可变策略字典）。关键字段：`tau_lo=0.44`、`tau_hi=0.56`、`collect_min=10`、`stop_loss=-4.0`、`freeze_threshold=2`、`freeze_duration=3`、`pred_mode="LOW_ONLY"`
- `server.py:76-77` `TABLES`（进程内 dict）、`TABLE_LOCKS`（进程内 `defaultdict(asyncio.Lock)`）
- `server.py:79` `app = FastAPI(...)`

### 2.2 存储层
- `server.py:86-89` `_connect()`
- `server.py:92-175` `init_db()` — 建表 `events` / `hands` / `shoes` / `table_state` / `pending_decisions`
- `server.py:137-138` `hands.decision_id` 唯一索引（幂等基础）
- `server.py:178-189` `db()` 事务上下文（BEGIN IMMEDIATE / COMMIT / ROLLBACK）

### 2.3 鉴权
- `server.py:196-201` `require_auth()` — `hmac.compare_digest` + 等长检查（✅）

### 2.4 状态
- `server.py:208-231` `new_table()` — 单桌内存状态结构
- `server.py:234-244` `_load_state_from_db()`
- `server.py:247-254` `_persist_state()`（UPSERT `table_state`）
- `server.py:257-262` `get_table()`（内存优先，回落 DB，再回落新建）

### 2.5 策略原语（纯函数）
- `server.py:269-274` `phase_of()` EXPLORE/ANALYZE/HARVEST
- `server.py:277-285` `bp_only()` / `calc_bias()`
- `server.py:288-303` `calc_regime()` TREND_B/TREND_P/OSC/CHAOS/MIXED
- `server.py:306-316` `calc_pred()`（拉普拉斯平滑 + 收缩校准）
- `server.py:319-330` `mono_state_of()`
- `server.py:333-346` `score_calc()`
- `server.py:349-467` **`decide()` — 主决策函数**（详见 ENGINE_WIRING_MAP）
- `server.py:470-479` `wilson()`（Wilson 置信下/上界）

### 2.6 持久化辅助（均在桌锁内调用）
- `server.py:486-493` `save_event()`
- `server.py:496-516` `save_hand()`
- `server.py:519-531` `save_shoe()`
- `server.py:534-546` `issue_decision_id()`（WAIT 不发 id）
- `server.py:549-566` `claim_decision()`（原子置 settled，含表不匹配/重复结算保护）

### 2.7 请求模型
- `server.py:573-610` `OutcomeReq` / `SettleReq` / `NewShoeReq` / `RollbackReq` / `HBReq` / `TuneReq`（pydantic 校验，pattern 限定 B/P/T、WIN/LOSS/TIE）

### 2.8 变更器
- `server.py:617-637` `_apply_outcome()`
- `server.py:640-683` `_apply_settlement()`

### 2.9 路由
- `server.py:690-692` `@app.on_event("startup")` → `init_db()`（注意：FastAPI 已弃用 on_event，见 BUG-03）
- `server.py:695-701` `GET /`
- `server.py:704-706` `GET /healthz`
- `server.py:709-722` `POST /v100/heartbeat`
- `server.py:725-729` `POST /v100/outcome`
- `server.py:732-736` `POST /v100/settle`
- `server.py:739-750` `POST /v100/new_shoe`
- `server.py:753-777` `POST /v100/rollback`
- `server.py:780-789` `POST /ai/tune`
- `server.py:792-820` `GET /ai/report`
- `server.py:823-843` `GET /ai/shoes`
- `server.py:846-853` `GET /ai/export_shoes`
- `server.py:856-865` `GET /terminal`（读 `terminal.html`，缺失返回 404 HTML）
- `server.py:868-875` `__main__` uvicorn，`workers=1`（明确禁止多 worker，✅ 与 README §10 一致）

## 3. terminal.html 真实锚点

- `terminal.html:36-43` `#login` 登录块（API Key 输入 + 后端地址）
- `terminal.html:45-79` `#app` 主界面
- `terminal.html:48-55` 桌号选择 T1-T5 + 心跳 + 登出
- `terminal.html:57` `#dec` 决策显示区
- `terminal.html:59-63` 录入按钮：庄 B / 闲 P / 和 T
- `terminal.html:65-69` 结算按钮：赢 / 输 / 和
- `terminal.html:71-75` 新靴 / 回退 / 报告
- `terminal.html:77-78` `#seq` 序列 / `#log` 日志
- `terminal.html:84-87` 全局 `KEY` / `BASE` / `seq` / `lastDecisionId`
- `terminal.html:99-113` `api()` / `getJSON()`（fetch 封装，带 `X-API-Key`）
- `terminal.html:117-138` `doLogin()`（healthz + heartbeat 双探活，key 存 sessionStorage）
- `terminal.html:147-175` `show()` 决策渲染（追踪 `lastDecisionId`）
- `terminal.html:177-228` `rec()` / `settle()` / `newShoe()` / `rollback()` / `hb()` / `report()`
- `terminal.html:230-238` `autoLogin()`（从 sessionStorage 恢复）

UI 关键词（与指令"手机端必须保留"对照）：✅ BACCARAT 风格、庄 B、闲 P、和 T、赢、输、新靴、回退、心跳、报告、T1-T5。
❌ **不存在**：珠盘 / 大路 / 大眼仔 / 小路 / 蟑螂路 / 庄对 / 闲对 / 汇鑫国际 / 云同步 / 采集 / 调参控制台 / 管理后台入口 / `.mcell` 主决策矩阵 / `id="mMain"` / `FINAL_135`。
v121 的终端是极简版，没有路单图，也没有 v999 的主决策矩阵 DOM。

## 4. 结构性观察

1. **单文件、单进程、单前端**：没有 admin/mobile 双端，没有 modules 分层，没有 sidecar，没有补丁层。这是干净的，但也意味着指令里的"7 卡导航 / AI Owner / S0-S5"全部是**新增**而非修补。
2. **DB schema 比当前代码用得多**：`hands` 表有 `regime/pred_score/mono_state/signal_source` 等列，`decide()` 也产出这些字段，落库链路完整 ✅。
3. **CONFIG 全局可变**：`/ai/tune`（`server.py:780-789`）直接 `CONFIG.update()`，对所有桌生效且无锁（见 BUG-04）。
