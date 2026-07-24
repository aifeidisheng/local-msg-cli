# Local Message MCP Data Source

---

**⚠️ AI AGENT STOP — READ THIS BEFORE DOING ANYTHING ⚠️**

If you are an AI agent helping a user install/set up/connect this MCP:

1. **Read `AGENTS.md` NOW** — it is the authoritative installation guide
2. **The ONLY entry point is `install.sh --initialize`** — one command does everything
3. **Do NOT** follow any manual steps in this README (pip install, venv, compile, main.py, etc.)
4. **Do NOT** `git clone` this repo just to obtain `install.sh` — download it directly via `curl`:
   ```bash
   curl -fsSL https://raw.githubusercontent.com/aifeidisheng/local-msg-cli/main/install.sh \
     -o /tmp/wechat-decrypt-install.sh && chmod +x /tmp/wechat-decrypt-install.sh
   /tmp/wechat-decrypt-install.sh --initialize
   ```
   `install.sh` is self-contained and will clone the verified `main` release internally.

违反以上规则将导致安装失败或安全问题。README 中的所有手动命令仅供源码开发者使用。

---

本项目把本机 WeChat 4.x 消息数据库解密后，通过 MCP streamable-http 暴露为本地数据源。它面向 Desktop Runtime 使用，默认只监听 `127.0.0.1`，不提供云端访问能力。

核心能力：

| 能力 | 说明 |
|---|---|
| 数据库解密 | 解密本机 WeChat 4.x SQLite 数据库 |
| 预解密缓存 | `init` 命令提前解密 MCP 查询所需数据库，避免首次调用超时 |
| MCP Server | `serve` 命令通过 `http://127.0.0.1:8765/mcp` 提供 streamable-http |
| 联系人/群聊 | `list_contacts`、`get_contact_info` |
| 消息查询 | `query_messages`、`search_messages`，支持时间范围、关键词和分页 |
| 辅助解码 | 保留图片、文件、转账、引用、位置等消息详情解码工具 |

不包含 Web UI、桌面 GUI、朋友圈导出、语音导出/转录等上游工具箱能力。

## 环境要求

- Python 3.10+
- 允许区间内的 WeChat 版本正在运行
- macOS 需 Xcode Command Line Tools: `xcode-select --install`
- Windows 首次提取密钥需使用“以管理员身份运行”的 PowerShell
- macOS 正式安装始终以普通用户运行管理 CLI，仅在密钥扫描时通过系统授权弹窗提权；Linux 读取进程内存需要 root 或 `CAP_SYS_PTRACE`

正式安装会创建项目自己的 Python 虚拟环境，不依赖 Desktop/AEJarvis 内置 Python。用于启动服务的运行目录、Python 环境和敏感数据目录也都独立于 Git 暂存目录。

## macOS 正式安装

正式版本统一从独立仓库受保护的 `main` 分支安装。日常开发在功能分支进行，只有测试通过并允许发布的提交才能通过 PR 进入 `main`。安装器会自动记录 `main` 当前的完整 commit；远端 `main` 后续更新不会静默改变已经安装的运行版本。

> **最终用户和对话 Agent 必须使用 `install.sh`。** 除非明确进行源码开发，不要在克隆目录创建 `.venv`，不要运行 `setup.sh`、`setup.py`、`main.py init` 或手动编译/执行密钥扫描器。

从仓库任意工作树运行统一引导入口即可。引导脚本不会部署当前工作树；它会重新拉取并校验受保护的 `main`，然后交给正式安装器：

```bash
./install.sh --initialize
```

如团队维护了经过确认的备用发布源，可显式传入；只有主源不可达时才会使用备用源：

```bash
./install.sh --initialize \
  --repository 'https://github.com/aifeidisheng/local-msg-cli.git' \
  --fallback-repository 'https://gitee.com/aifeidisheng/local-msg-cli.git'
```

`main` 必须禁止 force push 和删除，并限制为通过测试的 PR 更新。安装器会校验实际克隆的 `origin` 是否位于用户确认的可信源列表中，并校验 `origin/main`、当前 `HEAD` 和干净工作树。最终完整 commit、可信源列表和本次实际使用的源都会写入本机安装记录。

