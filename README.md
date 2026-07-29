# Local Message MCP Data Source

---

**⚠️ AI AGENT STOP — READ THIS BEFORE DOING ANYTHING ⚠️**

If you are an AI agent helping a user install/set up/connect this MCP:

1. **Read `AGENTS.md` NOW** — it is the authoritative installation guide
2. **The ONLY bootstrap entry point is `install.sh --initialize`** — it installs,
   inspects, and stops at the next user interaction boundary
3. **Do NOT** follow any manual steps in this README (pip install, venv, compile, main.py, etc.)
4. **Do NOT** `git clone` this repo just to obtain `install.sh` — download it directly via `curl`:
   ```bash
   curl -fsSL https://gitee.com/feipig_up_tree/local-msg-cli/raw/main/install.sh \
     -o /tmp/wechat-decrypt-install.sh && chmod +x /tmp/wechat-decrypt-install.sh
   /tmp/wechat-decrypt-install.sh \
     --repository '<user-provided-repository-url>' --initialize
   ```

Desktop 宿主如果提供 Python 3.10+，应优先把该解释器传给引导器：

```bash
/tmp/wechat-decrypt-install.sh \
  --repository 'https://gitee.com/feipig_up_tree/local-msg-cli.git' \
  --python "$CODEX_DESKTOP_PYTHON" --initialize
```

这只用于启动安装器；正式服务仍运行在安装器创建的独立 `.venv` 中。
`install.sh` is self-contained and will clone the verified `main` release internally.

如果用户提供了仓库地址，必须在第一次调用 `install.sh` 时显式传入
`--repository`。安装器不会从对话内容、当前工作目录或之前的临时 clone
自动推断仓库；没有提供地址时使用内置的 Gitee 默认发布源。

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
| 可选辅助解码 | `extended` 工具档提供图片、文件、转账、引用、位置等消息详情解码 |

不包含 Web UI、桌面 GUI、朋友圈导出、语音导出/转录等上游工具箱能力。

## 环境要求

- Python 3.10+ (the Desktop host may pass its bundled interpreter to the bootstrap; the installed service uses its own isolated venv)
- 允许区间内的 WeChat 版本；密钥提取阶段需要 WeChat 正在运行并已登录
- macOS 需 Xcode Command Line Tools: `xcode-select --install`
- Windows 首次提取密钥需使用“以管理员身份运行”的 PowerShell
- macOS 正式安装始终以普通用户运行管理 CLI，仅在密钥扫描时通过系统授权弹窗提权；Linux 读取进程内存需要 root 或 `CAP_SYS_PTRACE`

正式安装会创建项目自己的 Python 虚拟环境，不依赖宿主 Desktop 客户端的内置 Python 来运行服务；引导阶段可以复用 Desktop 传入的 Python 解释器，避免因系统 PATH 没有 Python 而失败。用于启动服务的运行目录、Python 环境和敏感数据目录也都独立于 Git 暂存目录。

## macOS 正式安装

正式版本统一从独立仓库受保护的 `main` 分支安装。日常开发在功能分支进行，只有测试通过并允许发布的提交才能通过 PR 进入 `main`。安装器会自动记录 `main` 当前的完整 commit；远端 `main` 后续更新不会静默改变已经安装的运行版本。

> **最终用户和对话 Agent 必须使用 `install.sh`。** 除非明确进行源码开发，不要在克隆目录创建 `.venv`，不要运行 `setup.sh`、`setup.py`、`main.py init` 或手动编译/执行密钥扫描器。

从仓库任意工作树运行统一引导入口即可。引导脚本不会部署当前工作树；它会重新拉取并校验受保护的 `main`，然后交给正式安装器：

```bash
./install.sh \
  --repository 'https://gitee.com/feipig_up_tree/local-msg-cli.git' \
  --initialize
```

`main` 必须禁止 force push 和删除，并限制为通过测试的 PR 更新。安装器会校验实际克隆的 `origin`、`origin/main`、当前 `HEAD` 和干净工作树，并把最终完整 commit 与实际使用的源写入本机安装记录。

引导安装阶段会完成以下工作：

- 复核 `origin`、`main` 发布通道、完整 commit 和干净工作树。
- 部署到 `~/Library/Application Support/WeChatDecryptLight/runtime/<commit>/`。
- 创建该版本独立的 `.venv`，安装项目固定版本的直接依赖并编译本地扫描器。
- 将配置、密钥和解密缓存保存在独立 `data/` 目录，升级时不覆盖已有数据。
- 生成稳定管理入口 `~/Library/Application Support/WeChatDecryptLight/bin/wechat-decrypt-light`。
- 记录安装 commit，支持检查和升级到 `main` 的最新提交。
- 记录实际发布源和安装 commit，升级时继续使用该发布源。

`./install.sh --initialize` 兼容旧 Agent 的入口名称，但不再把提权初始化和服务安装串成一次长事务。它只部署运行时并执行只读 `inspect`，不会弹出管理员授权窗口；Agent 根据 JSON 的 `next_step` 继续。用户无需在终端输入命令，交互仅限于确认敏感操作、退出或登录 WeChat，以及批准 macOS 系统弹窗。

