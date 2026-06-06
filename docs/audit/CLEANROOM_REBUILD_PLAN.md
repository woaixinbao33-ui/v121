# CLEANROOM_REBUILD_PLAN — 诚实的改造边界（基于 v121 现实）

> 前提澄清：v121 已经是一个干净、短链路、无补丁污染的实现。它**不需要**"Clean Room 重组"来去污染——它本身就是干净的。真正的问题是：你想要的是 v999 那一整套（admin 7 卡 / mobile 主决策矩阵 / AI Owner / S0-S5 治理），而那在本仓库里要靠**新建**，不是修补。
>
> 本计划只针对 v121 真实代码，给出风险可控、可回滚的改造，并诚实标注哪些是"小修"、哪些是"大工程"。

## 路线选择（先决，需你拍板）

- **路线 1（推荐，若你只想要 v121 健康+合规底座）**：只做 §1 小修三刀，不引入 v999 的庞大结构。投入小、风险低、立刻可验证。
- **路线 2（若你确实要 v999 全套）**：先把 `v999-cloud-cleanroom` 真实源码接入本会话（加 scope 或上传快照），对**真实 v999 代码**重做行号级审计，再谈三刀。在拿到真实源码前，任何 v999 补丁都是猜测，违反红线，不做。

---

## §1 路线 1：v121 小修三刀（DRY_RUN / APPLY / VERIFY / ROLLBACK 门控）

### 刀 1 — 红线 envelope + 仓库卫生（只动新增/配置，不动决策逻辑）
- 动作：
  - `git mv gitignore .gitignore`（BUG-01）
  - 新增 `deploy/v121.service`、`deploy/nginx.conf`（BUG-02 / GAP-A2）
  - `server.py` 新增 `redline_envelope()`，在各响应注入 `paper_only/NOT_REAL_BET/real_trading=false/...`（GAP-A1）
- 不动：`decide()` 及所有策略原语。
- VERIFY：`curl /healthz` 含红线字段；`/v100/heartbeat` 含红线字段；服务可起。
- ROLLBACK：git revert 单提交。

### 刀 2 — report 口径修正 + tune 审计（正确性）
- 动作：`/ai/report` 改为从 DB 聚合（BUG-05）；`/ai/tune` 落变更日志（GAP-A5）；评估 CONFIG 按桌隔离 or 显式声明全局（BUG-04，需你决定语义）。
- VERIFY：重启后 report 数字与 DB 一致；tune 留痕可查。
- 前置门控：刀 1 VERIFY FAIL=0 才进刀 2。

### 刀 3 — 测试与 CI（回归保护）
- 动作：`tests/test_decide.py` 覆盖 WAIT/COLLECTING/FREEZE/STOP_LOSS/FULL/SMALL/TIE-push/idempotent-settle；`.github/workflows/ci.yml` 跑 pytest。
- VERIFY：CI 绿；核心路径有断言。
- 前置门控：刀 2 FAIL=0 才进刀 3。

---

## §2 路线 2：若要 v999 全套（大工程，先接源码）

按指令三刀边界（sidecar/ai-owner → admin 7 卡 → mobile .mcell），但**必须**满足：
1. 真实 `admin.html` / `mobile.html` / `v999_api_R5.py` / `ai_brain/*` 可读（当前不可读）。
2. 真实 `.tabs` / `.mcell` / `FINAL_135` / sidecar 注册块的行号锚点已审计（当前无对象）。
3. 每刀 DRY_RUN → SIGNED_APPLY → VERIFY(FAIL=0) → 下一刀。
4. 无云端 live verify 不签 100%。

在 (1)(2) 满足前，本计划不展开 v999 补丁——这是遵守而非偷懒：指令第二章明令"不准猜 DOM / API / sidecar"。

---

## 不变的红线（两条路线都遵守）
```
real_trading=false / final_go=false / paper_only=true / NOT_REAL_BET=true
NO_PROFIT_GUARANTEE=true / 不承诺盈利 / 不暴露完整 key / 不开真钱交易
```
v121 当前行为已满足上述红线（靠 prose），刀 1 会把它变成机器可校验字段。

## 当前可签状态
```
V4_5_CLEANROOM_PASS = NO
READY_FOR_CLOUD_UPLOAD = NO
FULL_CHAIN_TAKEOVER_100_PERCENT = NO
原因：审计对象（v999）不可达；v121 自身 FAIL>0（见 BUG_LIST）；无云端 live verify。
```
