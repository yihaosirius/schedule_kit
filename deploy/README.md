# 部署说明

面向一台全新的 **Ubuntu 22.04 / 24.04** 服务器，只有公网 IP、没有域名、
不想开 80/443（避开大陆备案触发条件），且不使用 VPN。

## 部署顺序

按这个顺序做，每步都有明确的成功标志。

### ① 把代码送到服务器（约 2 分钟）

**注意：项目当前不是 git 仓库**，所以 `git clone` 还走不通。三选一：

```bash
# 方式 A（推荐，之后能 git pull 更新）：先在本地建仓并推到私有仓库
#   本地：git init && git add . && git commit -m init && git remote add origin <你的私有仓库>
#   服务器：
git clone <你的私有仓库> schedule_kit && cd schedule_kit

# 方式 B：直接同步（本地执行，需要 rsync）
rsync -av --exclude '.venv' --exclude 'data' --exclude '.git' \
      ./ root@<VPS_IP>:/root/schedule_kit/

# 方式 C：打个包传过去
tar --exclude=.venv --exclude=data --exclude=.git -czf sk.tar.gz .
scp sk.tar.gz root@<VPS_IP>:/root/ && ssh root@<VPS_IP> \
  'mkdir -p /root/schedule_kit && tar -xzf /root/sk.tar.gz -C /root/schedule_kit'
```

**成功标志**：服务器上 `/root/schedule_kit/deploy/install.sh` 存在。

> 不要传 `config.toml` 和 `data/` —— 前者含密钥，后者是本地开发数据。
> 部署脚本会生成全新的生产配置。

### ② 拿到一个免费域名（约 3 分钟）

打开 https://www.duckdns.org ，用 GitHub / Google / Reddit 账号直接登录（无需注册）：

1. 在 `domains` 输入框填一个名字（如 `schedulekit`），点 **add domain**
2. 在列表里把 `current ip` 改成你的 **VPS 公网 IP**，点 **update ip**
3. 复制页面上方那串 **token**（UUID 格式）

> ⚠️ 这个 token 等于你该域名的 DNS 控制权——拿到它的人可以给你的子域签出合法证书。
> 部署脚本会把它写进 `0600` 的 `config.toml`，并且**用隐藏输入读取**，不进 shell 历史。

**已实测的代价**：DuckDNS 的权威 DNS 在境外（解析到 AWS 北美），未缓存解析耗时
**200–780ms**，而国内域名是 **45–90ms**。手机冷启动或切网后首个请求可能多等
0.3–0.6 秒。想消除这个代价就换成方案 C（见文末）。

### ③ 在 VPS 服务商控制台放行入站 TCP 8443（约 2 分钟）

找到「安全组 / 防火墙」，添加入站规则：协议 TCP、端口 **8443**、来源 `0.0.0.0/0`。

**不要开 80 和 443** —— 这正是规避备案的关键。

> 服务商控制台这一层和服务器内部的 `ufw` 是**两道独立的墙**，脚本只能处理后者。

### ④ 在服务器上跑一条命令（约 5 分钟）

```bash
cd /root/schedule_kit
sudo bash deploy/install.sh --init
```

`--init` 会依次询问：域名、ACME 邮箱、DuckDNS Token（隐藏输入）、HTTPS 端口
（默认 8443）、管理员密码。然后自动完成下面全部工作。

## 脚本自动做了什么

| 步骤 | 内容 |
|---|---|
| 1 | 装 `curl` / `ca-certificates` / `ufw` |
| 2 | 建系统账号 `schedulekit` 与目录（`/opt`、`/etc`、`/var/lib`） |
| 3 | 同步代码到 `/opt/schedulekit` |
| 4 | 装 `uv`、Python 3.13、依赖，并 `compileall` 预编译（strict 模式下无需写 pycache） |
| 5 | 生成 `/etc/schedulekit/config.toml`（0600，属服务账号）并跑迁移 |
| 6 | 装 systemd 服务 + 每日备份定时器 |
| 7 | 装 Caddy，用 acme.sh 走 **DNS-01** 签证书，渲染 Caddyfile，放行 8443 |
| 8 | 装 DuckDNS A 记录自更新定时器 |
| 9 | 启动服务并做本机 `/healthz` 健康检查 |