目前没有经过项目方确认且可公开访问的国内镜像；不要使用随机 GitHub 代理、`ghproxy` 或通过全局 `url.*.insteadOf` 静默改写仓库地址。建议由项目方创建并持续同步公开镜像，例如 `https://gitee.com/aifeidisheng/local-msg-cli.git`，确认可匿名读取后再取消上面示例中的注释。安装和升级会按列表顺序尝试；主源不可达时才切换备用源，并固定备用源实际返回的完整 commit。

Git 网络操作会重试一次，并在持续低于 1 KiB/s 达 15 秒时快速失败。升级失败会返回 `git_source_unreachable` 或 `all_git_sources_unreachable`，不会要求用户手动重复拼接 clone 命令。Python 依赖安装也会自动重试；如团队已确认 PyPI 镜像，可在安装前通过标准 `PIP_INDEX_URL` 提供，不会被安装器硬编码覆盖。

安装器会完成以下工作：

- 复核 `origin`、`main` 发布通道、完整 commit 和干净工作树。
- 部署到 `~/Library/Application Support/WeChatDecryptLight/runtime/<commit>/`。
- 创建该版本独立的 `.venv`，安装项目固定版本的直接依赖并编译本地扫描器。
- 将配置、密钥和解密缓存保存在独立 `data/` 目录，升级时不覆盖已有数据。
- 安装用户级 LaunchAgent，并核对 launchd PID 与监听端口 PID。
- 生成稳定管理入口 `~/Library/Application Support/WeChatDecryptLight/bin/wechat-decrypt-light`。
- 记录安装 commit，支持检查和升级到 `main` 的最新提交。
- 记录主发布源、备用发布源和本次实际使用源；更新时自动切换不可达的发布源。

密钥提取和数据库预解密是安装器中的敏感步骤。Agent 必须先说明会出现 macOS 系统管理员授权弹窗并取得用户明确确认，然后直接执行上面的 `./install.sh --initialize`。安装器仅以管理员权限运行内置 C 扫描器；管理 CLI、配置、缓存和 LaunchAgent 始终以当前登录用户运行。

`initialize` 会先以普通用户权限完成数据目录、版本门禁、微信进程和 ad-hoc 签名预检；预检失败不会弹出授权窗口。全部预检通过后，单次初始化最多调用一次 `osascript`，该授权只覆盖扫描器本身。不要直接拼接额外的 `osascript` 命令，也不要为修改 `config.json`、`all_keys.json` 或文件所有权再次请求密码；旧流程遗留的单文件所有权问题会由管理 CLI 在普通用户上下文中原子修复。

安装入口的 stdout 始终只输出一条 JSON。失败结果中的 `phase` 标识失败阶段；当 `install_complete: true`、`initialize_complete: false` 且 `phase: "initialize"` 时，表示运行时和 LaunchAgent 已安装成功，不应重新下载或重装。按 `error_code` 完成恢复动作后，只重试下面的已安装管理入口即可。

仅当初始化已经执行过但返回结构化错误，并且用户按 `error_code` 对应动作处理完问题后，才使用已安装的管理 CLI 重试初始化：

```bash
"$HOME/Library/Application Support/WeChatDecryptLight/bin/wechat-decrypt-light" --json initialize
```

不要在上述命令前添加 `sudo`。如果错误使用 `sudo`，管理 CLI 会拒绝运行并返回 `management_cli_must_not_run_as_root`。在系统弹窗中授权即可；扫描器会把 `all_keys.json` 直接写入独立 `data/` 目录，不需要打开终端执行额外扫描命令、移动密钥文件、修改目录所有者或重新加载 LaunchAgent。

如果返回 `wechat_not_adhoc_signed`，先退出 WeChat，执行 `sudo codesign --force --deep --sign - /Applications/WeChat.app`，再重新打开并登录。之后只需重新执行同一条 `initialize` 命令。JSON 结果中的 `error_code` 和 `next_action` 是失败恢复的唯一依据；不要跳转到源码安装或手工扫描流程。

只有返回的 `service.status` 为 `ready`，才可以把 `http://127.0.0.1:8765/mcp` 注册到 mcporter。`waiting_for_wechat` 表示常驻机制正常，但 MCP 尚不可调用，不能提前报告接入完成。

