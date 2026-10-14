# 保留两项 P0 的升级流程

维护分支：`codex/iphone-use-p0-security`。
初始上游基线：`5cc2fa8808187797d73aea2167104f370dc29724`（0.3.12）。
本地修复版：0.3.13。这里的版本和基线是历史起点，不表示上游永远没有更新。

## 升级前

1. 检查 `git status`，提交已完成的源码改动。保留本地报告和无关文件。
2. 给已验证的提交建立回退 tag；另存 `git bundle` 或源码包到仓库外。
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
| 暂停阻断及本人确认恢复 | `server/wda_access.py`、Runtime/WDA client、`server/wda_screen.py`、CLI 输出与 `ui/src/app.ts` | `tests/test_access.py`、screen/protocol 测试、`ui/tests/widget.test.mjs` |

认证暂停应阻断树、截图、动作、批处理和刷新；旧队列、在途响应和旧帧不能
在暂停或恢复后泄露。恢复必须由用户亲自确认同一次暂停；取消、超时或状态
损坏时保持暂停。不要让模型通过工具参数声明“用户已确认”。

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

只有运行代码或安装资源改变时才生成新的本地版本号并安装。同步插件清单、
服务器版本、UI 版本及依赖锁文件，构建前端后运行 `sh scripts/install.sh`。
安装脚本不会自动将本地修复移植到其他版本。

## 安装后的验收

1. 核对已安装目录内文件与该维护提交的发布文件哈希一致。
2. 重连或新开 Codex 聊天，确认实际加载新版工具和屏幕资源。
3. 按 `iphone-use-setup` 查询状态，复用有效服务和签名；有明确构建失效原因时
   才重建 WDA。不要为了插件文档更新重建手机服务。
4. 获取 `ready=true`，并核对 `network_hardened=true`；两个监听器均为
   `127.0.0.1`，控制与 MJPEG 的实际 USB local endpoint 也一致。
5. 改动了暂停链路、工具入口或 UI 时，在用户可以接手的时段运行
   `python3 scripts/verify_p0_device.py`。用户本人依次选择“保持暂停”和
   “恢复手机访问”；脚本核对暂停期间无手机请求、取消保持暂停、确认后恢复。
   不自动点击确认窗口。
6. 保存本次版本、提交、测试、文件核对和真机结果。没有实测的项目明确标注；
   之前的验收不能冒充本次验收。

LAN 探测失败不是单独的防护证据，特别是修复前也失败时。网络验收边界和
同一 Mac 用户任意代码权限的限制见 `NETWORK_SECURITY.md`。

## 回退

从已验证的 tag 创建新目录，或从仓库外的 bundle 克隆，再安装该版本。先备份
当前工作，避免覆盖本地改动。保留用户原有设备、Team、bundle ID 和运行目录；
源码回退不保证所有 WDA 版本的构建可互换，按状态检查有效构建。
