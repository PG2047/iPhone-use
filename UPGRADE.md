# 保留两项 P0 的升级流程

维护分支：`codex/iphone-use-p0-security`。
初始上游基线：`5cc2fa8808187797d73aea2167104f370dc29724`（0.3.12）。
本地修复版：0.3.13。这里的版本和基线是历史起点，不表示上游永远没有更新。
当前本地版本：0.3.1506，基于上游 0.3.15。0.3.17 是旧编号的最后一个，之后按「本地版本号规则」一节编号。

## 升级前

1. 检查 `git status`，提交已完成的源码改动。保留本地报告和无关文件。
2. 给已验证的提交建立回退 tag，命名为 `local-<version>`；另存 `git bundle`
   或源码包到仓库外。
3. 确认 `upstream` 指向作者仓库，再运行 `git fetch upstream --tags`，查看
   `git log HEAD..upstream/main` 和实际差异。
   没有新提交时，记录检查结果即可，不重装同一份运行代码。
4. 有更新时在维护分支合并，或从维护分支创建独立升级分支后合并。
   解决冲突时保留行为，不以整文件选择上游或本地代替检查。

## GitHub 仓库与远端

- 作者仓库：`https://github.com/zhongerxin/iPhone-use`，用于获取上游更新。
- 用户维护仓库：`https://github.com/PG2047/iPhone-use`，发布维护分支与图文手册。
- 在本次安装目录中，`origin` 仍指向作者仓库；`personal` 指向用户维护仓库。
  从派生仓库新克隆时，`origin` 通常指向自己的仓库，不要据此误判上游没有更新。

先运行 `git remote -v` 检查名称与地址。若尚无 `upstream`，添加作者仓库：

```sh
git remote add upstream https://github.com/zhongerxin/iPhone-use.git
git fetch upstream --tags
git log HEAD..upstream/main --oneline
```

确认差异后在维护分支合并 `upstream/main`。向自己的维护仓库推送已验证提交，
不要把发布手册等同于向作者仓库提交 Pull Request。

## 两项修复的检查入口

| 修复 | 实现入口 | 回归检查 |
|---|---|---|
| 手机服务仅监听回环地址 | `server/patches/wda-16.14.0-loopback.patch`、`server/wda_setup.py`、READY 网络校验 | `tests/test_network_hardening.py`、`tests/fixtures/wda_loopback_probe.m`、`NETWORK_SECURITY.md` |
| 暂停阻断及手机认证自动恢复 | `server/wda_access.py`、Runtime/WDA client、`server/wda_screen.py`、CLI 输出与 `ui/src/app.ts` | `tests/test_access.py`、screen/protocol 测试、`ui/tests/widget.test.mjs` |

认证暂停应阻断树、截图、动作、批处理和刷新；旧队列、在途响应和旧帧不能
在暂停或恢复后泄露。默认由 `wait_auth` 连续检测手机解锁、目标 App 和页面标记后恢复同一次暂停；
不要求用户回到 Mac 确认。普通读取和操作仍被阻断，探测不返回字段值或图片。
通道异常、空页面或新的暂停不能当作认证完成。`resume` 是自动检测的兼容别名，不再创建 Mac 确认窗口。

WDA 固定版本改变时，重新审查并适配补丁，更新固定 commit 和补丁 SHA-256，
执行原生测试，再重建 WDA。旧构建标记不能用于未经核验的新二进制。

## 本机检查

在源码根目录运行：

```sh
npm run build --prefix ui
IPHONE_USE_WDA_SOURCE="$HOME/.local/share/iphone-use/runtime/WebDriverAgent" sh scripts/check.sh
git diff --check
```

`IPHONE_USE_WDA_SOURCE` 必须指向对应固定版本的原始 WDA 源码。它用于编译并
执行真实 Network.framework transport 测试；不要把 native test 被跳过记成通过。
用户更改运行目录时使用实际目录，不照抄上面的默认路径。

只有运行代码或安装资源改变时才生成新的本地版本号并安装，编号规则和要改的
文件见下一节。构建前端后运行 `sh scripts/install.sh`。
安装脚本不会自动将本地修复移植到其他版本。

## 本地版本号规则

本地版本号格式为 `0.<上游 minor>.<上游 patch × 100 + 两位本地序号>`：前两段跟随
已合并进维护分支的上游版本，第三段是上游 patch 号后接两位本地序号，从 01 起。

| 情况 | 本地版本 |
|---|---|
| 合并上游 0.3.15 后第一次安装 | `0.3.1501` |
| 同一上游基线上再次改动运行代码 | `0.3.1502` |
| 合并上游 0.3.16 后 | `0.3.1601` |

- 本地版本永远不等于任何上游版本串。合并上游时 `plugin.json` 等文件的版本冲突
  一律保留本地并按新基线重新编号，不接受上游的版本串。
- 只允许三段十进制数字：`scripts/package.py` 的校验是 `\d+\.\d+\.\d+`，预发布
  后缀、构建元数据和四段号都会被拒绝。Codex 只要求版本串非空，并把它用作缓存
  目录名 `~/.codex/plugins/cache/iphone-use-local/iphone-use/<version>/`、MCP 注册
  路径和 widget 资源 URI，所以同名重装不会让已连接的聊天和宿主缓存的页面换成
  新代码。
