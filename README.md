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
- WeChat 4.x 正在运行
- macOS 需 Xcode Command Line Tools: `xcode-select --install`
- 读取进程内存需要管理员/root 权限

Desktop 内置 Python 已验证：`/path/to/desktop/python3` 为 Python 3.13.12，带 pip 26.0.1，满足本项目版本要求。该环境未预装 `pycryptodome`、`zstandard`、`fastmcp`，仍需安装 `requirements.txt`。

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

## macOS 快速开始

```bash
# 1. 退出微信并重签名
killall WeChat
sudo codesign --force --deep --sign - /Applications/WeChat.app

# 2. 重新启动微信并登录，然后编译和提取 DB key
cc -O2 -o find_all_keys_macos find_all_keys_macos.c -framework Foundation
sudo ./find_all_keys_macos

# 3. 首次使用前预解密 MCP 查询缓存
python3 main.py init

# 4. 启动 MCP Server
python3 main.py serve --port 8765
```

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
| 首次预解密 MCP 缓存 | `python main.py init` |
| 仅预解密指定数据库 | `python main.py init --target-db MSG` |
| 启动 MCP Server | `python main.py serve --port 8765` |
| 解密全部数据库到目录 | `python main.py decrypt` |
| 批量导出聊天记录 | `python export_all_chats.py` |
| 批量解密图片 | `python main.py decode-images` |

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

程序会自动检测微信数据目录并生成 `config.json`。如果自动检测失败，手动创建：

```json
{
  "db_dir": "/path/to/your/wxid/db_storage",
  "keys_file": "all_keys.json",
  "decrypted_dir": "decrypted",
  "decoded_image_dir": "decoded_images",
  "wechat_process": "WeChat"
}
```

各平台默认路径：

- macOS: `~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/<wxid>/db_storage`
- Windows: 微信设置 -> 文件管理中查看
- Linux: `~/Documents/xwechat_files/<wxid>/db_storage`

## 安全提示

- `all_keys.json` 包含明文 raw key，勿提交到 git 或与人共享。
- 解密后的 `.db` 文件是明文 SQLite，包含联系人、群和消息内容。
- 本工具仅用于分析自己的本机数据。请遵守相关法律法规和软件服务协议。

## License

MIT
