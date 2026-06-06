# BUG_LIST — v121 真实代码缺陷（只读审计，未修复）

> 每条含：真实位置、当前行为、风险、修复边界。第一阶段不下刀，仅记录。

## BUG-01 — `.gitignore` 文件名缺少点号（中危）
- 位置：仓库根 `gitignore`（应为 `.gitignore`）
- 现状：文件名是 `gitignore`，git 不会把它当忽略规则文件。内容（`*.db` / `.env` / `__pycache__` 等）**当前完全未生效**。
- 风险：`.env`、`*.db`（含纸面 PnL 与可能的运行态）、`__pycache__` 有被误提交的风险；README §9 的"git 里搜不到 key"自检会被这个失效的忽略文件削弱。
- 修复边界：`git mv gitignore .gitignore`。零代码风险。

## BUG-02 — README 引用的 deploy 文件不存在（中危）
- 位置：`README.md:64`（`src/deploy/v121.service`）、`README.md:81`（`src/deploy/nginx.conf`）
- 现状：仓库无 `deploy/` 目录，这两个文件缺失。按 README 第 5/6 步部署会在 `cp` 处直接失败。
- 风险：部署文档不可执行；新机器无法按指南落地。
- 修复边界：补 `deploy/v121.service` 与 `deploy/nginx.conf`（systemd unit 用 `EnvironmentFile=/etc/v121.env`、`User=v121`；nginx 反代 127.0.0.1:8000 + 安全头）。属新增文件，不动业务代码。

## BUG-03 — FastAPI `on_event("startup")` 已弃用（低危）
- 位置：`server.py:690-692`
- 现状：用 `@app.on_event("startup")`。在 fastapi 0.115 / starlette 新版会有 DeprecationWarning，未来可能移除。
- 风险：升级依赖后 `init_db()` 可能不再触发，导致首次请求建表失败。
- 修复边界：迁移到 `lifespan` 上下文管理器。单点改动。

## BUG-04 — `/ai/tune` 修改全局 CONFIG，无锁、无桌隔离、进程级生效（中危）
- 位置：`server.py:780-789`，写入 `server.py:52-73` 的全局 `CONFIG`
- 现状：任一持 key 的客户端 `POST /ai/tune` 会 `CONFIG.update()`，立即对**所有 T1-T5 桌**生效，且不在任何 `TABLE_LOCKS` 内，与正在 `decide()` 的请求存在读写竞争。
- 风险：(a) 一台手机调参影响其余 4 桌，违背"5 桌互不干扰"（README §7）；(b) CPython 下 dict.update 单条赋值原子，但多键更新期间 `decide()` 可能读到半套参数，产生不一致决策；(c) 无审计留痕。
- 修复边界：要么把 CONFIG 改为按桌存储 + 锁内更新，要么明确声明 tune 为"全局管理操作"并加管理员级保护 + 落审计。需要设计决策，列入 GAP。

## BUG-05 — `/ai/report` 只聚合内存中已加载的桌（中危，数据正确性）
- 位置：`server.py:792-799`（`for s in TABLES.values()`）
- 现状：`total_bets/wins/losses/net_pnl/max_drawdown` 只统计**本进程启动后被访问过、已 hydrate 进 `TABLES`** 的桌。重启后未被触达的桌不计入；`shoes` 表的历史聚合（`server.py:801-804`）是单独从 DB 取的，二者口径不一致。
- 风险：报表数字会偏小/漂移，运维据此判断会被误导。与"诚实记账"的项目目标直接冲突。
- 修复边界：报表应统一从 DB（`hands`/`shoes`）聚合，而非内存 TABLES；或启动时预加载所有 `table_state`。属 `/ai/report` 单函数重写。

## BUG-06 — 终端本地 `seq` 与服务端序列可能静默不一致（低危）
- 位置：`terminal.html:84`（`var seq`）、`rec()` `terminal.html:177-183`
- 现状：`rec()` 先本地 `seq.push(o)` 再发请求；若请求失败（`catch` 只 `lg` 一行，`terminal.html:181-182`），本地 seq 已多了一个，但服务端没记。只有 `rollback()`（`terminal.html:206-213`）会用服务端 `r.sequence` 覆盖回来。
- 风险：录入失败后界面序列与真实状态偏差，操作者可能基于错误显示继续。
- 修复边界：录入成功后再 push，或失败时回滚本地 seq。前端单函数改动。

## BUG-07 — `save_hand` 结算分支用 `last_outcome` 作为 outcome 列（低危，语义）
- 位置：`server.py:681`（`s.get("last_outcome","")`）
- 现状：结算时落库的 `hands.outcome` 取的是"最近一次录入的 outcome"，而结算针对的是更早发出的那张决策券，两者 hand 不一定对齐。
- 风险：`hands` 表分析时 outcome↔result 对应关系可能错位，影响回测口径。
- 修复边界：结算行应记录被结算决策对应的 hand_no/outcome（`pending_decisions` 已存 `hand_no`，可回填），或明确该列语义。属数据建模澄清。

## BUG-08 — `require_auth` 等长分支带来可观察的长度旁路（极低危）
- 位置：`server.py:199-200`
- 现状：注释说"pad to avoid early-exit timing"，但实现是 `len(expected)!=len(presented) or not compare_digest(...)`——长度不等时直接短路返回，仍泄露"长度是否正确"这一比特。
- 风险：极低（key 是 48 hex 字符，长度几乎公开）。仅记录，不必紧急处理。
- 修复边界：可忽略；如要严谨，对输入先做固定长度 hash 再 compare_digest。

---

## 汇总
| ID | 严重度 | 文件 | 一句话 |
|----|--------|------|--------|
| BUG-01 | 中 | gitignore | 忽略文件名缺点号，规则未生效 |
| BUG-02 | 中 | README.md | 引用的 deploy 文件缺失，文档不可执行 |
| BUG-03 | 低 | server.py:690 | on_event 已弃用 |
| BUG-04 | 中 | server.py:780 | tune 改全局 CONFIG，跨桌、无锁 |
| BUG-05 | 中 | server.py:792 | report 只聚合内存桌，数字会漂移 |
| BUG-06 | 低 | terminal.html:177 | 本地 seq 与服务端可能静默不一致 |
| BUG-07 | 低 | server.py:681 | 结算行 outcome 语义错位 |
| BUG-08 | 极低 | server.py:199 | 等长分支泄露长度比特 |