正式安装按以下可恢复阶段推进：

```text
install -> inspect -> prepare-wechat（仅需要时） -> initialize
        -> enable-service -> mcporter install + enable -> data_source_status
```

`state/activation.json` 只保存运行时、初始化和服务阶段标记，不记录账号路径、密钥或消息数据。任一阶段失败时只重试该阶段：服务启用失败不会撤销初始化，初始化失败也不会重新下载运行时。

密钥提取和数据库预解密是敏感步骤。只有 `inspect` 返回 `initialize`，且 Agent 已说明会出现 macOS 系统管理员授权弹窗并取得用户明确确认后，才执行已安装管理 CLI 的 `initialize`。安装器仅在内置 C 扫描器或用户另行明确确认的 WeChat 重签命令中请求管理员权限；管理 CLI、配置、缓存和 LaunchAgent 始终以当前登录用户运行。

`initialize` 会先以普通用户权限完成数据目录、版本门禁、微信进程和 ad-hoc 签名预检；预检失败不会弹出授权窗口。`prepare-wechat` 在用户明确确认后可能请求一次管理员授权，用于清理扩展属性并重签 WeChat.app；全部初始化预检通过后，`initialize` 最多再调用一次 `osascript`，该授权只覆盖扫描器本身。不要直接拼接额外的 `osascript` 命令，也不要为修改 `config.json`、`all_keys.json` 或文件所有权再次请求密码；旧流程遗留的单文件所有权问题会由管理 CLI 在普通用户上下文中原子修复。

安装入口的 stdout 始终只输出一条 JSON。`install_complete: true` 表示运行时已安装，后续任何初始化或服务错误都不应重新下载或重装。按 `next_step` 或 `error_code` 完成恢复动作后，只运行对应的已安装管理命令。

只读检查下一阶段：

```bash
"$HOME/Library/Application Support/WeChatDecryptLight/bin/wechat-decrypt-light" --json inspect
```

仅当 `inspect` 指向 `initialize` 且用户确认系统授权后，才使用已安装的管理 CLI 初始化；结构化错误恢复后也只重试这一步：

```bash
"$HOME/Library/Application Support/WeChatDecryptLight/bin/wechat-decrypt-light" --json initialize
```

不要在上述命令前添加 `sudo`。如果错误使用 `sudo`，管理 CLI 会拒绝运行并返回 `management_cli_must_not_run_as_root`。在系统弹窗中授权即可；扫描器会把 `all_keys.json` 直接写入独立 `data/` 目录，不需要执行额外扫描命令、移动密钥文件或修改目录所有者。

如果返回 `wechat_not_adhoc_signed`，必须保持重签名与密钥提取的两阶段边界：Agent 应先请用户退出 WeChat 并明确确认允许修改应用签名，然后直接执行已安装管理入口的 `prepare-wechat --confirm-resign`。该命令只允许处理版本门禁实际检测到、bundle id 正确的 WeChat.app，只授权固定的扩展属性清理和 `codesign` 操作，完成后复核签名并重新打开 WeChat；不要让用户在终端手工运行 `sudo codesign`，也不要向 `initialize` 传入 `--confirm-resign`。等待用户完成登录后，再重新执行普通 `initialize`；这样密钥提取不会与同一次 WeChat 退出/重启产生竞态。

较新的 macOS 还会要求发起安装的宿主应用取得“隐私与安全性 → App 管理”权限，这与管理员密码弹窗是两项独立授权。若最终的重签命令被系统以 `Operation not permitted` 拒绝，`prepare-wechat` 会返回 `app_management_permission_required`，自动打开对应的系统设置页面，并在 `details.responsible_app` 中给出应开启的应用名称（可能是 ChatGPT、终端或当前 IDE/Agent 宿主，不能写死为终端）。用户开启后只重试 `prepare-wechat --confirm-resign`；不要转而执行 `initialize`，也不要修改 TCC 数据库、关闭 SIP 或尝试自动操作该开关。

多账号场景下，`initialize` 会在同一次密钥扫描中校验所有检测到的数据目录，并把唯一匹配当前 WeChat 进程的账号写入单一 `data/config.json`；已有密钥也会先在所有候选账号上本地验证，可在不弹授权窗口的情况下纠正旧配置。只有自动匹配仍无法恢复时，才使用已安装管理入口的 `accounts` 和 `select-account --account <account_id>`，不要手工编辑 config 或移动密钥文件。

`task_for_pid` 失败会返回 `wechat_process_access_failed`。这说明管理员授权可能已经完成，但目标进程或签名状态不稳定；不要让用户重复批准弹窗，应回到 `inspect`，保持 WeChat 已登录并按返回的进程/签名动作恢复。

初始化成功后执行 `enable-service`。该阶段负责安装 LaunchAgent、核对 launchd PID 与监听端口，并等待 `query_ready`；失败时初始化状态仍被保留，只重试 `enable-service`：

