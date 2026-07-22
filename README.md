# Local Message MCP Data Source

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
- macOS/Linux 读取进程内存需要 root 权限（Linux 也可使用 `CAP_SYS_PTRACE`）

若使用 Desktop 内置 Python（≥3.10 即可），仍需通过 `pip install -r requirements.txt` 安装依赖。

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

也可以使用 Desktop 内置 Python：

```bash
/path/to/desktop/python3 -m pip install -r requirements.txt
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

## macOS 快速开始

```bash
# 1. 退出微信并重签名
killall WeChat
sudo codesign --force --deep --sign - /Applications/WeChat.app

# 2. 重新启动微信并登录，然后编译和提取 DB key
cc -O2 -o find_all_keys_macos find_all_keys_macos.c -framework Foundation
sudo ./find_all_keys_macos

# 3. 首次使用前预解密 MCP 查询缓存
#    macOS 上 init 成功后会自动安装登录自启服务
.venv/bin/python3 main.py init
```

在 macOS 上，`init` 成功后会自动安装常驻服务。之后 macOS 会在当前用户登录时启动该服务；如果微信尚未启动，服务会先等待微信和版本门禁就绪，满足条件后再启动 MCP Server。MCP 进程异常退出时，服务会自动恢复。服务由项目自己的 `.venv/bin/python3` 运行，不依赖终端窗口、shell 激活状态或 AlphaClaw。常驻服务使用用户级 `launchd`，不需要 `sudo`。

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
