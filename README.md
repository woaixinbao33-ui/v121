# V121 Cloud APEX — 腾讯云 CVM 部署指南

> 本服务用于个人记录百家乐对局与做风险控制提示。
> **不保证盈利**：B/P 序列在统计上接近独立同分布，没有任何阈值组合能稳定战胜庄家边际。任何 APEX 模块（L85A 可预测性、Monolith DMR/KEP、Qimen 体制治理）都不会改变这个数学事实，它们只重新分配胜率方差。
> 部署前请确认你处于允许的合规场景。

## 升级说明（V121_CLOUD_2 → V121_CLOUD_APEX_PRO_4）

- 默认行为与 `V121_CLOUD_2` 完全一致：所有 APEX / APEX_PRO 模块的开关默认 `false`。
- 升级**无需迁移数据库**：APEX 衍生指标（predictability_label / dmr_action / kep_units / risk_coeff / regime_confidence / drift_level / meta_gate / risk_governor_reason）随 decide() 响应返回，hands 表沿用旧 schema。
- `/ai/tune` 现在会**持久化**到 `table_state(__config__)`，重启后保留。要彻底回滚到字面默认，运行 `POST /ai/config/reset` 然后 `systemctl restart v121`。
- 新增三套静态页：`terminal.html`（5 台手机录入）、`console.html`（调参控制台）、`admin.html`（管理后台 / 锁桌 / CSV 导出），全部部署在 `/opt/v121/` 同目录。
- 新增 `admin.py` 模块：管理员密码登录 + cookie 会话 + 5 桌锁定 + CSV 导出 + 一键解锁应急路由。
- 服务端所有 message 已全中文化，前端模式名也有完整翻译表，UI 不再出现裸英文 mode。

## 0. 你需要的东西

- 一台腾讯云 CVM（Ubuntu 22.04 / 24.04，1c2g 起）
- 一个域名解析到这台 CVM（用于签 HTTPS 证书）
- 安全组放行：`22`（限自家 IP）、`80`、`443`。**不要**把 `8000` 暴露公网。

