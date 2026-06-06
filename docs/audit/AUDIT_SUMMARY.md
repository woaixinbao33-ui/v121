# 只读 X 光审计 — 总览与签收结论 / Read-only X-ray Audit Summary

> 模式：God Architecture Mode · Clean Room 只读审计 · Release Manager 只读签收
> 阶段：第一阶段（只读，不改业务代码）
> 审计对象（真实 scope）：`woaixinbao33-ui/v121`，工作目录 `/home/user/v121`
> 审计分支：`claude/v999-cleanroom-xray-audit-wJIpg`
> 日期：2026-06-06

---

## 0. 最重要的结论：仓库与指令不匹配 / SCOPE MISMATCH

指令包（`V4_5_X_CLAUDE_ONE_SHOT_MASTER_COMMAND.md` 及聊天文本）描述的目标系统是：

```
仓库 woaixinbao33-ui/v999-cloud-cleanroom
V999 / V3000 / R5 / V4.5.x FULL SNAPSHOT
```

而本次会话真实可访问的仓库是另一个：

```
woaixinbao33-ui/v121    （GitHub scope 锁定，无法访问 v999-cloud-cleanroom）
```

**指令里要求"必须优先读取"的真实文件，在本仓库中一个都不存在。** 实测结果：

| 指令要求读取的文件 | 本仓库是否存在 |
|---|---|
| `README_FULL_SERVER_CODE_SNAPSHOT.md` | ❌ 不存在 |
| `inventory/FULL_SERVER_CODE_INVENTORY.md` | ❌ 不存在 |
| `audit_reports/FULL_CODE_XRAY.md` | ❌ 不存在 |
| `opt_v999_api/releases/r5_v4_2/v999_api_R5.py` | ❌ 不存在 |
| `opt_v999_api/v999_api.py` | ❌ 不存在 |
| `opt_v999_api/modules/ai_owner/` `ai_brain/` | ❌ 不存在 |
| `opt_v999_api/modules/*_v2.py` `*_v4_3.py` | ❌ 不存在 |
| `var_www_html/admin.html` | ❌ 不存在 |
| `var_www_html/mobile.html` | ❌ 不存在 |
| `etc_nginx/` `systemd/` `tmp_scripts/` | ❌ 不存在 |
| `opt_v999_api/verify/` `verify_v455/` | ❌ 不存在 |
| `data/action_target_profiles.json`（S0-S5 真源） | ❌ 不存在 |

本仓库 `v121` 的真实全部内容（`git ls-files`，共 5 个文件）：

```
README.md            4237 B   部署指南（腾讯云 CVM）
gitignore              99 B   （注意：文件名缺少点，见 BUG_LIST）
requirements.txt       60 B   fastapi / uvicorn / pydantic
server.py          28403 B   876 行，单文件 FastAPI 服务
terminal.html       8144 B   241 行，单页手机终端
```

**结论：v121 不是 v999-cloud-cleanroom 的快照，而是一个独立、干净、小得多的项目。** 二者只是同一作者（`woaixinbao33-ui`）、同一业务领域（百家乐纸面记录 / 风险控制提示）。v121 看起来本身就已经是一个"Clean Room"式的最小实现，没有 admin/mobile 双前端、没有 AI Owner、没有 sidecar、没有 7 卡导航、没有 S0-S5 档位、没有补丁污染层。

因此，指令里 12 个审计产物中，针对 v999 的 7 个（`ENGINE_WIRING_MAP` 的 V3000/R4B/R5 多模块链、`ADMIN_7_CARD_ANCHOR_AUDIT`、`MOBILE_DECISION_MATRIX_ANCHOR_AUDIT`、`AI_OWNER_API_AUDIT`、`PARAM_S0_S5_ALIGNMENT_AUDIT`、`PATCH_DRIFT_FORENSIC`）**在本仓库没有审计对象**。我没有伪造它们的"行号锚点"——那会直接违反指令第二章"不准猜 DOM / API / sidecar"和第十二章"不得虚报"。

---

## 1. 我实际做了什么 / What this audit actually covers

我对 **真实存在的 v121 代码** 做了逐行只读审计，产出以下落地报告（均带真实行号）：

