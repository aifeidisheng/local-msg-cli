# 微信 macOS 4.1.12 本地数据库解密可行性调研

调研日期：2026-08-11

## 结论

**macOS 微信 4.1.12 的本地数据库可以解密，Apple Silicon 上也已有公开代码和一次实机验证记录；但本仓库当前的 `x'<key><salt>'` 内存扫描器大概率不能直接提取 4.1.12 的密钥。** 可行的改造方向是：保留本仓库现有 SQLCipher 解密器和 page-1 HMAC 验证，新增一个在微信实际使用 32 字节 raw key 时截取它的提取器。

最直接的 4.1.12 证据是 [`fclwtt/wechat-cli` PR #10](https://github.com/fclwtt/wechat-cli/pull/10)：提交者报告在 Apple Silicon、macOS 14.5、微信 4.1.12 上完成了真实 TXT 导出，并在解密后的聊天数据库中验证了消息读写闭环。其实现使用 4.1.12 arm64 指令特征定位 `wechat.dylib` 中的 key 调用，再用 Frida 截取 32 字节候选 key，并只保存通过数据库第一页 HMAC 验证的候选。[PR 说明](https://github.com/fclwtt/wechat-cli/pull/10)；[实现源码](https://github.com/bobtian/wechat-cli/blob/d3dd2e54045dcddb5340db754ea39ef1dffadaa7/wechat_cli/keys/frida_macos.py#L30-L47)。

这仍不是本项目可以立即放行 4.1.12 的充分证据：PR #10 截至调研时仍是未合并的 Draft，验证结果由提交者自述，没有上游维护者 review；PR 和代码只写了 `4.1.12`，没有记录该实测应用的 `CFBundleVersion` 或四段完整版本。腾讯 [macOS 微信官网](https://mac.weixin.qq.com/?lang=zh_CN) 当前把对外下载版本标为 `4.1.12`；腾讯官方 [Sparkle 更新源](https://dldir1.qq.com/weixin/mac/mac-release.xml) 则把当前安装包精确标为 `4.1.12.29`、build `269341`，发布时间为 2026-07-28，大小为 509,508,392 字节。由于 PR 没有记录 build 或二进制摘要，不能证明其实测的就是当前官方 `4.1.12.29 (269341)`，更不能把所有同名内部构建视为已验证。

## 一手证据

### 1. 当前官方版本元数据

腾讯官网当前展示 macOS 微信 `4.1.12`，并直接链接 `dldir1v6.qq.com` 上的安装包。[腾讯 macOS 微信官网](https://mac.weixin.qq.com/?lang=zh_CN)。与该页面对应的腾讯 Sparkle 更新源给出了更精确的制品标识：`sparkle:shortVersionString=4.1.12.29`、`sparkle:version=269341`、发布时间 2026-07-28、最低系统版本 macOS 12.0、安装包长度 509,508,392 字节，并提供 EdDSA 更新签名。[腾讯官方更新源](https://dldir1.qq.com/weixin/mac/mac-release.xml)。

本调研没有下载这份约 509 MB 的 DMG，因此没有独立读取其中 `WeChat.app` 的 `CFBundleIdentifier`、`CFBundleShortVersionString`、`CFBundleVersion` 和 `wechat.dylib` 摘要。官方 feed 可以确定当前发布制品的版本/build，但不能补足 PR #10 缺失的测试制品身份。

### 2. PR #10：4.1.12 专用 Frida 捕获路径

PR 的实现先解析已安装的 `wechat.dylib` Mach-O，要求存在 arm64 slice，然后搜索一段源码明确标注为 “WeChat 4.1.12 arm64” 的调用前指令特征。它解码紧随其后的 arm64 `BL`，把文件偏移映射为模块相对虚拟地址；这避免依赖 ASLR 后的绝对地址，但仍依赖该构建保留这段调用特征。[Mach-O 解析与定位源码](https://github.com/bobtian/wechat-cli/blob/d3dd2e54045dcddb5340db754ea39ef1dffadaa7/wechat_cli/keys/frida_macos.py#L67-L172)。

Frida agent 等待 `/Applications/WeChat.app/Contents/Resources/wechat.dylib` 加载，然后 hook `module.base + offset` 指向的函数；仅当第三个参数表示长度为 32、第二个参数非空时，才复制第二个参数所指的 32 字节候选值。[Frida agent 源码](https://github.com/bobtian/wechat-cli/blob/d3dd2e54045dcddb5340db754ea39ef1dffadaa7/wechat_cli/keys/frida_macos.py#L287-L311)。捕获端会去重候选，并逐个对本地加密数据库 page 1 做 HMAC 验证；未验证候选不会保存。[验证和保存源码](https://github.com/bobtian/wechat-cli/blob/d3dd2e54045dcddb5340db754ea39ef1dffadaa7/wechat_cli/keys/frida_macos.py#L255-L285)。

该定位器是 fail-closed：找不到签名或出现多个候选目标时都会停止，不猜测地址。[定位失败分支](https://github.com/bobtian/wechat-cli/blob/d3dd2e54045dcddb5340db754ea39ef1dffadaa7/wechat_cli/keys/frida_macos.py#L158-L172)。这意味着其代码注释和 PR 测试只支持已识别的 `4.1.12 arm64` 二进制，不能因为调用方把它称为 `4.1.12+` 路径就推断后续版本也兼容。[调用方源码](https://github.com/bobtian/wechat-cli/blob/d3dd2e54045dcddb5340db754ea39ef1dffadaa7/wechat_cli/commands/init.py#L170-L214)。

进程附加需要微信带有 `com.apple.security.get-task-allow` entitlement。PR 的准备命令读取并保留原 entitlements，加入调试权限后 ad-hoc 重签名，并在完成后复核权限；该实现没有要求关闭 SIP。[准备命令源码](https://github.com/bobtian/wechat-cli/blob/d3dd2e54045dcddb5340db754ea39ef1dffadaa7/wechat_cli/commands/prepare_macos.py#L19-L67)。不过，重签名和允许调试器附加仍会削弱微信原始代码签名提供的保护，不能当作无风险操作。

### 3. `rmqg/wechat-mac-export`：通用 CommonCrypto 捕获原型

[`rmqg/wechat-mac-export`](https://github.com/rmqg/wechat-mac-export) 明确写的是在 **Apple Silicon、微信 4.1.11** 上构建和验证，不是 4.1.12 实测。[README](https://github.com/rmqg/wechat-mac-export/blob/64ff81882a86dbe686af9609faf186dfbffb3c5f/README.md#L1-L14)。它提供的重要证据是另一个可复现的提取机制：用 LLDB/debugserver 附加微信，在 `CCCryptorCreate` 上设置断点；按照 arm64 ABI，从 `x3` 读取 key 指针、从 `x4` 读取长度，只记录长度为 32 的值，并自动继续运行。[捕获源码](https://github.com/rmqg/wechat-mac-export/blob/64ff81882a86dbe686af9609faf186dfbffb3c5f/wxexport/lldb_capture.py#L28-L97)。随后它把每个候选 key 与每个数据库的第一页交叉验证，以 HMAC 成功结果完成 key 到数据库的归属。[匹配源码](https://github.com/rmqg/wechat-mac-export/blob/64ff81882a86dbe686af9609faf186dfbffb3c5f/wxexport/match.py#L1-L40)。

该项目把 CommonCrypto ABI 路径描述为不依赖微信内部结构或固定偏移，因此理论上可跨 4.1.x；但这只是设计层面的可移植性，仓库没有 4.1.12 实测记录。[技术说明](https://github.com/rmqg/wechat-mac-export/blob/64ff81882a86dbe686af9609faf186dfbffb3c5f/docs/how-it-works.md#L22-L48)。它还要求关闭 SIP、使用管理员权限运行 LLDB、重签名微信，并要求用户在捕获期间打开和滚动相关会话，使每个数据库真正触发解密。[使用要求和限制](https://github.com/rmqg/wechat-mac-export/blob/64ff81882a86dbe686af9609faf186dfbffb3c5f/README.md#L42-L89)。这些要求不适合原样移植到本仓库的安装流程。

## 数据库加密参数

`rmqg/wechat-mac-export` 在 4.1.11 上确认的参数为：AES-256-CBC、4096 字节 page、每库独立 32 字节 raw main key、文件头 16 字节 salt、80 字节 reserve（IV 16 + HMAC 64），以及 HMAC-SHA512；HMAC key 是 `PBKDF2-HMAC-SHA512(raw_key, salt XOR 0x3a, 2, dklen=32)`，页号以 little-endian 32 位整数加入 HMAC。[参数说明](https://github.com/rmqg/wechat-mac-export/blob/64ff81882a86dbe686af9609faf186dfbffb3c5f/docs/how-it-works.md#L50-L72)；[加密实现](https://github.com/rmqg/wechat-mac-export/blob/64ff81882a86dbe686af9609faf186dfbffb3c5f/wxexport/crypto.py#L1-L41)。

PR #10 的 page-1 verifier 独立使用相同的 4096/80/HMAC-SHA512 参数，并据此验证其 4.1.12 实机捕获结果。[PR 验证源码](https://github.com/bobtian/wechat-cli/blob/d3dd2e54045dcddb5340db754ea39ef1dffadaa7/wechat_cli/keys/common.py#L14-L28)。本仓库的 [`decrypt_db.py`](../decrypt_db.py) 和 [`find_all_keys_macos.c`](../find_all_keys_macos.c) 也实现相同参数，因此现有解密与 HMAC 验证逻辑很可能可以保留；公开证据指向的主要不兼容点是 **key 的定位/捕获方式**，不是数据库页加密算法。

## 与本仓库旧扫描器的差异

本仓库当前 scanner 通过 `mach_vm_region`/`mach_vm_read` 遍历可读写内存，寻找恰好 99 字节的 ASCII 形式 `x'<64 hex key><32 hex salt>'`，再用数据库 page-1 HMAC 认证候选。[当前扫描实现](../find_all_keys_macos.c)。`rmqg` 的 4.1.11 实测说明 4.1.x 已直接传递 32 字节 raw key，旧 ASCII pragma 不再存在于可扫描区域；同时其测试中 Mach VM API 也读不到包含 key 的区域。[问题分析](https://github.com/rmqg/wechat-mac-export/blob/64ff81882a86dbe686af9609faf186dfbffb3c5f/docs/how-it-works.md#L5-L20)。PR #10 的说明也明确称旧内存签名不匹配微信 4.1.12。[PR Why](https://github.com/fclwtt/wechat-cli/pull/10)。

因此，只增加 `64 hex` 或扩大 Mach VM 扫描范围没有一手证据支持，且不能解决区域不可读问题。合理改造是把当前流程拆成两个层次：

1. 新增 Apple Silicon 4.1.12 raw-key 捕获后端，优先采用“版本签名定位内部调用 + 只复制 32 字节参数”的窄 hook，并对未知/多重签名 fail-closed。参考 [PR #10 定位和 Frida hook](https://github.com/bobtian/wechat-cli/blob/d3dd2e54045dcddb5340db754ea39ef1dffadaa7/wechat_cli/keys/frida_macos.py#L140-L172)。
2. 复用本仓库现有的数据库枚举、page-1 HMAC 验证、key-to-DB 映射和解密代码；任何未通过 HMAC 的候选不得输出或落盘。[本仓库 HMAC 实现](../find_all_keys_macos.c)；[PR 的同类约束](https://github.com/bobtian/wechat-cli/blob/d3dd2e54045dcddb5340db754ea39ef1dffadaa7/wechat_cli/keys/frida_macos.py#L255-L285)。

全局断在 `CCCryptorCreate` 虽然较不依赖版本，但会截获进程中所有长度为 32 的加密候选。`rmqg` 在验证前就把不同候选写入明文 `rawkeys.txt`，可能包含与数据库无关的进程密钥；其 README 也警告 key、解密数据库和导出明文需要妥善删除或保护。[LLDB 捕获源码](https://github.com/rmqg/wechat-mac-export/blob/64ff81882a86dbe686af9609faf186dfbffb3c5f/wxexport/lldb_capture.py#L28-L51)；[安全说明](https://github.com/rmqg/wechat-mac-export/blob/64ff81882a86dbe686af9609faf186dfbffb3c5f/README.md#L134-L142)。本项目不应照搬“先保存所有候选、后验证”的做法。

## 支持决策与验收门槛

建议把首个实现目标精确写为“Apple Silicon + 腾讯当前 `4.1.12.29 (269341)` 制品”，而不是笼统的 `4.1.12+`；该完整版本/build 来自 [腾讯官方更新源](https://dldir1.qq.com/weixin/mac/mac-release.xml)。开发前还应从测试制品读取 Bundle ID、完整版本、代码签名和 `wechat.dylib` SHA-256，再将对应指令签名作为显式兼容数据。PR 没有记录其实测 build，因此必须在 `4.1.12.29 (269341)` 上重新验证；每个新构建也必须重新定位和实测，不能依赖三段短版本猜测。[PR 的单签名和 fail-closed 逻辑](https://github.com/bobtian/wechat-cli/blob/d3dd2e54045dcddb5340db754ea39ef1dffadaa7/wechat_cli/keys/frida_macos.py#L30-L47)。

最低验收应包括：

- 在固定完整构建上找到唯一 hook 目标，未知或多目标时拒绝继续；该行为与 [PR 定位器](https://github.com/bobtian/wechat-cli/blob/d3dd2e54045dcddb5340db754ea39ef1dffadaa7/wechat_cli/keys/frida_macos.py#L158-L172) 一致。
- 捕获 contact、session、message shard 和 message resource 等实际存在数据库的 key，且每个 key 都通过对应 page-1 HMAC；PR 的初始化流程以 `require_all=True` 等待所有发现的数据库。[PR 初始化调用](https://github.com/bobtian/wechat-cli/blob/d3dd2e54045dcddb5340db754ea39ef1dffadaa7/wechat_cli/commands/init.py#L191-L210)。
- 解密后用 SQLite 做结构校验，并验证 WAL 处理和最新消息可见性。PR 的 crypto 路径包含加密 WAL frame 应用，而 `rmqg` 的整库解密器只处理 `.db` 主文件；后者可能遗漏尚未 checkpoint 的最新数据。[PR crypto](https://github.com/bobtian/wechat-cli/blob/d3dd2e54045dcddb5340db754ea39ef1dffadaa7/wechat_cli/core/crypto.py#L17-L77)；[`rmqg` decrypt](https://github.com/rmqg/wechat-mac-export/blob/64ff81882a86dbe686af9609faf186dfbffb3c5f/wxexport/decrypt.py#L10-L35)。
- 最后执行本仓库的只读查询和 `data_source_status` 验收；在这些结果通过并发布受信任策略前，继续让版本门禁拒绝 4.1.12。

## 当前不能宣称的内容

- 不能宣称当前官方 `4.1.12.29 (269341)` 已被该 PR 验证：官方 build 可以从 [腾讯更新源](https://dldir1.qq.com/weixin/mac/mac-release.xml) 确定，但公开 PR 实测没有记录 `CFBundleVersion`，且提取器只有一个二进制指令签名。[PR 源码](https://github.com/bobtian/wechat-cli/blob/d3dd2e54045dcddb5340db754ea39ef1dffadaa7/wechat_cli/keys/frida_macos.py#L30-L47)。
- 不能宣称 Intel Mac 可用：PR #10 明确要求 arm64 Mach-O，`rmqg` 也只实现 arm64 参数寄存器读取。[PR 架构检查](https://github.com/bobtian/wechat-cli/blob/d3dd2e54045dcddb5340db754ea39ef1dffadaa7/wechat_cli/keys/frida_macos.py#L67-L106)；[`rmqg` 限制](https://github.com/rmqg/wechat-mac-export/blob/64ff81882a86dbe686af9609faf186dfbffb3c5f/README.md#L122-L132)。
- 不能把 PR #10 当作已审计的上游支持：它仍是 Draft、未合并，验证是作者自报。[PR 状态与说明](https://github.com/fclwtt/wechat-cli/pull/10)。
- 不能通过修改 `version-guard.policy.json` 做实验性放行；本仓库版本门禁要求未知版本 fail-closed，并要求策略完整性保护。[版本门禁设计](wechat-version-guard-design.md)。

综合判断：**改本项目代码支持 4.1.12 是可行的，预计解密器改动很小，主要工作是替换/扩展 macOS key extractor、约束重签名和进程附加权限、增加 WAL 与端到端验证。当前证据足够立项做受控原型，但还不够直接发布“支持 4.1.12”。**
