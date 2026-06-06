# ENGINE_WIRING_MAP — v121 真实主决策链接线图

> 仅描述 v121 真实存在的链路。v999 的 `save_hand → 状态机 → feature store → DNA → regime → V3000/R4B/R5 候选 → paper_position_matrix → road_capsule → decision_panel → action_profile → mobile/admin → AI Owner` 多模块发动机链路 **在本仓库不存在**，不予绘制（不猜）。

## 1. 真实主链（录入一手）

```
手机终端 rec(o)                         terminal.html:177-183
  └─ POST /v100/outcome                 terminal.html:180
       └─ post_outcome()                server.py:725-729
            └─ async with TABLE_LOCKS[table_id]   server.py:727   ← 单桌串行
                 └─ get_table()                    server.py:728 / 257-262
                 └─ _apply_outcome(s, outcome)     server.py:729 / 617-637
                      ├─ s.sequence.append / 计数   server.py:618-628
                      ├─ save_event()               server.py:629 / 486-493
                      ├─ d = decide(s)              server.py:630 / 349-467   ← 决策引擎
                      ├─ issue_decision_id()        server.py:631 / 534-546   ← 发纸面决策券
                      ├─ s.last_decision = d        server.py:634
                      ├─ save_hand()                server.py:635 / 496-516
                      └─ _persist_state()           server.py:636 / 247-254
       └─ show(r)                        terminal.html:180 → 147-175  ← 渲染 #dec
```

## 2. 真实结算链（赢/输/和）

```
手机终端 settle(result)                 terminal.html:185-195
  └─ 需 lastDecisionId 非空（WAIT 不可结算）terminal.html:186-189
  └─ POST /v100/settle                   terminal.html:190
       └─ post_settle()                  server.py:732-736
            └─ async with TABLE_LOCKS[table_id]    server.py:734
                 └─ _apply_settlement()            server.py:736 / 640-683
                      ├─ claim_decision()          server.py:641 / 549-566  ← 原子防重复结算
                      ├─ WIN:  庄扣 5% 抽水         server.py:646-654
                      ├─ LOSS: -bet，连输≥2 触发冻结 server.py:655-662
                      ├─ TIE:  B/P 注 push，PnL=0   server.py:663-664
                      ├─ 更新 peak/max_dd           server.py:666-672
                      ├─ 冻结递减（非 LOSS）         server.py:673-674
                      ├─ d = decide(s)（下一手）     server.py:676
                      ├─ issue_decision_id()        server.py:677-679
                      ├─ save_hand()                server.py:681
                      └─ _persist_state()           server.py:682
```

## 3. decide() 内部门控顺序（server.py:349-467）

```
phase_of / calc_bias / calc_regime           server.py:351-354
1. freeze>0           → WAIT(FREEZE)          server.py:387-388
2. shoe_pnl<=stop_loss→ WAIT(STOP_LOSS)       server.py:390-391
3. BP数<collect_min   → WAIT(COLLECTING)      server.py:393-395
4. calc_pred → p_cal                          server.py:397
   p_cal>=tau_hi(0.56) → side=B, PREDICT       server.py:401-402
   p_cal<=tau_lo(0.44) → side=P, PREDICT       server.py:403-404
   小填充区间          → SMALL_BET             server.py:405-408
   否则               → WAIT(NORMAL)          server.py:410-411
5. MONO_OPPOSITE 拦截  → WAIT(MONO_BLOCK)      server.py:415-418
6. LOW_ONLY 且 pred_score>=0.5 → WAIT(PRED_MODE_BLOCK) server.py:420-423
7. score_calc                                  server.py:425
   EXPLORE 阶段 → 强制 SMALL                   server.py:427-440
   sc>=score_full(78) → FULL                   server.py:442-444
   sc>=score_small(42)→ SMALL                  server.py:445-447
   否则 → WAIT(LOW_SCORE)                      server.py:448-451
   bet = min(bet, max_bet)                      server.py:453
```

**关键正确性点（✅）：**
- `decide()` 是状态的纯函数，所有调用都在桌锁内（`_apply_outcome` / `_apply_settlement` / `new_shoe` / `rollback`），无跨桌竞争。
- 决策券（`pending_decisions`）+ `claim_decision` 原子结算，防止重复结算同一决策（`server.py:549-566`）。
- TIE 正确按 push 处理，不计胜负（`server.py:663-664`），与 docstring `server.py:17` 一致。

## 4. 报表/导出链

```
GET /ai/report      server.py:792-820   ← 聚合**仅内存 TABLES**（见 BUG-05）+ shoes 表
GET /ai/shoes       server.py:823-843   ← DB shoes 最近 50 靴
GET /ai/export_shoes server.py:846-853  ← DB shoes 全量序列导出
GET /ai/tune (POST) server.py:780-789   ← 改全局 CONFIG
```

## 5. 与指令期望链路的差异（不是 BUG，是体系差异）

| 指令期望模块 | v121 现实 |
|---|---|
| feature_store / training_lake | ❌ 无；只有 sqlite `events/hands/shoes` |
| DNA / regime / V3000 / R4B / R5 候选 | ❌ 无；只有单一 `decide()` + `calc_regime()` |
| paper_position_matrix / road_capsule | ❌ 无 |
| decision_panel / action_profile | ❌ 无；决策直接由 `decide()` 返回 JSON |
| AI Owner diagnostics | ❌ 无 |
| candidate_can_override_main | ❌ 无候选机制，无覆盖 |

v121 的"发动机"就是 `decide()` 这一个纯函数 + 一套 sqlite 持久化。链路短、可审、无假接管——这反而是它的优点。