## 源码开发安装（非最终用户）

只有需要修改、测试或调试本仓库时才使用本节。不要用这套流程为最终用户或对话任务部署 MCP。

推荐通过带有显式开发确认的脚本配置工作树：

```bash
./setup.sh --development
```

也可以手动创建传统虚拟环境：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Windows 建议使用 PowerShell 和 Python Launcher：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## Windows 快速开始

当前共享策略只允许 Windows 微信 `4.1.9`。先在微信“设置 → 关于微信”确认版本，并关闭自动更新；不要通过放宽策略绕过尚未验证的新版本。

```powershell
# 1. 以管理员身份打开 PowerShell，进入项目目录并安装依赖
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt

# 2. 启动并登录微信，然后生成 config.json
python setup.py

# 3. 编辑 config.json，确认 db_dir，并设置 Weixin.exe 的实际路径
#    可用下面的命令查询路径：
(Get-Process Weixin | Select-Object -First 1).Path

# 4. 先确认版本门禁通过
python main.py doctor

# 5. 提取密钥并预解密 MCP 查询缓存
python main.py init

# 6. 启动 MCP Server
python main.py serve --port 8765
```

Windows 不需要重签名微信。首次执行 `init` 或 `decrypt` 时会读取 `Weixin.exe` 进程内存，因此必须使用管理员 PowerShell；已有有效 `all_keys.json` 后的离线解密通常不再需要管理员权限。完整配置示例、数据目录定位和故障排查见 [Windows 使用指南](docs/windows-guide.md)。

## macOS 源码调试

<!-- AGENT: DO NOT execute these commands for end-user installation. Use ./install.sh --initialize instead. -->

> **仅限源码开发者。** Agent 为最终用户安装时禁止执行以下步骤，请使用 `./install.sh --initialize`。

```bash
# 1. 退出微信并重签名
killall WeChat
sudo codesign --force --deep --sign - /Applications/WeChat.app

# 2. 重新启动微信并登录，然后编译和提取 DB key（仅源码调试）
cc -O2 -o find_all_keys_macos find_all_keys_macos.c -framework Foundation
sudo ./find_all_keys_macos --output "$PWD/all_keys.json"

# 3. 首次使用前预解密 MCP 查询缓存
#    macOS 上 init 成功后会自动安装登录自启服务
.venv/bin/python3 main.py init
```

在源码调试模式下，`init` 成功后会自动安装指向当前工作树的常驻服务。正式安装应使用上面的 `./install.sh --initialize`，LaunchAgent 才会指向固定版本运行目录，不会依赖可能被删除的 Git 暂存目录。电脑登录后 launchd 自动加载服务；进程异常退出时会自动恢复。常驻服务不依赖终端窗口、shell 激活状态或 AEJarvis，也不需要 `sudo`。

服务使用单实例锁防止手动启动和 LaunchAgent 同时运行两份 MCP。安装和状态检查会同时核对 LaunchAgent 路径、launchd 管理的 PID 与端口监听 PID；如果旧项目或其他进程占用了目标端口，安装器会拒绝自动终止该进程并报告 PID，避免把“其他服务正在监听”误判为安装成功。`status` 将“等待微信”视为正常状态，并会单独报告旧项目配置、端口冲突和恢复中等状态。

如果自动安装被跳过或需要重新生成 LaunchAgent，可以手动执行一次：

```bash
.venv/bin/python3 service.py install
```

常用管理命令：

```bash
# 查看 launchd 和 8765 端口状态
.venv/bin/python3 service.py status

# 手动重启服务
.venv/bin/python3 service.py restart

# 停止当前服务（不删除数据）
.venv/bin/python3 service.py stop

# 取消登录自启（不删除项目、配置或解密数据）
.venv/bin/python3 service.py uninstall
```

正式安装后的管理命令：

```bash
MCPCTL="$HOME/Library/Application Support/WeChatDecryptLight/bin/wechat-decrypt-light"

"$MCPCTL" status --json
"$MCPCTL" check-update --json
"$MCPCTL" upgrade --json
"$MCPCTL" repair --json
"$MCPCTL" uninstall --json
# 同时删除版本运行目录，但仍保留 data/ 中的敏感数据
"$MCPCTL" uninstall --remove-runtime --json
```