**改完配置重跑即可**：`sudo bash deploy/install.sh`（幂等，不会重复建用户或重签证书）。

## 目录布局

```
/opt/schedulekit/           代码与虚拟环境（root 所有，只读）
/etc/schedulekit/
    config.toml             唯一配置来源（0600，属 schedulekit）
    certs/                  Let's Encrypt 证书
/var/lib/schedulekit/
    schedulekit.db          数据库（含 -wal / -shm）
    uploads/YYYY-MM-DD/     上传的原图
    backups/                每日备份
```

## 端口与安全边界

- **uvicorn 只监听 `127.0.0.1:8000`**，外部无法绕过 Caddy 直连应用。
- Caddy 只监听 **8443**，不开 80/443。
- systemd 加固开着 `ProtectSystem=strict`，仅放开 `/var/lib/schedulekit` 与
  `/etc/schedulekit` 两处写入（后者是控制台保存 `[llm]` 配置所必需）。
- `MemoryMax=256M`：计划目标是常驻 100–140MB，真撞上限会被 systemd 重启，
  比 OOM 拖垮整机好。

## 常用运维命令

```bash
systemctl status schedulekit            # 服务状态
journalctl -u schedulekit -f            # 跟踪日志
journalctl -u schedulekit | grep 't=8f3a2b1c'   # 追一个请求的完整链路
systemctl restart schedulekit           # 改 [server]/[tls] 后需要重启
sudo bash deploy/install.sh             # 改完 config.toml 后重跑（幂等）
/usr/local/bin/schedulekit-backup       # 手动备份一次
```

日志格式与排查方法见 [`../docs/dev-notes.md`](../docs/dev-notes.md) §1。

## 证书续期

acme.sh 安装时会自带 cron，每约 60 天续期，并通过 `--reloadcmd "systemctl reload caddy"`
自动让 Caddy 加载新证书。**不需要人工干预**。

验证续期是否配置正确：

```bash
/root/.acme.sh/acme.sh --list
systemctl list-timers | grep acme
```

## 可能卡住的地方

**① 证书签发失败**

签发有两条腿，报错会区分是哪一段：

- `VPS → www.duckdns.org` 改 TXT 记录失败 → VPS 跨境出站有问题
- `Let's Encrypt → 查 DuckDNS NS` 失败 → 通常等几分钟重试即可

退路：改用 ZeroSSL（脚本里 `--set-default-ca` 处换成 `zerossl`），或直接换方案 C。

**② 外面连不上，但服务器内 `curl` 正常**

服务商拦了非标端口。改端口只需动 `config.toml` 的 `[tls].port` 与安全组两处，
然后重跑脚本。

**③ 解析慢**（就是上面实测那个问题）

升级到**方案 C**：买一个 ¥10/年的域名（国内注册商 + DNSPod/阿里 DNS），
改 `config.toml` 的 `[tls]`，然后重跑脚本。**代码与客户端零改动**。

```toml
[tls]
provider      = "duckdns"       # 保留，仅用于兼容
domain        = "你的域名"
duckdns_token = ""
```

若换成非 DuckDNS 域名，证书签发改用对应 provider 的 acme.sh 插件
（`--dns dns_dp` 或 `--dns dns_ali`），脚本里那一段有注释标明位置。

## 卸载

```bash
systemctl disable --now schedulekit schedulekit-backup.timer schedulekit-duckdns.timer
rm -f /etc/systemd/system/schedulekit*.service /etc/systemd/system/schedulekit*.timer
systemctl daemon-reload
userdel -r schedulekit
rm -rf /opt/schedulekit /etc/schedulekit /var/lib/schedulekit
```

**注意**：`/var/lib/schedulekit` 里有数据库与图片，删之前先确认备份。
