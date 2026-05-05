# v121# V121 Cloud — 腾讯云 CVM 部署指南

> 本服务用于个人记录百家乐对局与做风险控制提示。
> **不保证盈利**：B/P 序列在统计上接近独立同分布，没有任何阈值组合能稳定战胜庄家边际。
> 部署前请确认你处于允许的合规场景。

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

5 台手机分别选 `T1` ~ `T5`，互不干扰。

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
- [ ] `terminal.html` 里**搜不到** `V121_API_KEY` 的实际值（只有占位符）
- [ ] git 提交里**搜不到**任何旧的 hex key（之前那个 `971e3ea4...` 视为已泄露，必须作废）
- [ ] CVM 系统已开启自动安全更新：`sudo dpkg-reconfigure -plow unattended-upgrades`

## 10. 说明：为什么不能多 worker / 不能横向扩展

`server.py` 里 `TABLES` 是进程内字典，`asyncio.Lock` 也是进程内的。 多 worker 会让请求被分到不同进程,各自看到不同状态，PnL 必错。 想横向扩展请把 `TABLES` 搬到 Redis，把 `TABLE_LOCKS` 改成 Redis 分布式锁——不在本仓库范围。

5 台手机的并发对单进程来说轻松。