`check-update` 只查询远端 `main` 并比较本机安装 commit，不下载代码、不修改运行目录。返回 `update_available=true` 后，只有用户明确确认才执行 `upgrade`。升级会浅克隆最新 `main`、重新校验来源并部署新的固定 commit；配置、密钥和解密缓存继续保存在独立数据目录，安装或服务验证失败时保留原运行版本。

服务日志位于：`~/Library/Logs/WeChatDecryptLight/`。如果服务未启动，优先查看 `mcp.stderr.log`。

## Desktop MCP 配置

在 Desktop 客户端中添加本机 MCP 工具：

| 配置项 | 值 |
|---|---|
| 类型 | `streamablehttp` |
| 地址 | `http://127.0.0.1:8765/mcp` |
| Runtime | Desktop |

Cloud Runtime 无法连接用户本机的 `localhost`，使用本数据源的任务必须在 Desktop 在线时运行。`/sse` 不是当前主链路。

## 常用命令

| 用途 | 命令 |
|---|---|
| 配置向导 | `python setup.py` |
| 环境检查 | `python setup.py --check` |
| 查看状态 | `python main.py status` |
| 检查版本门禁 | `python main.py doctor` |
| 检查代码更新 | `python main.py update --check` |
| 执行代码更新 | `python main.py update` |
| 首次预解密 MCP 缓存 | `python main.py init` |
| 仅预解密指定数据库 | `python main.py init --target-db MSG` |
| 启动 MCP Server | `python main.py serve --port 8765` |
| 启动前自动更新后再起服务 | `python main.py serve --auto-update --port 8765` |
| 解密全部数据库到目录 | `python main.py decrypt` |
| 批量导出聊天记录 | `python export_all_chats.py` |
| 批量解密图片 | `python main.py decode-images` |

## 服务自更新

如果本机 MCP 服务是直接从 git 工作区启动，可以启用启动前自动更新：

```bash
python main.py serve --auto-update --port 8765
```

更新感知规则：

- 工作区必须干净；有未提交改动时拒绝自动更新
- 当前分支必须已跟踪远端 upstream
- 只允许 `git pull --ff-only`，不做自动合并
- 如果本地领先远端、与远端分叉、网络拉取失败，都会跳过自动更新并继续使用当前代码启动

单独检查是否有更新：

```bash
python main.py update --check
```

检查命令退出码：

- `0`：已是最新
- `3`：检测到可更新的远端提交
- `2`：工作区不干净、分支分叉、未配置 upstream、拉取失败等不安全状态

## MCP 工具

| 工具 | 功能 |
|---|---|
| `data_source_status()` | 不读取用户数据的安装验收工具，检查联系人库和消息库是否可只读访问 |
| `list_contacts(query, limit)` | 列出或搜索联系人和群聊，返回可传给查询工具的 `id` |
| `query_messages(chat_id, start_time, end_time, keyword, limit, offset)` | 按明确时间范围查询指定联系人或群聊的消息 |
| `search_messages(keyword, chat_name, start_time, end_time, limit, offset)` | 跨聊天或指定聊天搜索关键词 |
| `get_contact_info(contact_id)` | 获取联系人或群聊的本地元数据 |
| `get_recent_sessions(limit)` | 查看最近会话摘要 |
| `get_chat_history(...)` | 兼容旧名称的聊天历史查询 |
| `get_contacts(...)` | 兼容旧名称的联系人搜索 |
| `get_chat_images(...)` / `decode_image(...)` | 图片消息检索和解密 |
| `decode_file_message(...)` | 文件消息解码 |
| `decode_transfer(...)` / `decode_refer(...)` / `decode_location(...)` | 转账、引用、位置等结构化详情 |

安装接入验收应调用 `data_source_status`，不要通过读取联系人或消息来测试连通性。该工具只返回 `ready/not_ready`、初始化状态、数据库可访问性和消息分片数量，不返回联系人、聊天 ID、消息、密钥或本机路径。

