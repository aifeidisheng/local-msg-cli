# 微信指定版本管控与拒执行设计

## 背景

本项目依赖本机微信客户端的数据结构、数据库布局和进程内密钥形态。不同微信版本之间这些行为可能变化，继续在非指定版本上执行会带来账号风险、解密失败和数据结构不兼容风险。因此项目需要从“检测到微信即可执行”改为“命中指定版本区间才允许执行”。

## 目标

- 只允许指定版本区间内的微信客户端执行业务命令。
- 支持指定微信安装路径，避免误检测其他副本。
- 支持记录旧版安装包路径和 sha256，便于受控重装和运维验包。
- 版本不匹配、版本未知、路径不存在或检测失败时拒绝执行。
- `serve`、`init`、`decrypt`、`export`、`all`、`decode-images` 全部纳入门禁。
- MCP Server 运行期间在工具调用前做轻量复检，避免启动后微信被替换或升级。
- `doctor` 和 `status` 保留诊断能力，不执行解密和查询业务。

## 非目标

- 不修改微信客户端二进制。
- 不提供规避服务端风控、伪装版本或绕过更新机制的能力。
- 不依赖关闭自动升级作为唯一安全边界。
- 不对所有历史导出的明文文件做访问控制；本设计只控制工具执行路径。

## 总体策略

采用三层控制：

1. 受控安装：使用内部保存的旧版安装包，安装前校验 sha256，安装后校验客户端版本。
2. 配置版本区间：在 `config.json` 中声明允许的平台、版本区间和应用身份。
3. 运行时门禁：每次执行业务命令前读取真实安装信息，只有命中允许区间才继续。

关闭自动升级是运维要求，但不是最终判断依据。最终边界是运行时读取到的真实客户端版本。

## 配置契约

示例：

```json
{
  "wechat_app_path": "",
  "installer_path": "/opt/wechat-installers/WeChat-4.0.18.dmg",
  "installer_sha256": "expected_sha256_here",
  "version_guard": {
    "enabled": true,
    "block_on_unknown_version": true,
    "require_exact_app_path": true,
    "require_running_process_path": false,
    "require_update_disabled": false,
    "require_installer_hash": false,
    "allowed_version_ranges": [
      {
        "platform": "darwin",
        "bundle_id": "com.tencent.xinWeChat",
        "min_version": "4.0.18",
        "max_version": "4.0.18"
      }
    ]
  }
}
```

字段说明：

- `version_guard.enabled`: 是否启用版本门禁。生产环境必须为 `true`。
- `allowed_version_ranges`: 允许的版本区间列表。每条规则至少应包含 `min_version` / `max_version` 之一；单一版本可将两者配置成相同值。
- `bundle_id`: 可选应用身份约束，用于避免误识别成其他包。
- `wechat_app_path`: 本机实际安装路径。可留空让程序尝试从运行中的微信进程自动发现；如需固定某台机器的安装位置再填写。macOS 为 `.app` bundle，Windows 为 `Weixin.exe`。
- `installer_path`: 受控旧版安装包路径，仅用于运维诊断和安装包 hash 校验。
- `installer_sha256`: 受控旧版安装包 sha256。
- `build_version`: 当前仅作为 `doctor` 输出里的诊断信息保留，不作为主门禁条件。
- `require_exact_app_path`: 校验运行中微信进程路径是否匹配配置路径。
- `require_running_process_path`: 要求检测到运行中进程路径。默认关闭，避免 `serve` 前未启动微信时误阻断；严格部署可开启。
- `require_update_disabled`: 自动升级状态强校验。当前实现不可靠读取该状态，开启后会拒绝执行。
- `require_installer_hash`: 每次门禁时校验安装包 hash。安装包很大时会增加启动成本，建议只在 `doctor` 或安装流程中开启。

## 版本读取策略

macOS：

- 读取 `WeChat.app/Contents/Info.plist`。
- 校验 `CFBundleIdentifier`、`CFBundleShortVersionString`，并保留 `CFBundleVersion` 作为诊断信息。
- 若 `wechat_app_path` 留空，尝试读取运行中 `WeChat` 进程路径并定位 `.app` bundle。
- 若配置了 `wechat_app_path`，可选确认运行中进程与该路径属于同一个 `.app` bundle。

Windows：

- 通过 PowerShell 读取 `Weixin.exe` 的 `ProductVersion` 和 `FileVersion`。
- 后续可扩展进程路径校验。

Linux：

- 发行形式差异较大，当前支持通过配置提供 `wechat_version.short_version`。
- 后续可按具体发行包补充自动读取。

## 拒执行规则

启用 `version_guard.enabled=true` 后，以下任一情况拒绝执行：

- 未配置 `allowed_version_ranges`。
- 未配置 `wechat_app_path`，且未能从运行中的微信进程自动发现安装路径。
- 安装路径不存在。
- 版本读取失败。
- `bundle_id` 不匹配，或 `short_version` 不在任一允许区间内。
- 要求运行中进程路径校验，但进程路径缺失或不匹配。
- 要求自动升级状态校验，但当前平台无法可靠确认。
- 要求安装包 hash 校验，但安装包缺失或 hash 不一致。

## 命令行为

允许诊断：

- `python main.py help`
- `python main.py status`
- `python main.py doctor`

强制门禁：

- `python main.py serve`
- `python main.py init`
- `python main.py decrypt`
- `python main.py export`
- `python main.py all`
- `python main.py decode-images`

`doctor` 输出示例：

```text
[version] 微信版本门禁: PASS
  app_path      = /Applications/WeChat.app
  bundle_id     = com.tencent.xinWeChat
  version       = 4.0.18
  build         = 23110
  process_path  = /Applications/WeChat.app/Contents/MacOS/WeChat
```

失败示例：

```text
[version] 微信版本门禁: FAIL
  app_path      = /Applications/WeChat.app
  bundle_id     = com.tencent.xinWeChat
  version       = 4.0.20
  build         = 23897
  reasons       = 当前微信版本不在允许区间: 4.0.20
[!] 非指定微信版本，拒绝执行。请安装允许区间内的版本后重试。
```

## MCP Server 运行期控制

`main.py serve` 启动前执行一次门禁。MCP 工具函数调用前通过统一装饰器执行轻量复检，默认 30 秒 TTL 缓存检测结果。这样可以覆盖服务长时间运行期间微信被替换或升级的情况，同时避免每个工具调用都重复读取文件和进程信息。

## 受控安装流程

1. 将旧版微信安装包放入受控目录。
2. 计算安装包 sha256，并写入 `installer_sha256`。
3. 退出微信。
4. 安装指定版本。
5. macOS 如需读取进程内存，按现有流程重签名微信。
6. 启动微信并登录。
7. 执行 `python main.py doctor`。
8. 只有 `doctor` 通过后才允许 `init`、`decrypt`、`serve`、`export`。

## 自动升级策略

- 优先在微信客户端设置中关闭自动更新。
- 多人或受管设备建议用设备管理策略限制用户自行升级。
- 不建议删除 updater、修改微信二进制或依赖域名屏蔽作为主控制手段。
- 即使自动升级开关已关闭，工具仍必须每次执行前校验真实版本。

## 测试要求

- 版本落在允许区间内时通过。
- 版本号不匹配时拒绝。
- 版本读取失败时拒绝。
- `version_guard.enabled=false` 时兼容旧配置。
- `doctor` 能输出明确诊断。
- MCP 工具调用前会执行版本门禁。
- 主业务命令在门禁失败时不进入密钥提取、解密或查询逻辑。
