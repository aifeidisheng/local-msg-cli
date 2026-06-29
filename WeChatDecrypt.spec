# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec — 打包 wechat_decrypt_launcher.py 为本机 MCP 数据源 exe。
#
# 启动后:
#   1. 无参数/ serve: 起 streamable-http MCP Server
#   2. 带 CLI 参数: 分发到 main.py / export_all_chats.py / 其他工具脚本

# 所有子进程 (工具按钮触发) 需要的脚本, 打进 exe 同目录
datas = [
    ('main.py', '.'),
    ('config.py', '.'),
    ('config.example.json', '.'),
    ('decrypt_db.py', '.'),
    ('decode_image.py', '.'),
    ('export_messages.py', '.'),
    ('export_all_chats.py', '.'),
    ('export_chat.py', '.'),
    ('chat_export_helpers.py', '.'),
    ('find_all_keys.py', '.'),
    ('find_all_keys_windows.py', '.'),
    ('find_all_keys_linux.py', '.'),
    ('find_image_key.py', '.'),
    ('find_image_key_monitor.py', '.'),
    ('key_scan_common.py', '.'),
    ('key_utils.py', '.'),
    ('batch_decrypt_images.py', '.'),
    ('mcp_server.py', '.'),
]

binaries = []
hiddenimports = [
    # 显式列, 避免 PyInstaller 漏 detect 导致打包后 import 报错
    'argparse', 'csv', 'glob', 'hashlib', 'hmac', 'http.server', 'json',
    'platform', 'queue', 'socketserver', 'sqlite3', '_sqlite3',
    'subprocess', 'tempfile', 'threading', 'urllib.parse', 'uuid', 'wave',
    'xml.etree.ElementTree',
    'fastmcp', 'uvicorn',
    'mcp', 'mcp.server', 'mcp.server.fastmcp',
    'Crypto', 'Crypto.Cipher', 'Crypto.Cipher.AES',
    'Crypto.Util', 'Crypto.Util.Padding',
    'zstandard',
]


a = Analysis(
    ['wechat_decrypt_launcher.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='WeChatDecrypt',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,                # 保留 console 显示后端日志 + 错误
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
