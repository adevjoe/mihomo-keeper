# Changelog

## [Unreleased]

### Added

- Dedicated manager tokens for browser auto-login links and Bearer API authentication, alongside existing HTTP Basic authentication.
- Signed HttpOnly / SameSite=Strict sessions (seven days by default), logout, token rotation, and query redaction in access logs.
- Installer-generated `manager.token`, environment settings, authentication integration tests, and Chinese / English documentation.
- `tests/test_update_subscription.py`：用桩 `systemctl` / `mihomo` 和本地假 controller 覆盖 `update-subscription.sh` 的生效路径（热重载、回退重启、`restart` 模式、服务未运行、无 secret、配置未变、非法 `MIHOMO_APPLY_MODE`）。
### Changed

- **`update-subscription.sh` 默认改为热重载生效，不再重启 mihomo**：新配置通过 controller 的 `PUT /configs?force=false` 原地加载。
  开了 `tun` 的机器上，`systemctl restart` 会销毁重建 tun 设备，`tun.auto-route` 的路由/策略规则随之重建，同机的其它隧道会把这当成链路变化 —— Tailscale 会 `LinkChange: major, rebinding` 并重置所有 peer 路径，正在经它转发的 exit node / subnet router 连接随之断开；订阅每小时自动更新就等于每小时断一次。
  热重载失败（controller 不可达、服务未运行等）自动回退 `systemctl restart`，并在日志中写明本次由 `reload` 还是 `restart` 生效。新增 `MIHOMO_APPLY_MODE`（`reload` / `restart`，默认 `reload`）可恢复旧行为。
  同时把脚本末尾读取 controller 地址与 secret 的那段内联 Python 提取为 `read_controller_meta`，复用于热重载和 `/version` 检查。
- 外部面板跳转地址改为可配置：新增 `MANAGER_DASHBOARD_URL`（支持 `{host}` / `{port}` / `{secret}` 占位符）和 `MANAGER_DASHBOARD_NAME`。
  **去掉了默认的 metacubexd 跳转**（原来固定为 `http://<host>:<controller 端口>/ui/`）：未配置时顶部不再显示面板链接；`/api/status` 也只在模板使用 `{secret}` 时才返回 controller secret。

## [0.1.0] - 2026-11-17

首个开源整理版本。功能与作者自用版本一致，只做了开源所需的整理和少量改动：

### Changed（为开源所做的改动）

- **订阅地址不再写死在 `update-subscription.sh`**：改为从环境变量 `MIHOMO_SUB_URL` 或文件 `$MIHOMO_DIR/subscription.url`（`MIHOMO_SUB_URL_FILE`）读取；未配置时脚本以退出码 2 报错，管理页给出提示。
  `manager.py` 仍兼容旧部署中写死在脚本里的 `SUB_URL='https://...'`（只认 http/https 开头的值）。
- `update-subscription.sh`：`MIHOMO_DIR`、`MIHOMO_BIN`、`MIHOMO_SERVICE`、`MERGE_SCRIPT`、`BACKUP_KEEP` 改为可用环境变量覆盖（默认值与原来相同）；`merge-config.py` 默认在脚本同目录查找；controller secret 为空时不再依赖数组越界行为。
- `manager.py`：
  - 新增可配置项：`MIHOMO_SERVICE`（原写死 `mihomo`）、`MANAGER_USER`（原写死 `admin`）、`MANAGER_PASSWORD`（可替代密码文件）、`MANAGER_SECRET_FILE`、`MIHOMO_SUB_URL` / `MIHOMO_SUB_URL_FILE`、`MERGE_SCRIPT`、`UPDATE_SCRIPT`。
  - `merge-config.py` / `update-subscription.sh` 默认位置由 `$MIHOMO_DIR` 改为 `manager.py` 所在目录（按推荐方式部署在 `/etc/mihomo` 时两者相同）。
  - 调用 `update-subscription.sh` 时传入同一套路径配置和订阅地址，保证脚本与管理页一致。
  - Basic 认证比较改为按字节比较，非 ASCII 用户名/密码不再触发异常。
- 默认监听地址保持 `0.0.0.0:9092`，README 中补充了安全提示。

### Added

- `README.md` / `README.en.md`、`LICENSE`（MIT）、`.gitignore`、`requirements.txt`
- `override.example.yaml`：脱敏的 override 示例
- `deploy/`：systemd 单元（`mihomo-manager.service`、`mihomo.service`、`mihomo-sub-update.service` / `.timer`）、`manager.env.example`、`install.sh`
