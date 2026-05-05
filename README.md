# V121 Cloud APEX — 腾讯云 CVM 部署指南

> 本服务用于个人记录百家乐对局与做风险控制提示。
> **不保证盈利**：B/P 序列在统计上接近独立同分布，没有任何阈值组合能稳定战胜庄家边际。任何 APEX 模块（L85A 可预测性、Monolith DMR/KEP、Qimen 体制治理）都不会改变这个数学事实，它们只重新分配胜率方差。
> 部署前请确认你处于允许的合规场景。

## 升级说明（V121_CLOUD_2 → V121_CLOUD_APEX_3）

- 默认行为与 `V121_CLOUD_2` 完全一致：所有 APEX 模块的开关默认 `false`。
- 升级**无需迁移数据库**：APEX 衍生指标（predictability_label / dmr_action / kep_units / risk_coeff / regime_confidence）随 decide() 响应返回，hands 表沿用旧 schema。
- `/ai/tune` 现在会**持久化**到 `table_state(__config__)`，重启后保留。要彻底回滚到字面默认，运行 `POST /ai/config/reset` 然后 `systemctl restart v121`。
- 新增 `console.html` —— 调参控制台，建议放在与 terminal 同一目录。

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
sudo -u v121 cp /opt/v121/src/terminal.html    /opt/v121/terminal.html
sudo -u v121 cp /opt/v121/src/console.html     /opt/v121/console.html
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
V121_DB_PATH=/opt/v121/v121.db
V121_TERMINAL_HTML=/opt/v121/terminal.html
V121_CONSOLE_HTML=/opt/v121/console.html
V121_HOST=127.0.0.1
V121_PORT=8000
V121_BANKER_COMMISSION=0.05
EOF
```

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

## 12. APEX 模块概览（决策链路）

```
record outcome → decide()
  ├─ phase_of(bp_hand_no)            → EXPLORE / ANALYZE / HARVEST
  ├─ calc_bias(seq[-20])
  ├─ calc_regime(seq[-18])           → TREND_B/P / OSC / CHAOS / MIXED
  ├─ regime_metrics(seq, regime)     [APEX] → confidence, transition_risk
  ├─ freeze / stop_loss / collect_min governance
  ├─ calc_pred(seq[-24])             → p_cal, pred_score
  ├─ V121 信号                       → side, confidence, source
  ├─ l85a_predictability(...)        [APEX·L85A] → 0..1 + 标签
  ├─ mono_state_of(seq, side)        → MONO_SAME / MONO_OPPOSITE / INACTIVE
  ├─ MONO_OPPOSITE block
  ├─ pred_mode LOW_ONLY block
  ├─ score_calc(...)                 + L85A 加权
  ├─ dmr_advise(...)                 [APEX·DMR] → BLOCK 或覆盖 side
  ├─ kep_units(...)                  [APEX·KEP] → 注码×倍数
  └─ qimen_risk_coeff(...)           [APEX·Qimen] → 注码×风险系数
```

每个 APEX 步骤独立 flag，关闭即等同于直通透传。