## 1. 安装系统依赖

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip nginx certbot python3-certbot-nginx
```

创建专用用户与目录：

```bash
sudo useradd --system --create-home --home-dir /opt/v121 --shell /usr/sbin/nologin v121
sudo mkdir -p /opt/v121
sudo chown -R v121:v121 /opt/v121
```

## 2. 上传代码

```bash
sudo -u v121 git clone https://github.com/woaixinbao33-ui/v121.git /opt/v121/src
sudo -u v121 cp /opt/v121/src/server.py        /opt/v121/server.py
sudo -u v121 cp /opt/v121/src/admin.py         /opt/v121/admin.py
sudo -u v121 cp /opt/v121/src/terminal.html    /opt/v121/terminal.html
sudo -u v121 cp /opt/v121/src/console.html     /opt/v121/console.html
sudo -u v121 cp /opt/v121/src/admin.html       /opt/v121/admin.html
sudo -u v121 cp /opt/v121/src/requirements.txt /opt/v121/requirements.txt
```

## 3. 创建虚拟环境

```bash
sudo -u v121 python3 -m venv /opt/v121/.venv
sudo -u v121 /opt/v121/.venv/bin/pip install --upgrade pip
sudo -u v121 /opt/v121/.venv/bin/pip install -r /opt/v121/requirements.txt
```

## 4. 写环境变量文件（**API Key 必须从这里来，不要写进代码**）

```bash
sudo install -m 600 -o root -g root /dev/null /etc/v121.env
sudo tee /etc/v121.env > /dev/null <<EOF
V121_API_KEY=$(openssl rand -hex 24)
V121_ADMIN_PASSWORD=$(openssl rand -hex 6)
V121_DB_PATH=/opt/v121/v121.db
V121_TERMINAL_HTML=/opt/v121/terminal.html
V121_CONSOLE_HTML=/opt/v121/console.html
V121_ADMIN_HTML=/opt/v121/admin.html
V121_HOST=127.0.0.1
V121_PORT=8000
V121_BANKER_COMMISSION=0.05
EOF
```

`V121_API_KEY` 是手机端 / 调参控制台用的；`V121_ADMIN_PASSWORD` 是管理后台用的，两把钥匙独立。两者都至少 6 位，**密码不再有任何默认值，没配置就启动失败**（这是故意的）。

把生成的 `V121_API_KEY` 抄到本机安全的密码管理器里，**这是手机端登录用的 key**。

## 5. 安装 systemd 服务

```bash
sudo cp /opt/v121/src/deploy/v121.service /etc/systemd/system/v121.service
sudo systemctl daemon-reload
sudo systemctl enable --now v121
sudo systemctl status v121
```

本地 smoke test：

```bash
curl http://127.0.0.1:8000/healthz
```

应当返回 `{"ok":true,"version":"V121_CLOUD_2",...}`。

## 6. 配置 nginx + HTTPS

```bash
sudo cp /opt/v121/src/deploy/nginx.conf /etc/nginx/conf.d/v121.conf
# 把里面的 v121.example.com 替换成你的真实域名
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d v121.example.com
```

certbot 会自动改写 nginx 配置并续签证书。

## 7. 在 5 台手机上登录

每台手机用浏览器打开：

```
https://v121.example.com/terminal
```

在登录页输入：

- **API Key**：第 4 步生成的那个 hex 字符串
- **后端地址**：留空（同域）

API Key 只保存在浏览器 sessionStorage，关闭标签页就清掉，不会写进 HTML 源码。

5 台手机分别选 `T1` ~ `T5`，互不干扰。终端底部有「调参控制台」按钮，会在新标签打开 `/console`，使用同一把 API Key。

## 7.5 调参控制台（云端后台优化窗口）

URL：

```
https://v121.example.com/console
```

四个 Tab：

- **参数**：所有 CONFIG 字段，含 APEX 总开关（apex_enabled / l85a_enabled / qimen_enabled / monolith_enabled / dmr_enabled / kep_enabled）。改完点「保存到云」会调 `POST /ai/tune`，落地到 `table_state(__config__)`，重启不丢。
- **桌台**：`GET /ai/snapshot`，5 张桌的实时状态（手数 / 盈亏 / 冻结 / 上一手决策 / L85A / DMR / 尾序列）。
- **报表**：`GET /ai/report`，区分本会话（in-memory）与历史（DB hands 表）。Wilson 置信区间基于历史结算手数计算。
- **最近**：`GET /ai/recent_hands`，最近 30 手 raw 流水，可过滤桌台。

**APEX 调试推荐顺序：**

1. 先开 `apex_enabled`，其余保持 OFF — 终端会显示 regime / 置信度，但不会改变任何下注。
2. 再开 `l85a_enabled`，`l85a_score_weight` 从 0.1 起步 — 只是给评分加微调。
3. 再开 `qimen_enabled` — 在 CHAOS / MIXED 体制下自动缩注，不会反向。
4. 最后才考虑 `monolith_enabled` + `dmr_enabled`。把 `dmr_conflict_action` 留在 `BLOCK`，只让 DMR 拦截而非反向。`FOLLOW_DMR` 会让 DMR 直接覆盖 V121 的方向，方差变大，不要长期开。
5. `kep_enabled` 默认 `kep_max_multiplier=1.0` 即关闭，调到 1.5 才会真正放大注码。

**调参控制台不变量校验：**

`POST /ai/tune` 服务端会重新校验合并后的 CONFIG，违反任一约束直接返回 400：
- `tau_lo < tau_hi`
- `small_fill_lo < small_fill_hi`
- `score_small ≤ score_full`
- `base_bet ≤ max_bet`
- `small_bet_size ≤ max_bet`
- `phase1_hands ≤ phase2_hands`

## 8. 常用运维

```bash
# 查日志
sudo journalctl -u v121 -f

# 重启
sudo systemctl restart v121

# 备份 DB
sudo cp /opt/v121/v121.db /opt/v121/backup/v121.$(date +%F).db

