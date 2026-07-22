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
2. 配置版本区间：在 `version-guard.policy.json` 中声明允许的平台、版本区间和应用身份。
3. 策略完整性：默认策略使用 MCP 代码中的固定 SHA-256 摘要；外部策略必须由宿主环境提供摘要，策略被改写或替换时 fail-closed。
4. 运行时门禁：每次执行业务命令前读取真实安装信息，只有命中允许区间才继续。

关闭自动升级是运维要求，但不是最终判断依据。最终边界是运行时读取到的真实客户端版本。

## 配置契约

示例：

```json
{
  "version_guard": {
    "enabled": true,
    "block_on_unknown_version": true,
    "require_update_disabled": false,
    "allowed_version_ranges": [
      {
        "platform": "darwin",
        "min_version": "4.0.18",
        "max_version": "4.0.18"
      }
    ]
  }
}
```

本机 `config.json` 继续只保存运行态配置，例如：

```json
{
  "db_dir": "/path/to/xwechat_files/<wxid>/db_storage",
  "keys_file": "all_keys.json",
  "decrypted_dir": "decrypted",
  "decoded_image_dir": "decoded_images",
  "wechat_process": "WeChat",
  "wechat_app_path": "",
  "installer_path": "/opt/wechat-installers/WeChat-4.0.18.dmg",
  "installer_sha256": "expected_sha256_here"
}
```

字段说明：

- `version_guard.enabled`: 是否启用版本门禁。生产环境必须为 `true`。
- `allowed_version_ranges`: 允许的版本区间列表。每条规则至少应包含 `min_version` / `max_version` 之一；单一版本可将两者配置成相同值。
- `wechat_app_path`: 本机实际安装路径，保存在本地 `config.json`。可留空让程序尝试从运行中的微信进程自动发现；如需固定某台机器的安装位置再填写。macOS 为 `.app` bundle，Windows 为 `Weixin.exe`。
- `installer_path`: 受控旧版安装包路径，可选保存在本地 `config.json` 供运维记录使用，不参与门禁判定。
- `installer_sha256`: 受控旧版安装包 sha256，可选保存在本地 `config.json` 供运维记录使用，不参与门禁判定。
- `build_version`: 当前仅作为 `doctor` 输出里的诊断信息保留，不作为主门禁条件。
- `require_update_disabled`: 自动升级状态强校验，默认应为 `false`。macOS 微信 3.x 可通过旧 Sparkle plist 辅助判断；微信 4.x 的界面开关已迁移到微信自己的设置系统，旧 plist 无法反映真实状态，因此启用此项会 fail-closed。其他平台暂未实现。该字段不能替代实际版本门禁。

## 版本读取策略

macOS：

- 读取 `WeChat.app/Contents/Info.plist`。
- 校验 `CFBundleIdentifier`、`CFBundleShortVersionString`，并保留 `CFBundleVersion` 作为诊断信息。
- 若 `wechat_app_path` 留空，尝试读取运行中 `WeChat` 进程路径并定位 `.app` bundle。
- 若配置了 `wechat_app_path`，当前主要用于辅助定位本机微信安装位置，不再作为强制拒执行条件。

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
- `short_version` 不在任一允许区间内。
- 要求自动升级状态校验，但当前平台暂未实现。
- 在微信 4.x 上要求自动升级状态强校验，但真实界面开关无法可靠读取。

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
```

微信 3.x 启用 `require_update_disabled` 后，`doctor` 还会输出 `prefs_source=legacy_sparkle_plist`、`prefs_path`、`last_check` 和 `skipped_ver` 等诊断字段。微信 4.x 不读取这些旧字段作为界面开关，而是输出 `update_check=unsupported (WeChat 4.x)` 和手动关闭提示。

失败示例：

```text
[version] 微信版本门禁: FAIL
  app_path      = /Applications/WeChat.app
  bundle_id     = com.tencent.xinWeChat
  version       = 4.0.20
  build         = 23897
  reasons       = 当前微信版本不在允许区间: 4.0.20
[!] 微信安全门禁拒绝执行，请根据 reasons 处理后重试。
```

## MCP Server 运行期控制

`main.py serve` 启动前执行一次门禁。MCP 工具函数调用前通过统一装饰器执行轻量复检，默认 30 秒 TTL 缓存微信版本检测结果；策略文件完整性不使用该 TTL，每次工具调用都会重新校验。这样可以覆盖服务长时间运行期间微信被替换、升级或门禁策略被改写的情况，同时避免每个工具调用都重复读取微信安装信息和进程信息。

默认策略文件必须位于受信任的默认路径，并与代码内置的规范化 JSON 摘要一致。生产流程不接受自定义策略路径或环境变量注入摘要；需要支持新版本时，必须发布同步更新策略和内置摘要的受信任版本。规范化摘要避免 Windows 的 LF/CRLF 换行转换产生误报。

## 受控安装流程

1. 将旧版微信安装包放入受控目录。
2. 如需留档，可计算安装包 sha256 并写入本机 `config.json`。
3. 退出微信。
4. 安装指定版本。
5. macOS 如需读取进程内存，按现有流程重签名微信。
6. 启动微信并登录。
7. 执行 `python main.py doctor`。
8. 只有在用户确认版本和策略状态后，才执行 `init`、`decrypt`、`serve`、`export`。

## 自动升级策略

- 用户应在微信“设置 > 通用”中手动关闭“有更新时自动升级微信”；工具不修改微信设置、updater、客户端二进制或网络规则。
- 微信 4.x 无法通过旧 Sparkle plist 准确读取该界面开关，因此默认不启用自动升级状态强校验，也不根据 `MacUpdate` 插件存在与否推断开关状态。
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
- 门禁策略被修改、替换、删除、使用自定义路径或缺少内置可信摘要时拒绝执行。
- 主业务命令在门禁失败时不进入密钥提取、解密或查询逻辑。