`query_messages` 要求传入明确的 `start_time`，大时间跨度建议分段查询。返回内容来自本机历史消息，不代表实时数据；需要实时状态时重新查询。

## 配置

程序会自动检测微信数据目录并生成 `config.json`。这个文件只建议保存本机运行配置，不建议提交到 git。手动创建时可保持最小结构：

```json
{
  "db_dir": "/path/to/your/wxid/db_storage",
  "keys_file": "all_keys.json",
  "decrypted_dir": "decrypted",
  "decoded_image_dir": "decoded_images",
  "wechat_process": "WeChat"
}
```

如果要启用共享版本门禁，请把规则放到仓库内的 `version-guard.policy.json`：

```json
{
  "version_guard": {
    "enabled": true,
    "block_on_unknown_version": true,
    "require_update_disabled": false,
    "allowed_version_ranges": [
      {
        "platform": "windows",
        "min_version": "4.1.9",
        "max_version": "4.1.9"
      },
      {
        "platform": "darwin",
        "min_version": "4.1.8",
        "max_version": "4.1.8"
      }
    ]
  }
}
```

生产环境应启用 `version_guard.enabled=true` 并填写 `allowed_version_ranges`。当只允许单一版本时，可把 `min_version` 和 `max_version` 配成相同值；如果后续确认多个连续版本都安全，再适当放宽区间。共享版本规则建议提交 `version-guard.policy.json`，本机 `config.json` 继续只保存 `db_dir`、key、本机路径等运行态信息。`wechat_app_path`、`installer_path`、`installer_sha256` 仍然支持放在本机 `config.json` 中，但默认门禁只关注真实版本号，不再强制要求运行中的微信必须来自某个固定安装目录，也不再依赖安装包 hash 校验；`wechat_app_path` 留空时程序也会尝试从运行中的微信进程自动发现。`build_version` 当前只作为 `doctor` 的诊断信息，不作为主门禁条件。

门禁策略文件会在真正的敏感操作和 MCP 数据访问前做完整性校验。生产流程只接受仓库默认位置的 `version-guard.policy.json`，并使用 MCP 代码内置的规范化 JSON SHA-256 摘要；自定义策略路径、环境变量注入策略和运行时摘要覆盖均不受支持。这样 Windows 的 LF/CRLF 换行转换不会造成误报，但修改策略内容仍会 fail-closed。

`doctor` 是只读诊断命令：即使版本不匹配或策略完整性失败，也只报告问题并明确不会执行密钥提取、解密或查询。不要为了让诊断命令返回“通过”而修改 `version-guard.policy.json`；需要支持新版本时，应发布包含新策略和新内置摘要的受信任版本。

微信 4.x 的“有更新时自动升级微信”使用微信自己的设置系统，旧 Sparkle plist 中的 `SUEnableAutomaticChecks` 和 `SUAutomaticallyUpdate` 不能反映界面开关。工具不会替用户修改微信设置，也不会把旧字段或 `MacUpdate` 插件是否存在当成真实开关；请在微信“设置 > 通用”中手动关闭自动升级。默认共享策略保持 `require_update_disabled=false`，最终安全边界是实际版本门禁：`init`、`decrypt`、`export`、`all`、`decode-images` 和 MCP 数据访问会在执行前校验真实微信版本；版本未知、不匹配或策略不可信会直接拒绝执行。`serve` 启动前也会检查，避免在未初始化或不兼容版本上暴露数据源。`python main.py doctor` 仅用于安装后诊断。详细设计见 [docs/wechat-version-guard-design.md](docs/wechat-version-guard-design.md)。

各平台默认路径：

- macOS: `~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/<wxid>/db_storage`
- Windows: 微信“设置 → 文件管理”中查看数据根目录，最终选择 `xwechat_files\<wxid>\db_storage`
- Linux: `~/Documents/xwechat_files/<wxid>/db_storage`

## 安全提示

- `all_keys.json` 包含明文 raw key，勿提交到 git 或与人共享。
- 解密后的 `.db` 文件是明文 SQLite，包含联系人、群和消息内容。
- 本工具仅用于分析自己的本机数据。请遵守相关法律法规和软件服务协议。

## License

MIT
