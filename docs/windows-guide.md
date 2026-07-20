# Windows 使用指南

本文说明如何在 Windows 10/11 上运行本机消息 MCP 数据源。轻量版保留 CLI 和 MCP Server，不包含原项目的 Web UI、tkinter GUI、企业微信和朋友圈工具。

## 1. 前置条件

- Windows 10 或 Windows 11，推荐 64 位系统。
- Python 3.10+，推荐从 [python.org](https://www.python.org/downloads/windows/) 安装 64 位版本，并勾选 Python Launcher。
- Windows 微信已经启动并登录。
- 首次提取数据库密钥时，PowerShell 必须“以管理员身份运行”。
- 当前 `version-guard.policy.json` 只允许 Windows 微信 `4.1.9`。

先在微信“设置 → 关于微信”确认版本，并在微信设置中关闭自动更新。微信后台更新后，版本门禁会在读取进程内存前重新检查实际 `Weixin.exe`；版本未知或不匹配时会退出，不会继续打开进程。

## 2. 安装依赖

右键 PowerShell，选择“以管理员身份运行”，然后进入项目目录：

```powershell
cd C:\path\to\wechat-decrypt-light
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

如果系统禁止执行激活脚本，可以只为当前 PowerShell 放开限制：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\.venv\Scripts\Activate.ps1
```

也可以不激活虚拟环境，后续始终使用：

```powershell
.\.venv\Scripts\python.exe main.py doctor
```

## 3. 启动微信并查找安装路径

启动并登录微信后，在管理员 PowerShell 中执行：

```powershell
(Get-Process Weixin | Select-Object -First 1).Path
```

常见结果类似：

```text
C:/Program Files/Tencent/Weixin/Weixin.exe
```

以命令实际返回的路径为准。上面使用正斜杠只是为了避免文档转义；写入 `config.json` 时请采用 PowerShell 返回的实际 Windows 路径。项目需要这个路径读取 `ProductVersion`，不能假设微信一定安装在 `C:\Program Files`。

## 4. 配置数据目录

先运行配置向导：

```powershell
python setup.py
```

程序会尝试读取 `%APPDATA%\Tencent\xwechat\config\*.ini` 并定位：

```text
<数据根目录>\xwechat_files\<wxid>\db_storage
```

如果检测到多个账号，选择当前登录账号最近使用的目录。自动检测失败时，可在微信“设置 → 文件管理”查看数据根目录，再进入对应账号的 `db_storage`。不要把 `xwechat_files` 根目录或账号目录本身填入 `db_dir`。

编辑项目目录下的 `config.json`，Windows 最小配置示例：

```json
{
  "db_dir": "D:\\xwechat_files\\wxid_example\\db_storage",
  "keys_file": "all_keys.json",
  "decrypted_dir": "decrypted",
  "decoded_image_dir": "decoded_images",
  "wechat_process": "Weixin.exe",
  "wechat_app_path": "C:\\Program Files\\Tencent\\Weixin\\Weixin.exe"
}
```

JSON 中的反斜杠必须写成 `\`。`wechat_app_path` 应与上一节 PowerShell 命令返回的路径一致。

## 5. 检查版本门禁

在执行任何密钥提取前运行：

```powershell
python main.py doctor
```

正常输出应包含：

```text
[version] 微信版本门禁: PASS
version       = 4.1.9
```

如果显示 `FAIL`，不要继续提取密钥：

- `未配置 wechat_app_path`：在 `config.json` 填写实际 `Weixin.exe` 路径。
- `微信安装路径不存在`：路径错误，或微信安装目录已经变化。
- `当前微信版本不在允许区间`：微信可能后台更新了；安装已经验证的版本后重试。
- 无法读取版本：确认 PowerShell 能访问该文件，并检查路径是否指向真实 `Weixin.exe`。

普通命令会检查配置的安装文件；真正获取密钥时还会根据目标 PID 获取实际运行进程路径，再做一次无缓存检查。因此配置指向旧版本、实际运行新版本时仍会被拒绝。

## 6. 首次提取密钥并预解密

保持微信已登录，在管理员 PowerShell 中执行：

```powershell
python main.py init
```

`init` 会依次：

1. 检查微信版本门禁。
2. 确认 `Weixin.exe` 正在运行。
3. 缺少有效 `all_keys.json` 时，扫描微信进程内存并匹配当前 `db_dir` 下数据库的 salt。
4. 预解密 MCP 查询需要的数据库缓存。

如果希望把全部数据库解密到 `decrypted` 目录，可执行：

```powershell
python main.py decrypt
```

不要共享或提交 `all_keys.json`。它包含明文数据库密钥，拿到该文件即可解密对应账号的本地数据。

## 7. 启动 MCP Server

```powershell
python main.py serve --port 8765
```

Desktop MCP 配置：

| 配置项 | 值 |
|---|---|
| 类型 | `streamablehttp` |
| 地址 | `http://127.0.0.1:8765/mcp` |
| Runtime | Desktop |

服务默认只监听本机 `127.0.0.1`。Cloud Runtime 无法连接这台 Windows 电脑的 `localhost`。

## 8. 常用命令

| 用途 | 命令 | 是否需要管理员权限 |
|---|---|---|
| 环境检查 | `python setup.py --check` | 否 |
| 查看状态 | `python main.py status` | 否 |
| 检查版本门禁 | `python main.py doctor` | 通常不需要 |
| 首次提取密钥并初始化缓存 | `python main.py init` | 是 |
| 解密全部数据库 | `python main.py decrypt` | 缺少有效 key 时需要 |
| 启动 MCP Server | `python main.py serve --port 8765` | 否 |
| 批量导出聊天 | `python export_all_chats.py` | 否 |
| 批量解密图片 | `python main.py decode-images` | 否 |

## 9. 常见问题

### 找不到 `Weixin.exe` 进程

确认启动的是 Windows 微信 4.x、已经登录，并在任务管理器“详细信息”中能看到 `Weixin.exe`。之后重新运行：

```powershell
Get-Process Weixin
```

### 无法打开进程或未提取到密钥

1. 关闭当前终端。
2. 右键 PowerShell，选择“以管理员身份运行”。
3. 确认 `db_dir` 属于当前登录的微信账号。
4. 在微信中打开几个最近会话，使相关数据库被加载。
5. 重新执行 `python main.py init`。

### 找到了数据库，但 key 校验失败

通常是 `db_dir` 与当前登录账号不匹配，或者微信版本不属于已验证范围。不要把其他账号的 `all_keys.json` 与当前数据目录混用；程序会记录 key 文件对应的 `_db_dir` 并在目录变化时重新提取。

### PowerShell 找不到 `python` 或 `py`

重新安装 Python 并启用 Python Launcher，或者直接使用 Python 的绝对路径。安装完成后新开 PowerShell，再确认：

```powershell
py -3 --version
```

### MCP Server 已启动，但 Desktop 无法连接

- 确认地址是 `http://127.0.0.1:8765/mcp`，不是旧的 `/sse`。
- Runtime 必须选择 Desktop。
- 确认启动服务的 PowerShell 窗口仍在运行。
- 如果端口被占用，服务端和 Desktop 配置同时改用另一个端口。
