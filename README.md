# mihomo-keeper

一个给 Linux 服务器上的 [mihomo](https://github.com/MetaCubeX/mihomo)（Clash.Meta 内核）用的轻量运维管理页：
**订阅更新 + 本地 override 合并、规则增删、配置备份回滚、服务控制、日志、内核与 GeoIP 数据库更新**，都在一个网页里完成。

- 单文件后端：`manager.py`，只依赖 Python 3.8+ 标准库和 PyYAML，没有前端构建步骤
- 不替代 mihomo 面板：节点测速、连接查看等日常操作仍然交给 [metacubexd](https://github.com/MetaCubeX/metacubexd) 等面板，本项目只管「配置和运维」
- 面向 systemd 部署的单机场景（家用旁路由、小主机、VPS）

[English](README.en.md)

> ⚠️ 管理页能改配置、重启服务、替换内核二进制，必须以 root 运行。请务必阅读下方「安全提示」。

## 截图

> TODO：截图待补充（放在 `docs/screenshots/`）

<!--
| 状态 | 规则 | 更新 |
| --- | --- | --- |
| ![status](docs/screenshots/status.png) | ![rules](docs/screenshots/rules.png) | ![update](docs/screenshots/update.png) |
-->

## 功能

| 页面 | 能做什么 |
| --- | --- |
| 状态 | mihomo 服务状态、内核版本、配置/override 修改时间、备份数量、订阅地址（脱敏显示），一键更新订阅、重载 / 重启 / 校验配置，可选跳转到外部面板（metacubexd 等，需配置 `MANAGER_DASHBOARD_URL`） |
| 规则 | 表单方式编辑 override 中的 `rules-prepend` / `rules-append`，策略组和规则集自动补全，格式校验；保存时只替换对应 YAML 块，保留其他注释 |
| 策略组 | 查看并切换 Selector / URLTest / Fallback 等策略组当前选择（通过 mihomo external-controller） |
| override | 在线编辑 `override.yaml`；「校验」会下载订阅 → 合并 → `mihomo -t`，并给出与当前 config.yaml 的 diff，不落盘；保存时自动保留最近 10 份 override 备份，可选立即应用 |
| config | 只读查看当前生效的 `config.yaml`，支持过滤 |
| 更新 | **内核**：从 MetaCubeX/mihomo GitHub Release 列出版本，按 CPU 微架构（amd64 v1/v2/v3）推荐变体，下载 → `-v` / `-t` 校验 → 备份旧内核 → 替换 → 重启，失败自动回滚；可回滚到旧内核。**GeoIP / ASN 数据库**：从 MetaCubeX/meta-rules-dat（或 config 中的 `geox-url`）更新 `geoip.metadb` / `ASN.mmdb`（以及存在时的 `geosite.dat` / `geoip.dat`），校验 MMDB 元数据，保留 `.bak` 可回滚 |
| 备份 | 列出 `backup/config.yaml.*`，与当前配置 diff、校验后回滚（回滚前会再备份当前配置）、批量删除 |
| 日志 | `journalctl -u mihomo` 实时查看，关键字过滤高亮，自动刷新 |

GitHub 直连下载失败时，内核 / 数据库下载会自动改走本机 mihomo 的 `mixed-port` 代理重试一次。

### 订阅合并规则（merge-config.py）

`update-subscription.sh` 下载订阅后，用 `merge-config.py` 把 `override.yaml` 深合并进去生成 `config.yaml`：

- 字典递归合并，标量以 override 为准；值写 `null` 表示删除该字段
- `proxy-groups` 按 `name` 合并，`name` 支持通配符（fnmatch，如 `"*Auto*"`）批量修改；不含通配符且订阅中不存在的组会被追加
- 规则不要直接写 `rules`（会整体替换），用：
  - `rules-prepend`：插到最前
  - `rules-append`：插到最后的 `MATCH` 规则之前
  - `rules-insert`：`[{before: "<订阅中的某条规则>", rules: [...]}, {after: ..., rules: [...]}]`
- 其他列表整体替换；以 `x-` 开头的顶层键只在 override 内使用（放 YAML 锚点），合并时丢弃

示例见 [`override.example.yaml`](override.example.yaml)。

## 目录结构

```
.
├── manager.py               # Web 管理页（后端 + 内嵌前端）
├── merge-config.py          # 订阅 + override.yaml 合并
├── update-subscription.sh   # 下载订阅 → 合并 → mihomo -t → 备份 → 替换 → 重启
├── override.example.yaml    # 脱敏的 override 示例
├── requirements.txt         # PyYAML
├── deploy/
│   ├── install.sh                 # 一键安装（systemd）
│   ├── manager.env.example        # /etc/default/mihomo-manager 示例
│   ├── mihomo-manager.service     # 管理页
│   ├── mihomo.service             # mihomo 本体（示例）
│   ├── mihomo-sub-update.service  # 订阅更新（oneshot）
│   └── mihomo-sub-update.timer    # 每 6 小时更新一次（可选）
└── docs/screenshots/
```

部署后 `$MIHOMO_DIR`（默认 `/etc/mihomo`）里的运行时文件：

```
/etc/mihomo/
├── manager.py / merge-config.py / update-subscription.sh
├── config.yaml          # 生成的最终配置（勿手改，改 override.yaml）
├── override.yaml        # 你的本地覆盖
├── subscription.url     # 订阅地址（chmod 600）
├── manager.secret       # 管理页密码（chmod 600）
├── backup/              # config.yaml 历史备份
├── geoip.metadb / ASN.mmdb ...
└── ui/                  # 可选：metacubexd 面板
```

## 安装

### 前置条件

- Linux + systemd，root 权限
- Python 3.8+、PyYAML（Debian/Ubuntu：`apt install python3-yaml`；或 `pip3 install -r requirements.txt`）
- `curl`
- 已安装 mihomo 二进制（默认 `/usr/local/bin/mihomo`），可从 [MetaCubeX/mihomo Releases](https://github.com/MetaCubeX/mihomo/releases) 下载；之后可以在管理页里升级

### 一键安装

```bash
git clone https://github.com/adevjoe/mihomo-keeper.git
cd mihomo-keeper

# 订阅地址只在首次安装时写入 /etc/mihomo/subscription.url（也可以之后手动写）
sudo MIHOMO_SUB_URL='https://your-subscription-link' ./deploy/install.sh
# 可选参数：--enable-timer 启用每 6 小时自动更新订阅；--no-start 只安装不启动；
#           --force-units 覆盖已存在的 mihomo.service
```

安装脚本是幂等的，**不会覆盖**已存在的 `override.yaml`、`subscription.url`、`manager.secret`、`/etc/default/mihomo-manager`；首次安装会生成随机密码并打印一次（保存在 `manager.secret`）。

然后：

1. 编辑 `/etc/mihomo/override.yaml`（或在管理页里编辑）
2. 首次生成配置：`sudo systemctl start mihomo-sub-update.service`（或在管理页点「更新订阅」）
3. 浏览器打开 `http://<服务器>:9092`，用户 `admin`，密码见安装输出 / `manager.secret`

### 手动部署（systemd）

```bash
sudo install -d /etc/mihomo
sudo install -m 700 manager.py merge-config.py update-subscription.sh /etc/mihomo/
sudo install -m 600 override.example.yaml /etc/mihomo/override.yaml
echo 'https://your-subscription-link' | sudo tee /etc/mihomo/subscription.url >/dev/null
sudo chmod 600 /etc/mihomo/subscription.url
python3 -c 'import secrets;print(secrets.token_urlsafe(18))' | sudo tee /etc/mihomo/manager.secret >/dev/null
sudo chmod 600 /etc/mihomo/manager.secret

sudo install -m 600 deploy/manager.env.example /etc/default/mihomo-manager
sudo install -m 644 deploy/*.service deploy/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mihomo-manager.service
sudo systemctl enable --now mihomo.service           # 若 mihomo 还没有自己的 unit
sudo systemctl enable --now mihomo-sub-update.timer  # 可选：定时更新订阅
```

### 本地试跑（不需要真实 mihomo）

```bash
pip3 install -r requirements.txt
mkdir -p /tmp/mk && cp override.example.yaml /tmp/mk/override.yaml
MIHOMO_DIR=/tmp/mk MANAGER_LISTEN=127.0.0.1:9092 MANAGER_PASSWORD=test python3 manager.py
# 打开 http://127.0.0.1:9092 （admin / test）；依赖 systemctl / mihomo 的功能会报错，属正常
```

### 搭配 metacubexd 面板

`ui/` 不包含在本仓库中。如需面板：

1. 下载 [metacubexd](https://github.com/MetaCubeX/metacubexd/releases) 的构建产物（`compressed-dist.tgz`），解压到 `/etc/mihomo/ui`
2. 在 `override.yaml` 中设置 `external-controller`、`secret`、`external-ui: ui`
3. 设置 `MANAGER_DASHBOARD_URL`，管理页顶部才会出现面板跳转链接（默认不显示）。地址支持占位符 `{host}`（浏览器访问管理页用的主机名）、`{port}`（controller 端口）、`{secret}`（controller secret），替换时会做 URL 编码，例如：

   ```bash
   # 本机 mihomo 托管的 metacubexd，自动填好连接信息
   MANAGER_DASHBOARD_URL='http://{host}:{port}/ui/#/setup?hostname={host}&port={port}&secret={secret}'
   # 或者直接用公共托管的面板 / 自建地址
   MANAGER_DASHBOARD_URL='https://dash.example.com/'
   MANAGER_DASHBOARD_NAME=metacubexd
   ```

   注意：模板中含 `{secret}` 时，secret 会下发给已登录管理页的浏览器。

## 配置

全部通过环境变量配置，systemd 部署时写在 `/etc/default/mihomo-manager`（见 [`deploy/manager.env.example`](deploy/manager.env.example)），`mihomo-manager.service` 和 `mihomo-sub-update.service` 都会读取。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MIHOMO_DIR` | `/etc/mihomo` | mihomo 工作目录（config / override / backup / geo 数据库） |
| `MIHOMO_BIN` | `/usr/local/bin/mihomo` | mihomo 二进制；内核更新会替换它，旧版本保存为 `<bin>.bak.<版本>`（保留 3 个） |
| `MIHOMO_SERVICE` | `mihomo` | mihomo 的 systemd unit 名 |
| `MANAGER_LISTEN` | `0.0.0.0:9092` | 管理页监听地址 |
| `MANAGER_USER` | `admin` | HTTP Basic 用户名 |
| `MANAGER_PASSWORD` | 空 | 管理页密码；优先于密码文件 |
| `MANAGER_SECRET_FILE` | `$MIHOMO_DIR/manager.secret` | 密码文件（取首行） |
| `MIHOMO_SUB_URL` | 空 | 订阅地址；优先于订阅地址文件 |
| `MIHOMO_SUB_URL_FILE` | `$MIHOMO_DIR/subscription.url` | 订阅地址文件（取首个非注释行） |
| `MERGE_SCRIPT` | 与 manager.py 同目录的 `merge-config.py` | 合并脚本路径 |
| `UPDATE_SCRIPT` | 与 manager.py 同目录的 `update-subscription.sh` | 订阅更新脚本路径 |
| `BACKUP_KEEP` | `20` | `update-subscription.sh` 保留的 config.yaml 备份数 |
| `MANAGER_DASHBOARD_URL` | 空 | 外部面板跳转地址模板，支持 `{host}` / `{port}` / `{secret}`；为空时不显示跳转链接 |
| `MANAGER_DASHBOARD_NAME` | `面板` | 跳转链接显示的名称 |

mihomo 的 controller 地址和 secret 直接从 `config.yaml` 的 `external-controller` / `secret` 读取，无需另配。

## 安全提示

- **默认监听 `0.0.0.0:9092`，整个局域网都能访问。** 管理页以 root 运行，能修改配置、重启服务、下载并替换内核二进制，等同于这台机器的 root 权限。
- **一定要设置强密码**（`manager.secret` 或 `MANAGER_PASSWORD`）。没有密码时管理页会**无认证运行**（启动日志会有 WARN）。
- HTTP Basic 认证是明文传输的：只在可信内网使用；需要跨网络访问时，请改为 `MANAGER_LISTEN=127.0.0.1:9092`，再通过 Nginx / Caddy 等反向代理加 HTTPS（以及额外的访问控制），或通过 VPN / Tailscale 等访问。**不要直接暴露到公网。**
- 状态接口会把 mihomo controller 的 `secret` 返回给已登录的页面（用于一键打开面板），请把管理页密码视为与 controller secret 同等敏感。
- `subscription.url`、`manager.secret`、`override.yaml`、`config.yaml`、`backup/` 都包含敏感信息，保持 `chmod 600/700`，不要提交到任何仓库（本仓库的 `.gitignore` 已排除）。
- 内核更新只允许安装 `https://github.com/MetaCubeX/mihomo/releases/download/` 下的 `.gz` 资产。

## 从旧版（订阅地址写死在脚本里）迁移

旧的 `update-subscription.sh` 把订阅地址写死在 `SUB_URL='...'`。新版脚本改为从环境变量 / `subscription.url` 读取：

```bash
# 在服务器上，把旧脚本里的地址迁移到文件
sudo grep -oP "^SUB_URL='\K[^']+" /etc/mihomo/update-subscription.sh | sudo tee /etc/mihomo/subscription.url >/dev/null
sudo chmod 600 /etc/mihomo/subscription.url
```

之后再用新版脚本替换旧脚本即可。新版 `manager.py` 仍兼容读取旧脚本里的 `SUB_URL='https://...'`。

## 许可证

[MIT](LICENSE)

本项目与 MetaCubeX / mihomo / metacubexd 无官方关联。