# 轮换 API Key（5 分钟停机窗口）
sudo sed -i "s|^V121_API_KEY=.*|V121_API_KEY=$(openssl rand -hex 24)|" /etc/v121.env
sudo systemctl restart v121
# 然后让 5 台手机重新登录
```

## 9. 安全自检清单

- [ ] `/etc/v121.env` 权限是 `600 root:root`
- [ ] 8000 端口只监听 `127.0.0.1`，安全组未放公网
- [ ] nginx 配置里 `Strict-Transport-Security` / `X-Frame-Options` 已生效
- [ ] `terminal.html` / `console.html` 里**搜不到** `V121_API_KEY` 的实际值（只有占位符）
- [ ] git 提交里**搜不到**任何旧的 hex key（之前那个 `971e3ea4...` 视为已泄露，必须作废）
- [ ] CVM 系统已开启自动安全更新：`sudo dpkg-reconfigure -plow unattended-upgrades`
- [ ] nginx `limit_req_zone` 已在 http{} 块内定义（见 `deploy/nginx.conf` 注释）
- [ ] 备份策略：每日 cron `cp /opt/v121/v121.db /opt/v121/backup/v121.$(date +%F).db`，保留 30 天

## 10. 说明：为什么不能多 worker / 不能横向扩展

`server.py` 里 `TABLES` 是进程内字典，`asyncio.Lock` 也是进程内的。 多 worker 会让请求被分到不同进程,各自看到不同状态，PnL 必错。 想横向扩展请把 `TABLES` 搬到 Redis，把 `TABLE_LOCKS` 改成 Redis 分布式锁——不在本仓库范围。

5 台手机的并发对单进程来说轻松。

## 11. 全部 API 路由

| 路由 | 方法 | 鉴权 | 用途 |
|---|---|---|---|
| `/` | GET | 否 | 健康占位 |
| `/healthz` | GET | 否 | 探活，给 nginx / 监控 |
| `/terminal` | GET | 否 | 5 台手机录入终端（HTML） |
| `/console` | GET | 否 | 调参控制台（HTML） |
| `/v100/heartbeat` | POST | X-API-Key | 桌台心跳 + 当前状态 |
| `/v100/outcome` | POST | X-API-Key | 录入 B/P/T，返回新决策 |
| `/v100/settle` | POST | X-API-Key | 用 decision_id 结算 WIN/LOSS/TIE |
| `/v100/new_shoe` | POST | X-API-Key | 开新靴（旧靴入库） |
| `/v100/rollback` | POST | X-API-Key | 回退最后一手（仅序列，不退 PnL） |
| `/ai/tune` | POST | X-API-Key | 写 CONFIG，落地持久化 |
| `/ai/config` | GET | X-API-Key | 读 CONFIG |
| `/ai/config/reset` | POST | X-API-Key | 清空持久化覆盖（重启生效） |
| `/ai/snapshot` | GET | X-API-Key | 5 张桌实时状态 |
| `/ai/recent_hands` | GET | X-API-Key | 最近 N 手流水（可滤桌台） |
| `/ai/report` | GET | X-API-Key | 本会话 + 历史汇总报表 |
| `/ai/shoes` | GET | X-API-Key | 最近 50 靴 |
| `/ai/export_shoes` | GET | X-API-Key | 全靴 id.序列 文本导出 |

## 12. 管理后台 `/admin`

URL：

```
https://v121.example.com/admin
```

输入第 4 步生成的 `V121_ADMIN_PASSWORD`，登录后看到 5 桌总览：今日手数、今日盈亏、活跃时间、锁状态。

可用操作：

- **锁定**：写入操作（`/v100/outcome` / `/v100/settle`）会立即返回 HTTP 423，前端显示「桌台已被管理员锁定」。
- **解锁**：恢复写入。
- **一键解锁全部**：右上角红色按钮，避免 5 桌都被锁导致管理员自锁。
- **下载今日 CSV**：导出该桌全部 hands（带 BOM 的 UTF-8，Excel 直接打开中文不乱码）。

安全：

- 密码错误连续 5 次（同 IP，5 分钟窗口）会返回 429。
- 会话 8 小时过期，cookie 是 `httponly + samesite=lax`。
- 应急路由 `POST /admin/api/unlock_all` 用同一 cookie 即可调用。

## 13. APEX_PRO 渐进开启指南

新模块**全部默认关闭**。开启顺序建议：

1. `apex_enabled` → 开总开关，先观察终端 APEX 面板里的 regime / pred_score / mono_state。
2. `pro_drift_enabled` → 漂移监控（PSI），WARN 不影响下注，BLOCK 才静默。
3. `pro_calibration_enabled` → 不影响下注，仅用于 `/ai/calibration` 自检页。
4. `l85a_enabled` → L85A 评分加权进 score，weight 从 0.1 起步。
5. `qimen_enabled` → 注码风险系数，CHAOS 自动缩 0.6。
6. `pro_meta_enabled` → 元决策闸门，开后会增加 META_WAIT/META_FREEZE 拒签。
7. `pro_tau_dynamic_enabled` → 漂移/回撤会自动放宽 tau，候选窗口减少。
8. `pro_risk_governor_enabled` → 单靴 / 单日 / 连错三段封顶。
9. **最后**才考虑 `monolith_enabled` + `dmr_enabled`，且 `dmr_conflict_action` 留 `BLOCK`。`FOLLOW_DMR` 会反向覆盖 V121 方向，方差极大，仅供回测。
10. `kep_enabled` 默认 `kep_max_multiplier=1.0` 等于关闭，调到 1.5 才会真正放大注码。

每加一个 flag，建议跑至少 3 靴观察 PnL 与 hands 表，再决定是否保留。

## 14. 小白零基础：把代码推到 GitHub

### 一次性准备工作

1. 注册 GitHub 账号 → https://github.com/join
2. 在 https://github.com/woaixinbao33-ui/v121 打开你的仓库（已经存在）
3. 右上角 头像 → **Settings** → **Developer settings** → **Personal access tokens (classic)** → **Generate new token (classic)**
   - Note 写：`v121-push`
   - Expiration 选 90 days
   - 勾 `repo` 一项即可
   - 点 Generate，**复制保存**这串 `ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`（只显示一次！）

4. 在你电脑（Mac/Linux/WSL）打开终端，配置 git：
   ```bash
   sudo apt install -y git           # 没有 git 才需要
   git config --global user.name  "你的GitHub用户名"
   git config --global user.email "你的GitHub邮箱"
   ```

### 拿到本仓库代码

```bash
cd ~
git clone https://github.com/woaixinbao33-ui/v121.git
cd v121
git checkout claude/integrate-apex-engine-A4hGR   # 切到本次升级分支
```

### 推送（首次会让你输用户名 / token）

```bash
git push -u origin claude/integrate-apex-engine-A4hGR
# Username: 你的GitHub用户名
# Password: 粘贴 ghp_xxx 那串（不是 GitHub 登录密码！）
```

### 在 GitHub 找你的代码

仓库地址 → **https://github.com/woaixinbao33-ui/v121**

- `main` 分支 = 旧版本（V121_CLOUD_2）
- `claude/integrate-apex-engine-A4hGR` 分支 = 本次升级（V121_CLOUD_APEX_PRO_4 + 管理后台）

每个文件点进去都能看到「最近一次提交说明 + 行号 + 复制按钮」。

推送成功后浏览器顶部会自动弹出 "Compare & pull request"，点击直接建 PR；如果没弹，自己点 **Pull requests** → **New pull request**，base 选 `main`、compare 选这条分支，标题/描述写改了什么，提交即可。

## 15. APEX_PRO 模块概览（决策链路）

```
record outcome → decide()
  ├─ phase_of(bp_hand_no)            → EXPLORE / ANALYZE / HARVEST
  ├─ calc_bias(seq[-20])
  ├─ calc_regime(seq[-18])           → TREND_B/P / OSC / CHAOS / MIXED
  ├─ regime_metrics(seq, regime)     [APEX]      → confidence, transition_risk
  ├─ pro_drift_check(seq)            [APEX_PRO]  → OK/WARN/BLOCK + PSI
  ├─ freeze / stop_loss / collect_min governance
  ├─ pro_dynamic_tau(state, psi)     [APEX_PRO]  → 自适应 tau_lo/hi
  ├─ calc_pred(seq[-24])             → p_cal, pred_score
  ├─ V121 信号                       → side, confidence, source
  ├─ l85a_predictability(...)        [APEX·L85A] → 0..1 + 标签
  ├─ mono_state_of(seq, side)        → MONO_SAME / MONO_OPPOSITE / INACTIVE
  ├─ MONO_OPPOSITE block
  ├─ pred_mode LOW_ONLY block
  ├─ score_calc(...)                 + L85A 加权
  ├─ dmr_advise(...)                 [APEX·DMR]  → BLOCK 或覆盖 side
  ├─ kep_units(...)                  [APEX·KEP]  → 注码×倍数
  ├─ qimen_risk_coeff(...)           [APEX·Qimen]→ 注码×风险系数
  ├─ pro_meta_gate(features)         [APEX_PRO]  → PASS_HIGH/LOW/WAIT/FREEZE
  └─ pro_risk_governor(state, bet)   [APEX_PRO]  → Kelly/单靴/连错封顶