- `REAL_STRUCTURE_AUDIT.md` — v121 真实文件结构与锚点
- `ENGINE_WIRING_MAP.md` — v121 真实主决策链接线图（save→decide→settle）
- `BUG_LIST.md` — 真实代码缺陷
- `GAP_LIST.md` — 相对指令红线/字段要求的缺口
- `CLEANROOM_REBUILD_PLAN.md` — 在 v121 现实基础上的诚实改造边界

---

## 2. 红线核对（针对 v121 真实代码）/ Redline check

| 红线 | v121 现状 | 评估 |
|---|---|---|
| `real_trading=false` | 代码无真钱交易路径；`server.py:14-19` docstring 明确 "NOT a profit guarantee"；README 顶部明确"不保证盈利" | ✅ 行为合规，但**无显式布尔字段** |
| `paper_only=true` | 全程只记录 B/P/T 序列与纸面 PnL（`_apply_settlement` `server.py:640-683`），无真实下单 | ✅ 行为合规，但**无显式字段** |
| `NOT_REAL_BET=true` | 同上 | ✅ 行为合规，但**无显式字段** |
| `NO_PROFIT_GUARANTEE` | `server.py:16-19`、`README.md:2-4` 明文声明 | ✅ 有声明 |
| `S0 z_entry=3.0 LOCKED` | **不适用**：v121 用 `tau_lo=0.44 / tau_hi=0.56`（`server.py:53-54`），没有 z_entry，没有 S0-S5 档位 | ⚠️ 体系不同 |
| `candidate_can_override_main=false` | **不适用**：v121 是单一策略 `decide()`，没有候选/主链覆盖机制 | ⚠️ 体系不同 |

关键 GAP：v121 的 API 响应体里**完全没有**指令要求的红线布尔字段（`paper_only` / `NOT_REAL_BET` / `real_trading` / `final_go` 等）。合规性目前靠 prose（docstring + README）承载，不靠机器可校验的 envelope。详见 `GAP_LIST.md`。

---

## 3. 签收结论 / Sign-off — 不能签 YES

按指令第十二章门控，**任何 PASS=YES 都不允许签**，理由如下（不得虚报、不得预签）：

```
V4_5_CLEANROOM_PASS                       = NO
AI_SYSTEM_OWNER_100_PERCENT_TAKEOVER      = NO
READY_FOR_CLOUD_UPLOAD                     = NO
FULL_CHAIN_TAKEOVER_100_PERCENT           = NO
CLOUD_ADMIN_MOBILE_ALIGNMENT              = NO
S0_S5_PARAM_ALIGNMENT                      = NO (N/A — 体系不存在)
```

`FAILED_ITEMS` / `BLOCKED_ITEMS`：

1. 审计对象不可达：`v999-cloud-cleanroom` 不在本会话 GitHub scope 内，无法读取真实源码。
2. 指令要求的全部 v999 文件在 `v121` 中不存在（见上表），无法做行号级审计。
3. 无云端 live verify（无服务器、无 `43.156.21.154` 访问），按指令禁止签 100%。
4. 无 `VERIFY_*.sh` 在真实环境跑出 `FAIL=0`。
5. v121 自身存在若干真实缺陷（见 `BUG_LIST.md`），即使只对 v121 签收也未达 FAIL=0。

---

## 4. 下一步需要你决定 / Next steps (need your decision)

请二选一或都做：

**A. 如果目标真是 v999-cloud-cleanroom：**
   - 需要把该仓库加入本会话 scope（本会话 `claude-code-remote` 的 `list_repos`/`add_repo` 工具未启用；请在 web/app 端把 `v999-cloud-cleanroom` 加为本 session 的源仓库，或新开一个 scope 指向它的 session），我才能对真实源码做行号级 X 光审计。
   - 或者把 `v451_src.tar.gz` / 真实快照上传到本仓库，我就地审计。

**B. 如果目标其实是把 v121 升级成"全链路对齐的纸面研究系统"：**
   - v121 是个干净底座，但要达到指令描述的 admin 7 卡 / mobile 主决策矩阵 / AI Owner / S0-S5 治理，是一次**新建**而非"修补"。`CLEANROOM_REBUILD_PLAN.md` 给出了诚实的工作量边界与三刀计划（基于 v121 现实，而非 v999 假想结构）。

在你确认方向之前，我**没有**改动任何业务代码（`server.py` / `terminal.html` 一行未动），完全符合第一阶段只读要求。
