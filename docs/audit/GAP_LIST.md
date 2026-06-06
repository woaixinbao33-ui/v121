# GAP_LIST — 相对指令要求的缺口

> GAP = 指令要求但 v121 不具备的能力。区分两类：
> (A) 合理且应补的工程缺口；(B) 指令假设 v999 结构、但 v121 体系根本不同的"不适用"项。

## A 类：应补的真实缺口

### GAP-A1 — API 响应无机器可校验红线字段
- 指令要求所有响应含 `paper_only / NOT_REAL_BET / real_trading=false / final_go=false / alpha_generator=false / candidate_can_override_main=false`。
- v121 现状：合规靠 docstring（`server.py:14-19`）与 README（`README.md:2-4`）的 prose，**API JSON 里没有这些布尔字段**。`decide()` 返回（`server.py:356-367`）也没有。
- 建议：加一个 `redline_envelope()`，在 `/`, `/healthz`, `/v100/*`, `/ai/*` 响应统一注入红线常量。低风险、高价值，可作为 v121 的"刀 1"。

### GAP-A2 — 无 deploy 工件（systemd unit / nginx conf）
- 见 BUG-02。README 引用但缺失。

### GAP-A3 — 无任何测试 / 无 CI
- 仓库无 `tests/`、无 `.github/workflows/`。`decide()` 是核心逻辑却无单测；TIE push、freeze、stop_loss、idempotent settle 等关键路径无回归保护。
- 建议：补 pytest（纯函数 `decide`/`calc_*`/`wilson` 易测）+ 一个最小 GitHub Actions。可用 `session-start-hook` 技能配置 web 会话的测试/lint。

### GAP-A4 — report 口径不一致
- 见 BUG-05。属正确性缺口。

### GAP-A5 — 调参无审计、无持久化
- `/ai/tune` 改完即丢，重启回默认；无变更日志。与"诚实记录"目标不符。

## B 类：指令假设 v999 结构，在 v121 不适用（不予伪造）

| 指令产物 / 要求 | v121 状态 | 说明 |
|---|---|---|
| `ADMIN_7_CARD_ANCHOR_AUDIT.md`（admin 7 卡 `.tabs`） | N/A | 仓库无 `admin.html`，无后台前端 |
| `MOBILE_DECISION_MATRIX_ANCHOR_AUDIT.md`（`.mcell`/`mMain`/`FINAL_135`） | N/A | `terminal.html` 是极简单页，无主决策矩阵 DOM、无路单 |
| `AI_OWNER_API_AUDIT.md`（14 个 `/admin/api/ai-owner/*`） | N/A | 无 ai_owner / ai_brain / DeepSeek 模块，无 secrets store |
| `PARAM_S0_S5_ALIGNMENT_AUDIT.md`（S0-S5 / z_entry / action_target_profiles.json） | N/A | v121 用 `tau_lo/tau_hi`，无 z_entry、无档位、无 profiles 文件 |
| `PATCH_DRIFT_FORENSIC.md`（历史补丁污染） | N/A | v121 是单文件干净实现，无补丁层、无 overlay/dock/sidecar 可取证 |
| `ENGINE_WIRING_MAP`（V3000/R4B/R5 多候选发动机） | 部分 N/A | v121 只有单一 `decide()`，已在 ENGINE_WIRING_MAP.md 如实绘出 |
| sidecar 注册 `register_ai_owner_routes` | N/A | v121 路由直接挂在 `app`，无 sidecar 模式 |
| DeepSeek key `/opt/v999_api/secrets/...` | N/A | v121 只有 `V121_API_KEY` 环境变量 |
| `/mobile/api/omega/state/T1-T5` 等 | N/A | v121 路由是 `/v100/*` 与 `/ai/*` |

**这些不是 v121 的 BUG。** 是指令把另一个仓库（v999-cloud-cleanroom）的结构套到了 v121 上。若确需这些能力，属于"新建一个系统"，见 `CLEANROOM_REBUILD_PLAN.md`。

## S0-S5 对齐核对（指令第九章）
- 单一真源 `data/action_target_profiles.json`：❌ 不存在。
- `/mobile/api/research/action-profiles/T1-T5`、`/admin/api/research/action-target-matrix/T1-T5`：❌ 路由不存在。
- 结论：`S0_S5_PARAM_ALIGNMENT = N/A`（无对象可对齐），**不能签 PASS**。