```bash
"$HOME/Library/Application Support/WeChatDecryptLight/bin/wechat-decrypt-light" --json enable-service
```

所有失败 JSON 同时包含可直接展示的 `user_message`；需要用户动作时还会包含 `requires_user_action`，可重试的管理操作包含 `retry_command`。`error_code` 和 `next_action` 仍是 Agent 判断恢复分支的稳定机器字段。

如果 `error_code` 为 `version_not_allowed`，响应中的 `details.release_search` 会告诉 Agent 是否可以主动提议搜索公开来源。Agent 可以搜索官方页面、明确声明有分发权的发布仓库，以及 Gitee、GitHub 等稳定托管平台上的候选页面；但域名或仓库本身不代表官方授权、合法性或安全性。每个结果都必须先展示来源页面并取得用户确认，下载后再核对已公布的 SHA-256、`com.tencent.xinWeChat` Bundle ID 和受支持版本。搜索结果不能自动触发微信替换、重签名或修改 `version-guard.policy.json`。

只有 `enable-service` 返回 `query_ready: true`，才可通过 mcporter install + enable 把 `http://127.0.0.1:8765/mcp` 以 `streamablehttp` 注册到 Desktop。注册后调用不返回用户数据的 MCP 工具 `data_source_status`，且只有它返回 `status: "ready"` 才报告完成。不要用 `list_contacts`、`query_messages` 等用户数据工具验证安装。`waiting_for_wechat` 表示常驻机制正常，但 MCP 尚不可调用，不能提前注册或报告接入完成。

`data_source_status` 返回原生结构化对象（不是嵌套 JSON 字符串），只包含 `status`、数据库可访问性和消息分片数量等非敏感字段。

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

在源码调试模式下，`init` 成功后会自动安装指向当前工作树的常驻服务。正式安装应先使用上面的 `./install.sh --initialize`，初始化成功后再由已安装管理 CLI 的 `enable-service` 安装指向固定版本运行目录的 LaunchAgent。电脑登录后 launchd 自动加载服务；进程异常退出时会自动恢复。常驻服务不依赖终端窗口、shell 激活状态或宿主应用，也不需要 `sudo`。

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
- 如果本地领先远端、与远端分叉或 upstream 配置不安全，都会跳过自动更新并继续使用当前代码启动

单独检查是否有更新：

```bash
python main.py update --check
```

检查命令退出码：

- `0`：已是最新
- `3`：检测到可更新的远端提交
- `2`：工作区不干净、分支分叉、未配置 upstream 或其他不安全状态

## MCP 工具

默认使用 `core` 工具档，只向 Agent 暴露安装验收、联系人定位和消息查询所需的最小工具集：

| 工具 | 功能 |
|---|---|
| `data_source_status()` | 不读取用户数据的安装验收工具，检查联系人库和消息库是否可只读访问 |
| `list_contacts(query, limit)` | 列出或搜索联系人和群聊，返回可传给查询工具的 `id` |
| `query_messages(chat_id, start_time, end_time, keyword, limit, offset)` | 按明确时间范围查询指定联系人或群聊的消息 |
| `search_messages(keyword, chat_name, start_time, end_time, limit, offset)` | 跨聊天或指定聊天搜索关键词 |
| `get_contact_info(contact_id)` | 获取联系人或群聊的本地元数据 |
| `get_recent_sessions(limit)` | 查看最近会话摘要 |
| `get_new_messages()` | 查看自本 MCP 进程上次调用以来有变化的会话摘要 |

安装接入验收应调用 `data_source_status`，不要通过读取联系人或消息来测试连通性。该工具只返回 `ready/not_ready`、初始化状态、数据库可访问性和消息分片数量，不返回联系人、聊天 ID、消息、密钥或本机路径。

`query_messages` 要求传入明确的 `start_time`，大时间跨度建议分段查询。返回内容来自本机历史消息，不代表实时数据；需要实时状态时重新查询。

源码调试或需要处理图片、文件、转账、引用和位置详情时，可显式启用 `extended` 工具档。该档会额外暴露旧兼容别名、联系人标签和各类辅助解码工具：

```bash
WECHAT_DECRYPT_MCP_TOOL_PROFILE=extended .venv/bin/python3 main.py serve
```

也可在本机 `config.json` 中设置 `"mcp_tool_profile": "extended"`。修改后需重启 MCP 服务；未知值会按 `core` 处理，避免意外扩大数据暴露面。

## 配置

程序会自动检测微信数据目录并生成 `config.json`。这个文件只建议保存本机运行配置，不建议提交到 git。手动创建时可保持最小结构：

```json
{
  "db_dir": "/path/to/your/wxid/db_storage",
  "keys_file": "all_keys.json",
  "decrypted_dir": "decrypted",
  "decoded_image_dir": "decoded_images",
  "wechat_process": "WeChat",
  "mcp_tool_profile": "core"
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