```

每个 APEX / APEX_PRO 步骤独立 flag，关闭即等同于直通透传。

## 16. 自动驯化 + 回测 + 训练包

### 自动驯化触发

每开新靴时，如果**总靴数**命中 `auto_train_milestones` 里的任一值（默认 `[10, 30, 50, 100, 200, 500, 1000]`），系统会自动跑一次全库回测并把结果存进 `training_runs` 表。

- 默认 `auto_train_enabled=True`，可以在 `/console` 里关掉。
- 自动驯化跑在 `/v100/new_shoe` 同一线程内，是**毫秒级**的纯 SQL 聚合，不会卡录入。
- 触发后，新一手 decide() 响应里会带 `auto_train_triggered: {id, shoe_count, trigger}`。

### 在管理后台查看回测

`/admin` → 顶部「回测驯化」Tab。功能：

- 「立即回测」：扫一遍 `hands` 表，按 截图同款的 5 段输出（总体 / source / regime / pred / mono）。
- 「下载训练包」：把回测 JSON / 报告 TXT / BP 序列 / CSV 流水 / CONFIG 快照 / AI 提示词模板 全部打包成 ZIP。
- 「存档为驯化记录」：把当前回测手动写入 `training_runs`（trigger=`manual`）。
- 历史区列出最近 20 条驯化记录，含触发类型、靴数、注次、胜率、PnL。
- 「全靴 BP TXT」/「TSV」直接拉走 BP 序列文件（与本地 Pythonista 一键脚本同格式）。
- 「一键复制」把报告复制到剪贴板，或下载 `.txt` 用于发给 AI。

### 训练包 ZIP 内容

```
v121_training_bundle_YYYYMMDD_HHMMSS.zip
├── overall.json           总体回测（含 Wilson 区间）
├── by_source.json         信号来源分组（PREDICT / SMALL_BET / DMR_OVERRIDE / ...）
├── by_regime.json         体制（TREND_B/P / OSC / CHAOS / MIXED）
├── by_pred_bucket.json    可预测分桶（<0.35 / 0.35-0.50 / 0.50-0.72 / >=0.72）
├── by_mono.json           Monolith 状态（SAME / OPPOSITE / INACTIVE）
├── backtest_full.json     上述 5 项的合并版
├── backtest_report.txt    人类可读汇总（与手机终端截图同格式）
├── bp_training.txt        全靴 BP 序列：序号.台桌.日期.BPBPB...
├── bp_training.tsv        同上 + 元数据列（pandas/Excel 可读）
├── recent_hands.csv       最近 500 手原始流水（带 BOM，Excel 中文不乱码）
├── config.json            当前 CONFIG 快照
├── ai_prompt.md           发给 ChatGPT/Claude 的标准提问模板
└── README.txt             包内文件说明
```

## 17. 把训练包发给 AI 大模型优化（小白零基础步骤）

### 步骤一：在手机或电脑打开管理后台

```
https://v121.example.com/admin
```

输入管理员密码登录 → 顶部点「回测驯化」 Tab → 点「下载训练包」按钮 → 浏览器会下载一个 `v121_training_bundle_xxx.zip`。

### 步骤二：解压

- iPhone：用「文件」App 长按 ZIP → 解压。
- Android：用 RAR / ZArchiver 解压。
- 电脑：双击解压。

### 步骤三：打开 AI 大模型

支持任一：

- ChatGPT (https://chat.openai.com)
- Claude (https://claude.ai)
- Gemini (https://gemini.google.com)
- 通义千问 / Kimi / DeepSeek 等国内大模型

### 步骤四：上传 + 粘贴提示词

1. 在 AI 对话框上传以下文件作为附件（一次拖进去）：
   ```
   overall.json
   by_source.json
   by_regime.json
   by_pred_bucket.json
   by_mono.json
   backtest_report.txt
   config.json
   ```
2. 打开解压出的 `ai_prompt.md`，**全选 → 复制 → 粘贴到 AI 对话框**。
3. 发送，等待 AI 输出诊断 + 必改清单 + 应急封禁 + 风险治理建议。

### 步骤五：把建议落地

AI 会给你一个 JSON 块，例如：

```json
{
  "tau_hi": 0.575,
  "qimen_chaos_coeff": 0.5,
  "pro_drift_enabled": true,
  "score_small": 50
}
```

打开 `/console` → 顶部「参数」Tab → 把 AI 给的每个键的新值填进对应输入框 → 点「保存到云」。

服务端会校验所有不变量（tau_lo &lt; tau_hi 之类），不通过会返回 400。

### 步骤六：观察 3 ~ 5 靴

回到「回测驯化」Tab，跑一次「立即回测」对比 PnL / 胜率 / 最大回撤。每改一组参数**至少跑满 3 靴**再决定是否保留。

### 关键提醒

> **AI 不能改变赌场的庄家边际**。它给的建议只是在重新分配方差与暴露，目标是「同样亏损下更小回撤」「同样下注次数下更高命中率」。
>
> 如果 AI 在样本数 < 200 注的情况下给你「鼓吹」高胜率建议，**忽略它**——那是噪声。
>
> 任何要求你「翻倍 / 追损 / 马丁格尔 / 强制补仓」的建议，**直接拒绝**——本系统的风险治理层会硬封顶。

## 18. 全部环境变量

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `V121_API_KEY` | ✅ | — | 手机端 / 调参控制台 X-API-Key |
| `V121_ADMIN_PASSWORD` | ✅ | — | 管理后台密码（≥6 位）|
| `V121_DB_PATH` |  | `/opt/v121/v121.db` | SQLite 文件路径 |
| `V121_TERMINAL_HTML` |  | `/opt/v121/terminal.html` | 手机端静态页 |
| `V121_CONSOLE_HTML` |  | `/opt/v121/console.html` | 调参控制台静态页 |
| `V121_ADMIN_HTML` |  | `/opt/v121/admin.html` | 管理后台静态页 |
| `V121_BANKER_COMMISSION` |  | `0.05` | 庄家佣金（5%）|
| `V121_HOST` |  | `127.0.0.1` | uvicorn 监听地址 |
| `V121_PORT` |  | `8000` | uvicorn 端口 |

## 19. 回测 / 驯化 / 训练包路由速查

| 路由 | 方法 | 鉴权 | 用途 |
|---|---|---|---|
| `/admin/api/backtest` | GET | cookie | 立即回测，返回 JSON（同截图格式）|
| `/admin/api/backtest/report` | GET | cookie | 同上，返回 text/plain（可一键发 AI）|
| `/admin/api/training_run` | POST | cookie | 手动跑一次回测并存表（trigger=manual）|
| `/admin/api/training_runs` | GET | cookie | 最近 N 条驯化记录（含 auto_10/30/50/100）|
| `/admin/api/training_runs/{id}` | GET | cookie | 单条驯化记录的完整 JSON |
| `/admin/api/training_bundle` | GET | cookie | 下载完整训练包 ZIP |
| `/admin/api/export_bp_training` | GET | cookie | 全靴 BP 序列 TXT |
| `/admin/api/export_bp_training/{tid}` | GET | cookie | 单桌 BP 序列 TXT |
| `/admin/api/export_bp_tsv` | GET | cookie | 全靴 BP TSV（带元数据列）|

可选 `?table_id=T1` 参数过滤单桌。

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `V121_API_KEY` | ✅ | — | 手机端 / 调参控制台 X-API-Key |
| `V121_ADMIN_PASSWORD` | ✅ | — | 管理后台密码（≥6 位）|
| `V121_DB_PATH` |  | `/opt/v121/v121.db` | SQLite 文件路径 |
| `V121_TERMINAL_HTML` |  | `/opt/v121/terminal.html` | 手机端静态页 |
| `V121_CONSOLE_HTML` |  | `/opt/v121/console.html` | 调参控制台静态页 |
| `V121_ADMIN_HTML` |  | `/opt/v121/admin.html` | 管理后台静态页 |
| `V121_BANKER_COMMISSION` |  | `0.05` | 庄家佣金（5%）|
| `V121_HOST` |  | `127.0.0.1` | uvicorn 监听地址 |
| `V121_PORT` |  | `8000` | uvicorn 端口 |