- 回退 tag 命名为 `local-<version>`，与上游分开；仓库里的 `v0.3.13` 到 `v0.3.15`
  是上游发布 tag，不要复用。
- 0.3.13 到 0.3.17 是旧编号，与上游同号不同内容，已安装和已打 tag 的不回头改；
  从合并上游 0.3.15 的那次改动起使用新规则，即 `0.3.1501`。

换版本号时同步修改以下位置，然后构建前端、运行本机检查、安装并重连聊天：

| 位置 | 内容 |
|---|---|
| `plugin.json`、`.codex-plugin/plugin.json` | `version` |
| `ui/package.json`、`ui/package-lock.json` | `version`，lock 文件有两处 |
| `server/iphone_use.py` | `VERSION` 和 `SCREEN_URI` 里的 `phone-<version>.html` |
| `ui/src/app.ts` | MCP App 客户端自述版本；改后 `npm run build --prefix ui` 并提交 `assets/phone-screen.html` |
| `CHANGELOG.md`、`README.md`、`README.en.md` | 新版本条目和「本地 x.y.z」小节 |

## 租约接入点清单（合并上游时核对）

锁库 `server/device_lease.py` 和根目录的 `DEVICE_LEASE.md` 是 phonebooth 参考实现的原样副本，
不在这里改：在 `~/Downloads/Codex/phonebooth` 改完、测完，再运行
`python3 ~/Downloads/Codex/phonebooth/scripts/vendor.py .` 同步，`--check` 核对是否逐字节相同。
iPhone Use 自己的策略（锁目录、哪些工具取锁、哪些改变屏幕、哪些算观察、报错措辞）在
`server/lease_policy.py`。上游不会碰这三个文件。接入点只有下面几处，合并上游后逐项确认仍在，
再跑 `tests/test_device_lease.py`（它端到端走 `Runtime.call`，接入点丢失会直接变红）：

| 位置 | 内容 |
|---|---|
| `server/iphone_use.py` 顶部 | `from device_lease import DeviceLease, LeaseError` 和 `from lease_policy import OBSERVING_TOOLS, TRANSPORT, is_mutation, lease_directory, needs_lease, message as lease_message` |
| `Runtime.__init__` | `self.lease=DeviceLease.get(lease_directory(self.state_dir),transport=TRANSPORT)` |
| `Runtime._dispatch` | 预览工具栏 Home / 截图走 `self.leased(..., wait_seconds=0)`；`pua_device` 分支；`with self.leased(...)` 包住 `_operate`，之后 `_after_operation` |
| `Runtime.leased / _after_operation / device / _operate` | 四个方法整体属于租约接入 |
| `Runtime.close` | `self.lease.close()` |
| `SCHEMAS["device"]`、`DESCRIPTIONS["device"]`、`READS` | `pua_device` 工具定义，含 `quiet`：回合结束归还的 Codex Stop hook 靠它只回 `{}`，丢了 hook 会被 Codex 记为 Failed |
| `INSTRUCTIONS` | 末尾的多对话一段 |
| `server/wda_access.py` | `begin()` 的 safe 名单含 `pua_device` |
| `scripts/phone.py` | `--session` |
| `scripts/package.py` | 工具计数 21 / 19，`DEVICE_LEASE.md` 在 FILES 里 |
| `tests/test_tool_loading.py` | 目录预算 31700 / 22600 |
| `skills/`、`ui/src/app.ts` | 多对话段落与 `device_held` / `device_queued` 文案 |

## 安装后的验收

1. 核对已安装目录内文件与该维护提交的发布文件哈希一致。
2. 重连或新开 Codex 聊天，确认实际加载新版工具和屏幕资源。
3. 按 `iphone-use-setup` 查询状态，复用有效服务和签名；有明确构建失效原因时
   才重建 WDA。不要为了插件文档更新重建手机服务。
4. 获取 `ready=true`，并核对 `network_hardened=true`；两个监听器均为
   `127.0.0.1`，控制与 MJPEG 的实际 USB local endpoint 也一致。
5. 改动暂停链路后运行 `tests/test_auth_wait.py` 和原有 access / screen 测试。
   在用户实际遇到认证时，验证 `pause → wait_auth → fresh observation`，只记录
   状态和结果，不记录密码或输入画面。`verify_p0_device.py` 使用明确的目标 App 和页面标记验收，不再包含 Mac 确认步骤。
6. 保存本次版本、提交、测试、文件核对和真机结果。没有实测的项目明确标注；
   之前的验收不能冒充本次验收。

LAN 探测失败不是单独的防护证据，特别是修复前也失败时。网络验收边界和
同一 Mac 用户任意代码权限的限制见 `NETWORK_SECURITY.md`。

## 回退

从已验证的 tag 创建新目录，或从仓库外的 bundle 克隆，再安装该版本。先备份
当前工作，避免覆盖本地改动。保留用户原有设备、Team、bundle ID 和运行目录；
源码回退不保证所有 WDA 版本的构建可互换，按状态检查有效构建。
