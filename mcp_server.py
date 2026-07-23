r"""
Local message MCP server.

Exposes local chat contacts and message history through FastMCP streamable-http.
"""

import functools
import io
import os, sys, json, time, sqlite3, tempfile, struct, hashlib, atexit, re, threading, subprocess
import glob
import wave
import hmac as hmac_mod
from contextlib import closing
from datetime import datetime, timedelta
from typing import Optional, List, Union
import xml.etree.ElementTree as ET
from Crypto.Cipher import AES
try:
    from fastmcp import FastMCP
except ImportError:  # pragma: no cover - compatibility for older local test envs
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:  # pragma: no cover - unit-test fallback when MCP deps are absent
        class FastMCP:  # type: ignore[no-redef]
            def __init__(self, *args, **kwargs):
                self._tools = {}

            def tool(self, *decorator_args, **decorator_kwargs):
                def decorator(fn):
                    self._tools[fn.__name__] = fn
                    return fn
                return decorator

            def http_app(self):
                raise RuntimeError("fastmcp is required to run the MCP server")

            def streamable_http_app(self):
                raise RuntimeError("fastmcp is required to run the MCP server")
import zstandard as zstd
from config import _config_file_path, _DEFAULT
from decode_image import ImageResolver
from key_utils import get_key_info, key_path_variants, strip_key_metadata

# ============ 加密常量 ============
PAGE_SZ = 4096
KEY_SZ = 32
SALT_SZ = 16
RESERVE_SZ = 80
SQLITE_HDR = b'SQLite format 3\x00'
WAL_HEADER_SZ = 32
WAL_FRAME_HEADER_SZ = 24

# ============ 配置加载 ============
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = _config_file_path()

try:
    with open(CONFIG_FILE, encoding="utf-8") as f:
        _cfg = json.load(f)
except FileNotFoundError:
    _cfg = dict(_DEFAULT)
for _key in ("keys_file", "decrypted_dir", "decoded_image_dir", "mcp_cache_dir"):
    if _key in _cfg and not os.path.isabs(_cfg[_key]):
        _cfg[_key] = os.path.join(os.path.dirname(CONFIG_FILE), _cfg[_key])

DB_DIR = _cfg["db_dir"]
KEYS_FILE = _cfg["keys_file"]
DECRYPTED_DIR = _cfg["decrypted_dir"]

# 图片相关路径
_db_dir = _cfg["db_dir"]
if os.path.basename(_db_dir) == "db_storage":
    WECHAT_BASE_DIR = os.path.dirname(_db_dir)
else:
    WECHAT_BASE_DIR = _db_dir

DECODED_IMAGE_DIR = _cfg.get("decoded_image_dir")
if not DECODED_IMAGE_DIR:
    DECODED_IMAGE_DIR = os.path.join(SCRIPT_DIR, "decoded_images")
elif not os.path.isabs(DECODED_IMAGE_DIR):
    DECODED_IMAGE_DIR = os.path.join(os.path.dirname(CONFIG_FILE), DECODED_IMAGE_DIR)

try:
    with open(KEYS_FILE, encoding="utf-8") as f:
        ALL_KEYS = strip_key_metadata(json.load(f))
except FileNotFoundError:
    ALL_KEYS = {}

# ============ 解密函数 ============

def decrypt_page(enc_key, page_data, pgno):
    iv = page_data[PAGE_SZ - RESERVE_SZ : PAGE_SZ - RESERVE_SZ + 16]
    if pgno == 1:
        encrypted = page_data[SALT_SZ : PAGE_SZ - RESERVE_SZ]
        cipher = AES.new(enc_key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(encrypted)
        return bytes(bytearray(SQLITE_HDR + decrypted + b'\x00' * RESERVE_SZ))
    else:
        encrypted = page_data[: PAGE_SZ - RESERVE_SZ]
        cipher = AES.new(enc_key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(encrypted)
        return decrypted + b'\x00' * RESERVE_SZ


def full_decrypt(db_path, out_path, enc_key):
    file_size = os.path.getsize(db_path)
    total_pages = file_size // PAGE_SZ
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(db_path, 'rb') as fin, open(out_path, 'wb') as fout:
        for pgno in range(1, total_pages + 1):
            page = fin.read(PAGE_SZ)
            if len(page) < PAGE_SZ:
                if len(page) > 0:
                    page = page + b'\x00' * (PAGE_SZ - len(page))
                else:
                    break
            fout.write(decrypt_page(enc_key, page, pgno))
    return total_pages


def decrypt_wal(wal_path, out_path, enc_key):
    if not os.path.exists(wal_path):
        return 0
    wal_size = os.path.getsize(wal_path)
    if wal_size <= WAL_HEADER_SZ:
        return 0
    frame_size = WAL_FRAME_HEADER_SZ + PAGE_SZ
    patched = 0
    with open(wal_path, 'rb') as wf, open(out_path, 'r+b') as df:
        wal_hdr = wf.read(WAL_HEADER_SZ)
        wal_salt1 = struct.unpack('>I', wal_hdr[16:20])[0]
        wal_salt2 = struct.unpack('>I', wal_hdr[20:24])[0]
        while wf.tell() + frame_size <= wal_size:
            fh = wf.read(WAL_FRAME_HEADER_SZ)
            if len(fh) < WAL_FRAME_HEADER_SZ:
                break
            pgno = struct.unpack('>I', fh[0:4])[0]
            frame_salt1 = struct.unpack('>I', fh[8:12])[0]
            frame_salt2 = struct.unpack('>I', fh[12:16])[0]
            ep = wf.read(PAGE_SZ)
            if len(ep) < PAGE_SZ:
                break
            if pgno == 0 or pgno > 1000000:
                continue
            if frame_salt1 != wal_salt1 or frame_salt2 != wal_salt2:
                continue
            dec = decrypt_page(enc_key, ep, pgno)
            df.seek((pgno - 1) * PAGE_SZ)
            df.write(dec)
            patched += 1
    return patched


# ============ DB 缓存 ============

class DBCache:
    """缓存解密后的 DB，通过 mtime 检测变化。使用固定文件名，重启后可复用。"""

    CACHE_DIR = _cfg.get("mcp_cache_dir") or (
        os.path.join(os.path.dirname(CONFIG_FILE), "mcp_cache")
        if os.environ.get("WECHAT_DECRYPT_DATA_DIR")
        else os.path.join(tempfile.gettempdir(), "wechat_mcp_cache")
    )
    MTIME_FILE = os.path.join(CACHE_DIR, "_mtimes.json")

    def __init__(self):
        self._cache = {}  # rel_key -> (db_mtime, wal_mtime, tmp_path)
        os.makedirs(self.CACHE_DIR, exist_ok=True)
        self._load_persistent_cache()

    def _cache_path(self, rel_key):
        """rel_key -> 固定的缓存文件路径"""
        h = hashlib.md5(rel_key.encode()).hexdigest()[:12]
        return os.path.join(self.CACHE_DIR, f"{h}.db")

    def _load_persistent_cache(self):
        """启动时从磁盘恢复缓存映射，验证 mtime 后复用"""
        if not os.path.exists(self.MTIME_FILE):
            return
        try:
            with open(self.MTIME_FILE, encoding="utf-8") as f:
                saved = json.load(f)
        except (json.JSONDecodeError, OSError):
            return
        reused = 0
        for rel_key, info in saved.items():
            tmp_path = info["path"]
            if not os.path.exists(tmp_path):
                continue
            rel_path = rel_key.replace('\\', os.sep)
            db_path = os.path.join(DB_DIR, rel_path)
            wal_path = db_path + "-wal"
            try:
                db_mtime = os.path.getmtime(db_path)
                wal_mtime = os.path.getmtime(wal_path) if os.path.exists(wal_path) else 0
            except OSError:
                continue
            if db_mtime == info["db_mt"] and wal_mtime == info["wal_mt"]:
                self._cache[rel_key] = (db_mtime, wal_mtime, tmp_path)
                reused += 1
        if reused:
            print(f"[DBCache] reused {reused} cached decrypted DBs from previous run", flush=True)

    def _save_persistent_cache(self):
        """持久化缓存映射到磁盘"""
        data = {}
        for rel_key, (db_mt, wal_mt, path) in self._cache.items():
            data[rel_key] = {"db_mt": db_mt, "wal_mt": wal_mt, "path": path}
        try:
            with open(self.MTIME_FILE, 'w', encoding="utf-8") as f:
                json.dump(data, f)
        except OSError:
            pass

    def get(self, rel_key):
        key_info = get_key_info(ALL_KEYS, rel_key)
        if not key_info:
            return None
        rel_path = rel_key.replace('\\', '/').replace('/', os.sep)
        db_path = os.path.join(DB_DIR, rel_path)
        wal_path = db_path + "-wal"
        if not os.path.exists(db_path):
            return None

        try:
            db_mtime = os.path.getmtime(db_path)
            wal_mtime = os.path.getmtime(wal_path) if os.path.exists(wal_path) else 0
        except OSError:
            return None

        if rel_key in self._cache:
            c_db_mt, c_wal_mt, c_path = self._cache[rel_key]
            if c_db_mt == db_mtime and c_wal_mt == wal_mtime and os.path.exists(c_path):
                return c_path

        tmp_path = self._cache_path(rel_key)
        enc_key = bytes.fromhex(key_info["enc_key"])
        full_decrypt(db_path, tmp_path, enc_key)
        if os.path.exists(wal_path):
            decrypt_wal(wal_path, tmp_path, enc_key)
        self._cache[rel_key] = (db_mtime, wal_mtime, tmp_path)
        self._save_persistent_cache()
        return tmp_path

    def cleanup(self):
        """正常退出时保存缓存映射（不删文件，下次启动可复用）"""
        self._save_persistent_cache()


_cache = DBCache()
atexit.register(_cache.cleanup)


# ============ 联系人缓存 ============

_contact_names = None  # {username: display_name}
_contact_full = None   # [{username, nick_name, remark, alias, description, phone}]
_contact_tags = None   # {label_id: {name, sort_order, members: [{username, display_name}]}}
_self_username = None
_contact_db_mtime = 0  # mtime of the decrypted contact.db when caches were last populated


def _invalidate_contact_caches():
    global _contact_names, _contact_full, _contact_tags, _self_username
    _contact_names = None
    _contact_full = None
    _contact_tags = None
    _self_username = None
_XML_UNSAFE_RE = re.compile(r'<!DOCTYPE|<!ENTITY', re.IGNORECASE)
_XML_PARSE_MAX_LEN = 20000
_QUERY_LIMIT_MAX = 500
_HISTORY_QUERY_BATCH_SIZE = 500


def _load_contacts_from(db_path):
    names = {}
    full = []
    conn = sqlite3.connect(db_path)
    try:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(contact)").fetchall()
        }
        optional_columns = {
            "alias": "",
            "description": "",
            "phone": "",
            "phone_number": "",
            "mobile": "",
            "mobile_phone": "",
            "telephone": "",
        }
        select_columns = ["username", "nick_name", "remark"]
        select_columns.extend(
            col for col in optional_columns
            if col in columns and col not in select_columns
        )
        # 过滤群成员 (local_type=3, 每个群每个成员一条 → 数量爆炸 → 共 9416 假联系人)
        # 仅在 local_type 列存在时加 WHERE, 兼容老版本 schema (#117 fix)
        sql = (
            "SELECT " + ", ".join(f"[{col}]" for col in select_columns)
            + " FROM contact"
        )
        if "local_type" in columns:
            sql += " WHERE local_type != 3"
        rows = conn.execute(sql).fetchall()
        for r in rows:
            data = dict(zip(select_columns, r))
            uname = data.get("username")
            nick = data.get("nick_name")
            remark = data.get("remark")
            display = remark if remark else nick if nick else uname
            names[uname] = display
            phone = ""
            for col in ("phone", "phone_number", "mobile", "mobile_phone", "telephone"):
                if data.get(col):
                    phone = data.get(col) or ""
                    break
            full.append({
                'username': uname,
                'nick_name': nick or '',
                'remark': remark or '',
                'alias': data.get("alias") or '',
                'description': data.get("description") or '',
                'phone': phone,
            })
    finally:
        conn.close()
    return names, full


def _get_contact_db_path():
    """获取 contact.db 路径并按 mtime 决定是否清缓存。

    优先实时解密路径（DBCache 已经按源 mtime 触发重解密），其次回退到
    静态已解密副本。任何一次 mtime 变化都使内存缓存失效，避免新增联系人
    或改名/改备注后 MCP 查询仍读到旧数据。
    """
    global _contact_db_mtime

    path = _cache.get(os.path.join("contact", "contact.db"))
    if not path:
        pre = os.path.join(DECRYPTED_DIR, "contact", "contact.db")
        path = pre if os.path.exists(pre) else None

    if not path:
        return None

    try:
        mt = os.path.getmtime(path)
    except OSError:
        return path

    if mt != _contact_db_mtime:
        _invalidate_contact_caches()
        _contact_db_mtime = mt

    return path


def get_contact_names():
    global _contact_names, _contact_full

    path = _get_contact_db_path()
    if not path:
        return {}

    if _contact_names is not None:
        return _contact_names

    try:
        _contact_names, _contact_full = _load_contacts_from(path)
        return _contact_names
    except Exception:
        return {}


def get_contact_full():
    get_contact_names()
    return _contact_full or []


def get_contact_tag_names_by_username():
    tags = _load_contact_tags()
    by_username = {}
    for tag in tags.values():
        name = tag.get('name') or ''
        if not name:
            continue
        for member in tag.get('members', []):
            username = member.get('username')
            if username:
                by_username.setdefault(username, []).append(name)
    return by_username


def _extract_pb_field_30(data):
    """从 extra_buffer (protobuf) 中提取 Field #30 的字符串值（联系人标签ID）"""
    if not data:
        return None
    pos = 0
    n = len(data)
    while pos < n:
        # 读 varint tag
        tag = 0
        shift = 0
        while pos < n:
            b = data[pos]; pos += 1
            tag |= (b & 0x7f) << shift
            if not (b & 0x80):
                break
            shift += 7
        field_num = tag >> 3
        wire_type = tag & 0x07
        if wire_type == 0:  # varint
            while pos < n and data[pos] & 0x80:
                pos += 1
            pos += 1
        elif wire_type == 2:  # length-delimited
            length = 0; shift = 0
            while pos < n:
                b = data[pos]; pos += 1
                length |= (b & 0x7f) << shift
                if not (b & 0x80):
                    break
                shift += 7
            if field_num == 30:
                try:
                    return data[pos:pos + length].decode('utf-8')
                except Exception:
                    return None
            pos += length
        elif wire_type == 1:  # 64-bit
            pos += 8
        elif wire_type == 5:  # 32-bit
            pos += 4
        else:
            break
    return None


def _load_contact_tags():
    """加载并缓存联系人标签数据"""
    global _contact_tags

    db_path = _get_contact_db_path()
    if not db_path:
        return {}

    if _contact_tags is not None:
        return _contact_tags

    try:
        conn = sqlite3.connect(db_path)
    except Exception:
        return {}

    try:
        # 1. 加载标签定义
        try:
            label_rows = conn.execute(
                "SELECT label_id_, label_name_, sort_order_ FROM contact_label ORDER BY sort_order_"
            ).fetchall()
        except sqlite3.OperationalError:
            return {}
        if not label_rows:
            return {}

        labels = {}
        for lid, lname, sort_order in label_rows:
            labels[lid] = {'name': lname, 'sort_order': sort_order, 'members': []}

        # 2. 扫描联系人的标签关联
        names = get_contact_names()
        rows = conn.execute(
            "SELECT username, extra_buffer FROM contact WHERE extra_buffer IS NOT NULL"
        ).fetchall()

        for username, buf in rows:
            label_str = _extract_pb_field_30(buf)
            if not label_str:
                continue
            display = names.get(username, username)
            for lid_s in label_str.split(','):
                try:
                    lid = int(lid_s.strip())
                except (ValueError, AttributeError):
                    continue
                if lid in labels:
                    labels[lid]['members'].append({'username': username, 'display_name': display})

        _contact_tags = labels
        return _contact_tags
    except Exception:
        return {}
    finally:
        conn.close()


# ============ 辅助函数 ============

def format_msg_type(t):
    base_type, _ = _split_msg_type(t)
    return {
        1: '文本', 3: '图片', 34: '语音', 42: '名片',
        43: '视频', 47: '表情', 48: '位置', 49: '链接/文件',
        50: '通话', 10000: '系统', 10002: '撤回',
    }.get(base_type, f'type={t}')


def _split_msg_type(t):
    try:
        t = int(t)
    except (TypeError, ValueError):
        return 0, 0
    # WeChat packs the base type into the low 32 bits and app subtype into the high 32 bits.
    if t > 0xFFFFFFFF:
        return t & 0xFFFFFFFF, t >> 32
    return t, 0


def resolve_username(chat_name):
    """将聊天名/备注名/wxid 解析为 username"""
    names = get_contact_names()

    # 直接是 username
    if chat_name in names or chat_name.startswith('wxid_') or '@chatroom' in chat_name:
        return chat_name

    # 模糊匹配(优先精确包含)
    chat_lower = chat_name.lower()
    for uname, display in names.items():
        if chat_lower == display.lower():
            return uname
    for uname, display in names.items():
        if chat_lower in display.lower():
            return uname

    return None


_zstd_dctx = zstd.ZstdDecompressor()


def _decompress_content(content, ct):
    """解压 zstd 压缩的消息内容"""
    if ct and ct == 4 and isinstance(content, bytes):
        try:
            return _zstd_dctx.decompress(content).decode('utf-8', errors='replace')
        except Exception:
            return None
    if isinstance(content, bytes):
        try:
            return content.decode('utf-8', errors='replace')
        except Exception:
            return None
    return content


def _parse_message_content(content, local_type, is_group):
    """解析消息内容，返回 (sender_id, text)。

    群消息 content 形如 'wxid_xxx:\n<xml...>'；某些 type=19 合并转发也会
    写成 'wxid_xxx:<?xml...' 或 'wxid_xxx:<msg...' 不带换行——剥离逻辑两种都要处理。
    """
    if content is None:
        return '', ''
    if isinstance(content, bytes):
        return '', '(二进制内容)'

    sender = ''
    text = content
    if is_group:
        if ':\n' in content:
            sender, text = content.split(':\n', 1)
        else:
            # 'sender:<?xml...' / 'sender:<msg...' 等无换行 case
            m = re.match(r'^([A-Za-z0-9_\-@.]+):(<\?xml|<msg|<msglist|<voipmsg|<sysmsg)', content)
            if m:
                sender = m.group(1)
                text = content[len(sender) + 1:]

    return sender, text


def _collapse_text(text):
    if not text:
        return ''
    return re.sub(r'\s+', ' ', text).strip()


def _get_self_username():
    global _self_username

    if not DB_DIR:
        return ''

    names = get_contact_names()

    if _self_username:
        return _self_username

    account_dir = os.path.basename(os.path.dirname(DB_DIR))
    candidates = [account_dir]

    m = re.fullmatch(r'(.+)_([0-9a-fA-F]{4,})', account_dir)
    if m:
        candidates.insert(0, m.group(1))

    for candidate in candidates:
        if candidate and candidate in names:
            _self_username = candidate
            return _self_username

    return ''


def _load_name2id_maps(conn):
    id_to_username = {}
    try:
        rows = conn.execute("SELECT rowid, user_name FROM Name2Id").fetchall()
    except sqlite3.Error:
        return id_to_username

    for rowid, user_name in rows:
        if not user_name:
            continue
        id_to_username[rowid] = user_name
    return id_to_username


def _display_name_for_username(username, names):
    if not username:
        return ''
    if username == _get_self_username():
        return 'me'
    return names.get(username, username)


def _resolve_sender_label(real_sender_id, sender_from_content, is_group, chat_username, chat_display_name, names, id_to_username):
    sender_username = id_to_username.get(real_sender_id, '')

    if is_group:
        if sender_username and sender_username != chat_username:
            return _display_name_for_username(sender_username, names)
        if sender_from_content:
            return _display_name_for_username(sender_from_content, names)
        return ''

    if sender_username == chat_username:
        return chat_display_name
    if sender_username:
        return _display_name_for_username(sender_username, names)
    return ''


def _resolve_quote_sender_label(ref_user, ref_display_name, is_group, chat_username, chat_display_name, names):
    if is_group:
        if ref_user:
            return _display_name_for_username(ref_user, names)
        return ref_display_name or ''

    self_username = _get_self_username()
    if ref_user:
        if ref_user == chat_username:
            return chat_display_name
        if self_username and ref_user == self_username:
            return 'me'
        return names.get(ref_user, ref_display_name or ref_user)
    if ref_display_name:
        if ref_display_name == chat_display_name:
            return chat_display_name
        self_display_name = names.get(self_username, self_username) if self_username else ''
        if self_display_name and ref_display_name == self_display_name:
            return 'me'
        return ref_display_name
    return ''


# 合并转发消息（含 recorditem 内嵌 XML）在 dataitem 数量多时显著超过默认 20K 上限，
# 实测真实 outer XML 可达 ~500KB。caller 可通过 max_len 参数为 type=19 类大消息放宽限制。
_RECORD_XML_PARSE_MAX_LEN = 500_000


def _safe_basename(name):
    """对 user-derived filename（从消息 XML 来，不可信）做严格 sanitize。

    Reject 而不是 normalize：哪怕 os.path.basename 把 '../foo' 剥成 'foo' 是
    safe 的，意图依然可疑，应该显式失败让用户看到。
    """
    if not name:
        return ''
    if '\x00' in name:
        return ''
    if os.path.isabs(name):
        return ''
    # 任何 path separator 或 .. component 直接拒（不做 normalize）
    parts = name.replace('\\', '/').split('/')
    if any(p in ('', '.', '..') for p in parts) and len(parts) > 1:
        return ''
    if len(parts) > 1:
        return ''
    if name in ('.', '..'):
        return ''
    return name


def _path_under_root(path, root):
    """resolve realpath 后确认仍在 root 下（防 symlink 跳出）。"""
    try:
        real_path = os.path.realpath(path)
        real_root = os.path.realpath(root)
    except OSError:
        return False
    return real_path == real_root or real_path.startswith(real_root + os.sep)


# 大附件 md5 校验时的安全上限：超过此 size 直接拒绝校验（避免 MCP 进程
# 在 100MB+ 视频/附件上一次性 read() 整文件爆内存或长时间阻塞）。
_MD5_VERIFY_MAX_SIZE = 500 * 1024 * 1024  # 500 MB
_MD5_CHUNK_SIZE = 64 * 1024  # 64 KB


def _md5_file_chunked(path, max_size=_MD5_VERIFY_MAX_SIZE):
    """流式分块计算文件 md5，避免大文件一次读完爆内存。

    超过 max_size 直接拒绝（DoS 防御 + 大附件 md5 校验现实意义不大）。
    返回 (md5_hex, error)；成功时 error 为 None。
    """
    try:
        size = os.path.getsize(path)
    except OSError as e:
        return None, f"无法读取文件 size: {e}"
    if size > max_size:
        return None, f"文件 size {size:,} 超过 md5 校验上限 {max_size:,}（防 DoS）"
    h = hashlib.md5()
    try:
        with open(path, 'rb') as f:
            while True:
                chunk = f.read(_MD5_CHUNK_SIZE)
                if not chunk:
                    break
                h.update(chunk)
    except OSError as e:
        return None, f"读取文件失败: {e}"
    return h.hexdigest().lower(), None


def _parse_xml_root(content, max_len=_XML_PARSE_MAX_LEN):
    if not content or len(content) > max_len or _XML_UNSAFE_RE.search(content):
        return None

    try:
        return ET.fromstring(content)
    except ET.ParseError:
        return None


def _parse_int(value, fallback=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _parse_app_message_outer(content):
    """Parse outer appmsg XML，对 type=19 合并卡片自动放宽到 _RECORD_XML_PARSE_MAX_LEN。

    所有解析 outer appmsg 的 caller（get_chat_history 渲染 / decode_file_message /
    decode_record_item）共用此 helper，避免同一条大消息在不同 caller 上行为不一致。
    Substring 短路保证非 type=19 的大 appmsg 不付出 500K parse 代价。"""
    root = _parse_xml_root(content)
    if root is None and content and len(content) <= _RECORD_XML_PARSE_MAX_LEN:
        if '<type>19</type>' in content:
            root = _parse_xml_root(content, max_len=_RECORD_XML_PARSE_MAX_LEN)
    return root


def _format_namecard_text(content):
    """Parse type=42 (名片) XML into a compact human-readable line.

    Source XML carries dozens of fields (antispamticket, biznamecardinfo,
    brand URLs, image MD5s) but the useful signal is just three attrs:
    ``nickname`` (display name), ``username`` (wxid; ``gh_*`` for 公众号),
    and ``certinfo`` (the user-authored bio). Everything else is either
    auth tokens that should not be piped to downstream systems, or
    rendering metadata that bloats the chat log without helping a human
    or an LLM understand the conversation.
    """
    root = _parse_xml_root(content)
    if root is None:
        return None
    nickname = (root.get("nickname") or "").strip()
    username = (root.get("username") or "").strip()
    certinfo = _collapse_text(root.get("certinfo") or "")
    if not nickname and not username:
        return None
    head = nickname or username
    if username.startswith("gh_"):
        head = f"{head} (公众号 {username})"
    return f"[名片] {head}: {certinfo}" if certinfo else f"[名片] {head}"


# 微信位置消息 <location> 的字段名 → 结构化键名。
#
# 字段语义分类 (#121 自撤回的教训：必须逐字段过语义，不能套 #83 namecard 的
# "丢敏感字段" 模板)。1411 条真实样本统计 + 用户分享 vs 客户端渲染二分：
#
#   user-shared signal (用户在地图上主动选/填) → 进 decode_location 结构化层：
#     poiname / label / poiid / poiCategoryTips / poiPhone / poiBusinessHour /
#     poiPriceTips / isFromPoiList / cityname / adcode / buildingId / floorName
#   主信号坐标 (精度数字，不进单行渲染避免 LLM context 污染)：
#     x (实际是纬度) / y (实际是经度)
#   schema slot 但本 corpus 0% 非空 (defensive 保留，别家账号可能填)：
#     infourl / version
#   渲染样式 / enum / 冗余 (defensive 保留供 debug，不参与渲染决策)：
#     maptype / scale / fromusername
_LOCATION_TEXT_FIELDS = (
    'label', 'poiname', 'poiid', 'poiCategoryTips', 'poiBusinessHour',
    'poiPhone', 'poiPriceTips', 'isFromPoiList', 'cityname', 'adcode',
    'buildingId', 'floorName', 'infourl', 'maptype', 'scale',
    'fromusername', 'version',
)


def _extract_location_info(content):
    """Parse type=48 (位置) XML into a structured dict, or return None.

    返回字段在 _LOCATION_TEXT_FIELDS 之外还有 lat/lng (x/y 数值化)。
    所有缺失字段返回空串而非 None，跟 _extract_transfer_info 一致。
    """
    root = _parse_xml_root(content)
    if root is None:
        return None
    loc = root.find('.//location')
    if loc is None:
        return None

    def _attr(name):
        return _collapse_text(loc.get(name) or '')

    def _f(name):
        v = loc.get(name)
        if v is None or v == '':
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    info = {k: _attr(k) for k in _LOCATION_TEXT_FIELDS}
    info['lat'] = _f('x')  # 微信 x 实际是纬度
    info['lng'] = _f('y')  # 微信 y 实际是经度
    info['category_top'] = info['poiCategoryTips'].split(':', 1)[0] if info['poiCategoryTips'] else ''
    return info


def _is_location_poiname_placeholder(poiname):
    """检测客户端在用户手扔图钉 (未选 POI) 时填的占位符。

    实测样本：poiname="[位置]" / poiname="[Location]"。形如 [*] 一律视为占位符，
    渲染层需要 fallback 到 label。
    """
    if not poiname:
        return True
    return len(poiname) >= 2 and poiname.startswith('[') and poiname.endswith(']')


def _format_location_text(content):
    """Parse type=48 (位置) XML into a compact human-readable line.

    Source XML carries 15+ attrs (poiBusinessHour, poiPhone, adcode, buildingId,
    floorName, maptype, scale, fromusername, infourl, ...) but the chat-log line
    only needs 3 signals: category (顶层 poiCategoryTips 主类，形如 "主类")，
    poiname (POI 名)，label (地址串)。经纬度精度数字进单行只会污染 LLM context；
    电话/营业时间/价格/POI id/adcode/buildingId/floorName/version 留给
    ``decode_location`` 结构化工具。

    Fallback chain:
      1) <location> 节点缺失 → None (caller 退到 "[位置]")
      2) poiname 是 "[位置]"/"[Location]" 类占位符 → 用 label (用户手扔图钉场景)
      3) poiname 与 label 都缺 → "[位置]" (不堆 lat/lng 数字)
    """
    info = _extract_location_info(content)
    if not info:
        return None

    category = info['category_top']
    head = f"[位置·{category}]" if category else "[位置]"
    poiname = info['poiname']
    label = info['label']

    if _is_location_poiname_placeholder(poiname):
        # 用户手扔图钉：poiname 是占位符，label 才是用户看到的描述
        return f"{head} {label}" if label else head

    if not poiname and not label:
        return "[位置]"
    if poiname and label and poiname != label:
        return f"{head} {poiname} @ {label}"
    return f"{head} {poiname or label}"


def _format_app_message_text(content, local_type, is_group, chat_username, chat_display_name, names):
    if not content or '<appmsg' not in content:
        return None

    _, sub_type = _split_msg_type(local_type)
    root = _parse_app_message_outer(content)
    if root is None:
        return None

    appmsg = root.find('.//appmsg')
    if appmsg is None:
        return None

    title = _collapse_text(appmsg.findtext('title') or '')
    app_type_text = (appmsg.findtext('type') or '').strip()
    app_type = _parse_int(app_type_text, _parse_int(sub_type, 0))

    if app_type == 57:
        return _format_refer_message_text(
            appmsg, is_group, chat_username, chat_display_name, names
        )

    if app_type == 19:
        return _format_record_message_text(appmsg, title)

    if app_type == 2000:
        return _format_transfer_message_text(appmsg, title)
    if app_type == 2001:
        return _format_redpacket_message_text(appmsg, title)

    if app_type == 6:
        return f"[文件] {title}" if title else "[文件]"
    if app_type == 5:
        return f"[链接] {title}" if title else "[链接]"
    if app_type == 51:
        return _format_finder_message_text(appmsg, title)
    if app_type in (33, 36, 44):
        return f"[小程序] {title}" if title else "[小程序]"
    if title:
        return f"[链接/文件] {title}"
    return "[链接/文件]"


_RECORD_MAX_ITEMS = 50
_RECORD_MAX_LINE_LEN = 200

# 合并转发 dataitem 的 datatype → wechat 缓存子目录映射。仅这 4 类有真本地
# binary 文件；其他 datatype（链接/名片/小程序/视频号 等）只有 metadata。
_RECORD_BINARY_SUBDIR = {'8': 'F', '2': 'Img', '5': 'V', '4': 'A'}

# datatype → 中文标签，散在多处使用：渲染合并卡片 / decode_record_item 的
# 错误提示 / 单元测试。统一在模块顶部维护避免漂移。
_RECORD_DATATYPE_LABEL = {
    '1': '文本', '2': '图片', '3': '名片', '4': '语音',
    '5': '视频', '6': '链接', '7': '位置', '8': '文件',
    '17': '聊天记录', '19': '小程序', '22': '视频号',
    '23': '视频号直播', '29': '音乐', '36': '小程序/H5',
    '37': '表情包',
}


def _format_record_dataitem(item):
    """格式化合并记录中的单个 dataitem，返回展示文本。"""
    datatype = (item.get('datatype') or '').strip()

    if datatype == '1':
        return _collapse_text(item.findtext('datadesc') or '') or '[文本]'
    if datatype in ('2', '3', '4', '5', '7', '23', '37'):
        return f"[{_RECORD_DATATYPE_LABEL[datatype]}]"
    if datatype in ('6', '36'):
        link_title = _collapse_text(item.findtext('datatitle') or '')
        label = _RECORD_DATATYPE_LABEL[datatype]
        return f"[{label}] {link_title}" if link_title else f"[{label}]"
    if datatype == '8':
        file_title = _collapse_text(item.findtext('datatitle') or '')
        return f"[文件] {file_title}" if file_title else '[文件]'
    if datatype == '17':
        nested_title = _collapse_text(item.findtext('datatitle') or '')
        return f"[聊天记录] {nested_title}" if nested_title else '[聊天记录]'
    if datatype == '19':
        # 小程序：appbranditem/sourcedisplayname 是直接子代，不需要 .// 递归
        app_name = _collapse_text(item.findtext('appbranditem/sourcedisplayname') or '')
        item_title = _collapse_text(item.findtext('datatitle') or '')
        label = item_title or app_name or '小程序'
        return f"[小程序] {label}"
    if datatype == '22':
        feed_desc = _collapse_text(item.findtext('finderFeed/desc') or '')
        return f"[视频号] {feed_desc[:80]}" if feed_desc else '[视频号]'
    if datatype == '29':
        song = _collapse_text(item.findtext('datatitle') or '')
        artist = _collapse_text(item.findtext('datadesc') or '')
        if song and artist:
            return f"[音乐] {song} - {artist}"
        return f"[音乐] {song}" if song else '[音乐]'

    desc = _collapse_text(item.findtext('datadesc') or '')
    title_text = _collapse_text(item.findtext('datatitle') or '')
    fallback = desc or title_text
    return fallback if fallback else f"[未知类型 {datatype}]"


def _format_record_message_text(appmsg, title):
    """解析合并转发的聊天记录卡片（appmsg type=19, recorditem）。"""
    fallback_title = title or '聊天记录'
    record_node = appmsg.find('recorditem')
    if record_node is None or not record_node.text:
        return f"[聊天记录] {fallback_title}（待加载）"

    inner = _parse_xml_root(record_node.text, max_len=_RECORD_XML_PARSE_MAX_LEN)
    if inner is None:
        return f"[聊天记录] {fallback_title}"

    record_title = _collapse_text(inner.findtext('title') or '') or fallback_title
    is_chatroom = (inner.findtext('isChatRoom') or '').strip() == '1'
    datalist = inner.find('datalist')
    items = list(datalist.findall('dataitem')) if datalist is not None else []
    if not items:
        suffix = "（群聊转发，待加载）" if is_chatroom else "（待加载）"
        return f"[聊天记录] {record_title}{suffix}"

    header = f"[聊天记录] {record_title}"
    if is_chatroom:
        header += "（群聊转发）"
    header += f"，共 {len(items)} 条"

    lines = [header + ":"]
    for idx, item in enumerate(items[:_RECORD_MAX_ITEMS]):
        sender = _collapse_text(item.findtext('sourcename') or '')
        when = _collapse_text(item.findtext('sourcetime') or '')
        content = _format_record_dataitem(item)

        if len(content) > _RECORD_MAX_LINE_LEN:
            content = content[:_RECORD_MAX_LINE_LEN] + '…'

        # 0-based index 让用户能用 decode_record_item(chat, local_id, item_index) 引用
        prefix_parts = [f"[{idx}]"] + [p for p in (when, sender) if p]
        prefix = ' '.join(prefix_parts)
        lines.append(f"  {prefix}: {content}")

    if len(items) > _RECORD_MAX_ITEMS:
        lines.append(f"  …（还有 {len(items) - _RECORD_MAX_ITEMS} 条未显示）")

    return "\n".join(lines)


# 微信转账 (appmsg type=2000, <wcpayinfo>) paysubtype 含义。
# 微信官方无公开文档，此表来自社区抓包归纳。1/3/4 在所有已知版本一致；
# 5/7/8 在不同版本存在变体（"过期已退还"在某些抓包里也归为 4），所以遇到
# 未识别值时降级显示原始数字，方便用户自行核对。
_TRANSFER_PAYSUBTYPE_LABEL = {
    '1': '发起转账',     # 发送方记录：等待对方收钱
    '3': '已收款',       # 双向：发送方看到"对方已收"，接收方看到"已收钱"
    '4': '已退还',       # 主动退还或被退还
    '5': '过期已退还',    # 24h 未收，自动退还（发送方记录）
    '7': '待领取',       # 已发起未接收
    '8': '已领取',       # 部分版本：转账被领取（接收方记录）
}


# 微信引用回复（appmsg type=57, <refermsg>）内层 <type> 的标签映射。
# refermsg/<type> 用的是顶层 base_type 数字（跟 format_msg_type 重合），
# 但语义不同：format_msg_type 给"消息类型 chip"，这里给"被引用消息的一行摘要"，
# 不展开 cdn url / aeskey / md5 等二进制元数据（直接截断 XML 字符串当摘要是
# 现状的 bug，会把"图片/语音/视频/动画表情/嵌套卡片"渲染成乱码——见 issue #44 #45）。
_REFER_INNER_TYPE_LABEL = {
    '1': '文本',         # 特殊：直接展开 content
    '3': '图片',
    '34': '语音',
    '42': '名片',
    '43': '视频',
    '47': '动画表情',
    '48': '位置',
    '49': '链接/卡片',   # 特殊：嵌套 appmsg，进一步解 inner type
    '50': '通话',
}

# refer_type=49 时 content 是嵌套 <msg><appmsg>...，inner appmsg/<type> → 标签。
# 跟合并转发 _RECORD_DATATYPE_LABEL 的数字含义不同（datatype 是 recorditem 的私有
# schema），独立维护。
_INNER_APPMSG_TYPE_LABEL = {
    '5': '链接', '6': '文件', '8': '动画表情卡',
    '19': '聊天记录', '33': '小程序', '36': '小程序',
    '51': '视频号', '57': '引用消息',
    '2000': '转账', '2001': '红包',
}


def _extract_refer_info(appmsg):
    """从 appmsg type=57 解出 refermsg 各字段，返回 dict 或 None。

    refermsg/<content> 是 escape 后的字符串，内层 type 决定其 schema:
      type=1 (纯文本) / 3 (img cdn) / 34 (voicemsg) / 47 (emoji)
      / 49 (嵌套 appmsg) / ...

    refer_content 保留原始字符串（不 collapse），让 _summarize_refer_content
    按 type 进一步处理（type=49 还要再解一层 XML）。其他字段过 _collapse_text
    清掉换行/前后空白。
    """
    refer = appmsg.find('refermsg')
    if refer is None:
        return None

    return {
        'reply_text': _collapse_text(appmsg.findtext('title') or ''),
        'refer_type': _collapse_text(refer.findtext('type') or ''),
        'refer_svrid': _collapse_text(refer.findtext('svrid') or ''),
        'refer_fromusr': _collapse_text(refer.findtext('fromusr') or ''),
        'refer_chatusr': _collapse_text(refer.findtext('chatusr') or ''),
        'refer_displayname': _collapse_text(refer.findtext('displayname') or ''),
        'refer_content': refer.findtext('content') or '',
        'refer_createtime': _collapse_text(refer.findtext('createtime') or ''),
    }


def _summarize_refer_content(refer_type, content, max_len=160):
    """把被引用消息的 content 摘要成一行可读文本。

    分支规则：
      type=1 (文本): 取原文，截断到 max_len
      type=3/34/43/47/...: 给标签兜底，不展开 cdn url / aeskey / md5
      type=49 (嵌套 appmsg): 解一层 inner appmsg/type + title，给"[链接] xxx"
      未识别 type: 给 [type=N] 兜底，方便用户自查

    max_len 只对 type=1 文本生效；标签型摘要本身就短。
    """
    refer_type = (refer_type or '').strip()

    if not content:
        label = _REFER_INNER_TYPE_LABEL.get(refer_type)
        if label:
            return f'[{label}]'
        return f'[type={refer_type}]' if refer_type else '[引用消息]'

    if refer_type == '1':
        text = _collapse_text(content)
        return text[:max_len] + '…' if len(text) > max_len else text

    if refer_type == '49':
        # 嵌套 appmsg：content 是来源不可信的微信侧 payload，走 _parse_xml_root
        # 经 _XML_UNSAFE_RE 过滤 DOCTYPE/ENTITY 防 XXE 注入。
        inner_root = _parse_xml_root(content)
        if inner_root is None:
            return '[卡片]'
        inner_appmsg = inner_root.find('.//appmsg')
        if inner_appmsg is None:
            return '[卡片]'
        inner_type = _collapse_text(inner_appmsg.findtext('type') or '')
        inner_title = _collapse_text(inner_appmsg.findtext('title') or '')
        label = _INNER_APPMSG_TYPE_LABEL.get(
            inner_type, f'卡片 type={inner_type}' if inner_type else '卡片'
        )
        return f'[{label}] {inner_title}' if inner_title else f'[{label}]'

    label = _REFER_INNER_TYPE_LABEL.get(refer_type)
    if label:
        return f'[{label}]'
    return f'[type={refer_type}]'


def _format_refer_message_text(appmsg, is_group, chat_username, chat_display_name, names):
    """渲染微信引用回复（appmsg type=57）的两行展示文本。

    格式:
      <用户的回复正文>
        ↳ 回复 <对方>: <被引用消息摘要>

    fallback:
      1) refermsg 缺失 → 退回到外层 title 兜底
      2) refer_content 空 → summary 给"[refer_type 标签]"或"[引用消息]"
      3) sender 解析不出来 → "回复:" 不带名字
    """
    info = _extract_refer_info(appmsg)
    if info is None:
        title = _collapse_text(appmsg.findtext('title') or '')
        return title or '[引用消息]'

    summary = _summarize_refer_content(info['refer_type'], info['refer_content'])
    sender_label = _resolve_quote_sender_label(
        info['refer_fromusr'], info['refer_displayname'],
        is_group, chat_username, chat_display_name, names
    )

    quote_text = info['reply_text'] or '[引用消息]'
    prefix = f'回复 {sender_label}: ' if sender_label else '回复: '
    quote_text += f'\n  ↳ {prefix}{summary}'
    return quote_text


def _extract_transfer_info(appmsg):
    """从 appmsg type=2000 解出 wcpayinfo 各字段，返回 dict 或 None。

    字段大小写在不同微信版本间漂移（见过 feedesc/feeDesc, pay_memo/paymemo），
    用 lower-case 兜底。所有值用 _collapse_text 清掉换行/前后空白。
    """
    info = appmsg.find('wcpayinfo')
    if info is None:
        return None

    def _pick(*tags):
        for t in tags:
            v = _collapse_text(info.findtext(t) or '')
            if v:
                return v
        return ''

    paysubtype = _pick('paysubtype')
    return {
        'paysubtype': paysubtype,
        'paysubtype_label': _TRANSFER_PAYSUBTYPE_LABEL.get(
            paysubtype, f'未知(paysubtype={paysubtype})' if paysubtype else ''
        ),
        # feedesc 通常是 "¥0.01" 风格的展示串；feedescxml 是富文本变体
        'fee_desc': _pick('feedesc', 'feeDesc'),
        'pay_memo': _pick('pay_memo', 'paymemo'),
        # 三种交易号：transcationid 是微信支付侧（注意拼写是 transc 不是 trans），
        # transferid 是微信内部转账 id，paymsgid 偶见于旧版本
        'transcation_id': _pick('transcationid', 'transcationId'),
        'transfer_id': _pick('transferid', 'transferId'),
        'pay_msg_id': _pick('paymsgid', 'payMsgId'),
        'begin_transfer_time': _pick('begintransfertime', 'beginTransferTime'),
        'invalid_time': _pick('invalidtime', 'invalidTime'),
        'effective_date': _pick('effectivedate', 'effectiveDate'),
        'payer_username': _pick('payer_username', 'payerUsername'),
        'receiver_username': _pick('receiver_username', 'receiverUsername'),
    }


def _format_transfer_message_text(appmsg, title):
    """渲染微信转账（appmsg type=2000）一行展示文本，给 history/export 共用。

    fallback 顺序：
      1) wcpayinfo 缺失 → 只显示 title 兜底，避免吞数据
      2) paysubtype 未知 → 显示原始数字让用户自查
      3) 没有 fee_desc → 至少给个方向标签
    """
    info = _extract_transfer_info(appmsg)
    if not info:
        return f"[转账] {title}" if title else "[转账]"

    label = info['paysubtype_label'] or '转账'
    parts = [f"[转账·{label}]"] if label != '转账' else ["[转账]"]
    if info['fee_desc']:
        parts.append(info['fee_desc'])
    if info['pay_memo']:
        parts.append(f"备注: {info['pay_memo']}")
    return ' '.join(parts)


# 微信红包 <wcpayinfo> 里仅这两类 AA 收款在 <senderdes> 带人均额。标准微信红包的
# 消息 XML 不含金额字段（金额在领取后才可见，不写进聊天消息）。
_REDPACKET_AMOUNT_SCENES = frozenset({'群收款', '活动账单'})
_REDPACKET_AMOUNT_RE = re.compile(r'(\d+(?:\.\d+)?)\s*元')
# 发红包人 wxid 藏在领取链接 <nativeurl> 的 sendusername= 参数里。
_REDPACKET_SENDER_RE = re.compile(r'sendusername=([^&]+)')


def _format_redpacket_message_text(appmsg, title):
    """渲染微信红包（appmsg type=2001）一行展示文本，与转账渲染对称。

    现状：独立红包消息走 _format_app_message_text 的 generic 分支，渲染成无信息的
    [链接/文件]，丢掉 scenetext（场景）/ sendertitle（祝福语）/ 发红包人。
    （_INNER_APPMSG_TYPE_LABEL 的 '2001' 标签仅在引用回复 type=57 的嵌套路径生效。）

    金额：标准微信红包的消息 XML 不含金额（领取后才可见，不写进消息）；仅
    「群收款」/「活动账单」在 <senderdes> 带人均额。

    fallback：wcpayinfo 缺失 → 只显示 title 兜底，避免吞数据。
    """
    info = appmsg.find('wcpayinfo')
    if info is None:
        return f"[红包] {title}" if title else "[红包]"

    scene = _collapse_text(info.findtext('scenetext') or '')
    greeting = _collapse_text(info.findtext('sendertitle') or '')

    parts = [f"[红包·{scene}]" if scene else "[红包]"]
    if greeting:
        parts.append(greeting)
    if scene in _REDPACKET_AMOUNT_SCENES:
        m = _REDPACKET_AMOUNT_RE.search(info.findtext('senderdes') or '')
        if m:
            parts.append(f"人均 {m.group(1)} 元")
    sender = _REDPACKET_SENDER_RE.search(info.findtext('nativeurl') or '')
    if sender:
        parts.append(f"(发自 {sender.group(1)})")
    return ' '.join(parts)


def _format_finder_message_text(appmsg, title):
    """渲染视频号分享（appmsg type=51）一行展示文本。

    现状：独立视频号分享走 _format_app_message_text 的 generic 分支，渲染成无信息的
    [链接/文件]；而同一条视频号被引用回复嵌套时反而有 [视频号] 标签
    （见 _INNER_APPMSG_TYPE_LABEL['51']），两条路径体验不一致。本 helper 把合并转发
    记录 _format_record_dataitem 的 datatype==22 已用的 finderFeed 读法接到独立消息。

    fallback：缺 nickname → [视频号] {title}；title 也缺 → [视频号]。
    """
    nickname = _collapse_text(appmsg.findtext('finderFeed/nickname') or '')
    desc = _collapse_text(appmsg.findtext('finderFeed/desc') or '')
    if nickname:
        return f"[视频号] {nickname}: {desc[:80]}" if desc else f"[视频号] {nickname}"
    return f"[视频号] {title}" if title else "[视频号]"


def _format_voip_message_text(content):
    if not content or '<voip' not in content:
        return None

    root = _parse_xml_root(content)
    if root is None:
        return "[通话]"

    raw_text = _collapse_text(root.findtext('.//msg') or '')
    if not raw_text:
        return "[通话]"

    status_map = {
        'Canceled': '已取消',
        'Line busy': '对方忙线',
        'Already answered elsewhere': '已在其他设备接听',
        'Declined on other device': '已在其他设备拒接',
        'Call canceled by caller': '主叫已取消',
        'Call not answered': '未接听',
        "Call wasn't answered": '未接听',
    }

    if raw_text.startswith('Duration:'):
        duration = raw_text.split(':', 1)[1].strip()
        return f"[通话] 通话时长 {duration}" if duration else "[通话]"

    return f"[通话] {status_map.get(raw_text, raw_text)}"


def _format_voice_text(content):
    if not content or '<voicemsg' not in content:
        return "[语音]"
    root = _parse_xml_root(content)
    if root is None:
        return "[语音]"
    voice = root.find('.//voicemsg')
    if voice is None:
        return "[语音]"
    length_ms = _parse_int(voice.get('voicelength'), 0)
    if length_ms <= 0:
        return "[语音]"
    return f"[语音 {length_ms / 1000:.1f}s]"


def _format_message_text(local_id, local_type, content, is_group, chat_username, chat_display_name, names, create_time=0):
    sender_from_content, text = _parse_message_content(content, local_type, is_group)
    base_type, _ = _split_msg_type(local_type)

    # 同一 chat 的消息可能跨 message_N.db 分片，导致 local_id 跨分片冲突。
    # 把 create_time 一起注入到输出，让 decode_file_message / decode_record_item
    # 能用 (local_id, create_time) 唯一定位 row。
    def _id_suffix():
        return f"(local_id={local_id}, ts={create_time})" if create_time else f"(local_id={local_id})"

    if base_type == 3:
        text = f"[图片] {_id_suffix()}"
    elif base_type == 34:
        text = f"{_format_voice_text(text)} {_id_suffix()}"
    elif base_type == 47:
        text = "[表情]"
    elif base_type == 50:
        text = _format_voip_message_text(text) or "[通话]"
    elif base_type == 42:
        text = _format_namecard_text(text) or "[名片]"
    elif base_type == 48:
        text = _format_location_text(text) or "[位置]"
    elif base_type == 49:
        formatted = _format_app_message_text(
            text, local_type, is_group, chat_username, chat_display_name, names
        ) or "[链接/文件]"
        if formatted.startswith('[文件]'):
            formatted = f"{formatted} {_id_suffix()}"
        elif formatted.startswith('[聊天记录]'):
            # 多行：把 ID 后缀放在 header 末尾，":" 之前
            if '\n' in formatted:
                first_line, rest = formatted.split('\n', 1)
                first_line_no_colon = first_line.rstrip(':').rstrip()
                formatted = f"{first_line_no_colon} {_id_suffix()}:\n{rest}"
            else:
                formatted = f"{formatted} {_id_suffix()}"
        text = formatted
    elif base_type != 1:
        type_label = format_msg_type(local_type)
        text = f"[{type_label}] {text}" if text else f"[{type_label}]"

    return sender_from_content, text


def _is_safe_msg_table_name(table_name):
    return bool(re.fullmatch(r'Msg_[0-9a-f]{32}', table_name))


# 消息 DB 的 rel_keys
# 用 message_\d+\.db$ 匹配，自然排除 message_resource.db / message_fts_*.db
MSG_DB_KEYS = sorted([
    k for k in ALL_KEYS
    if any(v.startswith("message/") for v in key_path_variants(k))
    and any(re.search(r"message_\d+\.db$", v) for v in key_path_variants(k))
])


def _find_msg_table_for_user(username):
    """在所有 message_N.db 中查找用户的消息表，返回 (db_path, table_name)"""
    table_hash = hashlib.md5(username.encode()).hexdigest()
    table_name = f"Msg_{table_hash}"
    if not _is_safe_msg_table_name(table_name):
        return None, None

    for rel_key in MSG_DB_KEYS:
        path = _cache.get(rel_key)
        if not path:
            continue
        conn = sqlite3.connect(path)
        try:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,)
            ).fetchone()
            if exists:
                conn.close()
                return path, table_name
        except Exception:
            pass
        finally:
            conn.close()

    return None, None


def _find_msg_tables_for_user(username):
    """返回用户在所有 message_N.db 中对应的消息表，按最新消息时间倒序排列。"""
    table_hash = hashlib.md5(username.encode()).hexdigest()
    table_name = f"Msg_{table_hash}"
    if not _is_safe_msg_table_name(table_name):
        return []

    matches = []
    for rel_key in MSG_DB_KEYS:
        path = _cache.get(rel_key)
        if not path:
            continue
        conn = sqlite3.connect(path)
        try:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,)
            ).fetchone()
            if not exists:
                continue
            max_create_time = conn.execute(
                f"SELECT MAX(create_time) FROM [{table_name}]"
            ).fetchone()[0] or 0
            matches.append({
                'db_path': path,
                'table_name': table_name,
                'max_create_time': max_create_time,
            })
        except Exception:
            pass
        finally:
            conn.close()

    matches.sort(key=lambda item: item['max_create_time'], reverse=True)
    return matches


def _validate_pagination(limit, offset=0, limit_max=_QUERY_LIMIT_MAX):
    if limit <= 0:
        raise ValueError("limit 必须大于 0")
    if limit_max is not None and limit > limit_max:
        raise ValueError(f"limit 不能大于 {limit_max}")
    if offset < 0:
        raise ValueError("offset 不能小于 0")


def _parse_time_value(value, field_name, is_end=False):
    value = (value or '').strip()
    if not value:
        return None

    formats = [
        ('%Y-%m-%d %H:%M:%S', False),
        ('%Y-%m-%d %H:%M', False),
        ('%Y-%m-%d', True),
    ]
    for fmt, date_only in formats:
        try:
            dt = datetime.strptime(value, fmt)
            if date_only and is_end:
                dt = dt.replace(hour=23, minute=59, second=59)
            return int(dt.timestamp())
        except ValueError:
            continue

    raise ValueError(
        f"{field_name} 格式无效: {value}。支持 YYYY-MM-DD / YYYY-MM-DD HH:MM / YYYY-MM-DD HH:MM:SS"
    )


def _parse_time_range(start_time='', end_time=''):
    start_ts = _parse_time_value(start_time, 'start_time', is_end=False)
    end_ts = _parse_time_value(end_time, 'end_time', is_end=True)
    if start_ts is not None and end_ts is not None and start_ts > end_ts:
        raise ValueError('start_time 不能晚于 end_time')
    return start_ts, end_ts


def _pagination_hint(count, limit, offset):
    """当返回结果数 == limit 时，提示调用方可能还有更多。

    用于工具返回字符串末尾，帮助 LLM 决定是否需要继续翻页。
    返回结果数 < limit 表示已读到当前查询条件下的全部结果，不再提示。
    """
    if limit and count >= limit:
        return f"\n\n（可能还有更多结果，可设 offset={offset + limit} 继续查询）"
    return ""


_MSG_TYPE_MAP = {
    'text': [1],
    'image': [3],
    'voice': [34],
    'namecard': [42],
    'video': [43],
    'emoji': [47],
    'location': [48],
    'app': [49],
    'voip': [50],
    'system': [10000],
}


def _resolve_msg_types(msg_types):
    """把 ['text', 'image'] 风格的输入翻成 local_type 整数列表。

    返回 (type_filter_list, error_msg); 任一项无效返回 (None, error)。
    None / 空列表表示不过滤。
    """
    if not msg_types:
        return None, None
    type_filter = []
    for t in msg_types:
        key = t.strip().lower()
        if key == 'file':
            key = 'app'  # 'file' 是常见叫法; WeChat 把文件归到 type=49 (app message)
        if key not in _MSG_TYPE_MAP:
            return None, (
                f"未知消息类型 \"{t}\"。可选: " + ", ".join(sorted(_MSG_TYPE_MAP))
            )
        type_filter.extend(_MSG_TYPE_MAP[key])
    return type_filter, None


def _build_message_filters(start_ts=None, end_ts=None, keyword='', type_filter=None):
    clauses = []
    params = []
    if start_ts is not None:
        clauses.append('create_time >= ?')
        params.append(start_ts)
    if end_ts is not None:
        clauses.append('create_time <= ?')
        params.append(end_ts)
    if keyword:
        clauses.append('message_content LIKE ?')
        params.append(f'%{keyword}%')
    if type_filter:
        placeholders = ','.join('?' * len(type_filter))
        clauses.append(f'local_type IN ({placeholders})')
        params.extend(type_filter)
    return clauses, params


def _query_messages(conn, table_name, start_ts=None, end_ts=None, keyword='', limit=20, offset=0, oldest_first=False, type_filter=None):
    if not _is_safe_msg_table_name(table_name):
        raise ValueError(f'非法消息表名: {table_name}')

    clauses, params = _build_message_filters(start_ts, end_ts, keyword, type_filter)
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ''
    order = 'ASC' if oldest_first else 'DESC'
    sql = f"""
        SELECT local_id, local_type, create_time, real_sender_id, message_content,
               WCDB_CT_message_content
        FROM [{table_name}]
        {where_sql}
        ORDER BY create_time {order}
    """
    if limit is None:
        return conn.execute(sql, params).fetchall()
    sql += "\n        LIMIT ? OFFSET ?"
    return conn.execute(sql, (*params, limit, offset)).fetchall()


def _resolve_chat_context(chat_name):
    username = resolve_username(chat_name)
    if not username:
        return None

    names = get_contact_names()
    display_name = names.get(username, username)
    message_tables = _find_msg_tables_for_user(username)
    if not message_tables:
        return {
            'query': chat_name,
            'username': username,
            'display_name': display_name,
            'db_path': None,
            'table_name': None,
            'message_tables': [],
            'is_group': '@chatroom' in username,
        }

    primary = message_tables[0]
    return {
        'query': chat_name,
        'username': username,
        'display_name': display_name,
        'db_path': primary['db_path'],
        'table_name': primary['table_name'],
        'message_tables': message_tables,
        'is_group': '@chatroom' in username,
    }


def _resolve_chat_contexts(chat_names):
    if not chat_names:
        raise ValueError('chat_names 不能为空')

    resolved = []
    unresolved = []
    missing_tables = []
    seen = set()

    for chat_name in chat_names:
        name = (chat_name or '').strip()
        if not name:
            unresolved.append('(空)')
            continue
        ctx = _resolve_chat_context(name)
        if not ctx:
            unresolved.append(name)
            continue
        if not ctx['message_tables']:
            missing_tables.append(ctx['display_name'])
            continue
        if ctx['username'] in seen:
            continue
        seen.add(ctx['username'])
        resolved.append(ctx)

    return resolved, unresolved, missing_tables


def _normalize_chat_names(chat_name):
    if chat_name is None:
        return []
    if isinstance(chat_name, str):
        value = chat_name.strip()
        return [value] if value else []
    if isinstance(chat_name, (list, tuple, set)):
        normalized = []
        for item in chat_name:
            if item is None:
                continue
            value = str(item).strip()
            if value:
                normalized.append(value)
        return normalized
    value = str(chat_name).strip()
    return [value] if value else []


def _format_history_lines(rows, username, display_name, is_group, names, id_to_username):
    lines = []
    ctx = {
        'username': username,
        'display_name': display_name,
        'is_group': is_group,
    }
    for row in reversed(rows):
        _, line = _build_history_line(row, ctx, names, id_to_username)
        lines.append(line)
    return lines


def _build_search_entry(row, ctx, names, id_to_username):
    local_id, local_type, create_time, real_sender_id, content, ct = row
    content = _decompress_content(content, ct)
    if content is None:
        return None

    sender, text = _format_message_text(
        local_id, local_type, content, ctx['is_group'], ctx['username'], ctx['display_name'], names,
        create_time=create_time,
    )
    if text and len(text) > 300:
        text = text[:300] + '...'

    sender_label = _resolve_sender_label(
        real_sender_id,
        sender,
        ctx['is_group'],
        ctx['username'],
        ctx['display_name'],
        names,
        id_to_username,
    )
    time_str = datetime.fromtimestamp(create_time).strftime('%Y-%m-%d %H:%M')
    entry = f"[{time_str}] [{ctx['display_name']}]"
    if sender_label:
        entry += f" {sender_label}:"
    entry += f" {text}"
    return create_time, entry


def _build_history_line(row, ctx, names, id_to_username):
    local_id, local_type, create_time, real_sender_id, content, ct = row
    time_str = datetime.fromtimestamp(create_time).strftime('%Y-%m-%d %H:%M')
    content = _decompress_content(content, ct)
    if content is None:
        content = '(无法解压)'

    sender, text = _format_message_text(
        local_id, local_type, content, ctx['is_group'], ctx['username'], ctx['display_name'], names,
        create_time=create_time,
    )

    sender_label = _resolve_sender_label(
        real_sender_id, sender, ctx['is_group'], ctx['username'], ctx['display_name'], names, id_to_username
    )
    if sender_label:
        return create_time, f'[{time_str}] {sender_label}: {text}'
    return create_time, f'[{time_str}] {text}'


def _get_chat_message_tables(ctx):
    if ctx.get('message_tables'):
        return ctx['message_tables']
    if ctx.get('db_path') and ctx.get('table_name'):
        return [{'db_path': ctx['db_path'], 'table_name': ctx['table_name']}]
    return []


def _iter_table_contexts(ctx):
    for table in _get_chat_message_tables(ctx):
        yield {
            'query': ctx['query'],
            'username': ctx['username'],
            'display_name': ctx['display_name'],
            'db_path': table['db_path'],
            'table_name': table['table_name'],
            'is_group': ctx['is_group'],
        }


def _candidate_page_size(limit, offset):
    return limit + offset


def _message_query_batch_size(candidate_limit):
    return candidate_limit


def _history_query_batch_size(candidate_limit):
    return min(candidate_limit, _HISTORY_QUERY_BATCH_SIZE)


def _page_ranked_entries(entries, limit, offset, oldest_first=False):
    ordered = sorted(entries, key=lambda item: item[0], reverse=not oldest_first)
    paged = ordered[offset:offset + limit]
    paged.sort(key=lambda item: item[0])
    return paged


def _collect_chat_history_lines(ctx, names, start_ts=None, end_ts=None, limit=20, offset=0, oldest_first=False, type_filter=None):
    collected = []
    failures = []
    candidate_limit = _candidate_page_size(limit, offset)
    batch_size = _history_query_batch_size(candidate_limit)

    for table_ctx in _iter_table_contexts(ctx):
        try:
            with closing(sqlite3.connect(table_ctx['db_path'])) as conn:
                id_to_username = _load_name2id_maps(conn)
                fetch_offset = 0
                collected_before_table = len(collected)
                # 当前页上的消息一定落在各分表最近的 offset+limit 条记录内。
                while len(collected) - collected_before_table < candidate_limit:
                    rows = _query_messages(
                        conn,
                        table_ctx['table_name'],
                        start_ts=start_ts,
                        end_ts=end_ts,
                        limit=batch_size,
                        offset=fetch_offset,
                        oldest_first=oldest_first,
                        type_filter=type_filter,
                    )
                    if not rows:
                        break
                    fetch_offset += len(rows)

                    for row in rows:
                        try:
                            collected.append(_build_history_line(row, table_ctx, names, id_to_username))
                        except Exception as e:
                            failures.append(
                                f"{table_ctx['display_name']} local_id={row[0]} create_time={row[2]}: {e}"
                            )
                        if len(collected) - collected_before_table >= candidate_limit:
                            break

                    if len(rows) < batch_size:
                        break
        except Exception as e:
            failures.append(f"{table_ctx['db_path']}: {e}")

    paged = _page_ranked_entries(collected, limit, offset, oldest_first=oldest_first)
    return [line for _, line in paged], failures


def _collect_chat_search_entries(ctx, names, keyword, start_ts=None, end_ts=None, candidate_limit=20):
    collected = []
    failures = []
    contexts_by_db = {}
    for table_ctx in _iter_table_contexts(ctx):
        contexts_by_db.setdefault(table_ctx['db_path'], []).append(table_ctx)

    for db_path, db_contexts in contexts_by_db.items():
        try:
            with closing(sqlite3.connect(db_path)) as conn:
                db_entries, db_failures = _collect_search_entries(
                    conn,
                    db_contexts,
                    names,
                    keyword,
                    start_ts=start_ts,
                    end_ts=end_ts,
                    candidate_limit=candidate_limit,
                )
                collected.extend(db_entries)
                failures.extend(db_failures)
        except Exception as e:
            failures.extend(f"{table_ctx['display_name']}: {e}" for table_ctx in db_contexts)

    return collected, failures


def _load_search_contexts_from_db(conn, db_path, names):
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
    ).fetchall()

    table_to_username = {}
    try:
        for (user_name,) in conn.execute("SELECT user_name FROM Name2Id").fetchall():
            if not user_name:
                continue
            table_hash = hashlib.md5(user_name.encode()).hexdigest()
            table_to_username[f"Msg_{table_hash}"] = user_name
    except sqlite3.Error:
        pass

    contexts = []
    for (table_name,) in tables:
        username = table_to_username.get(table_name, '')
        display_name = names.get(username, username) if username else table_name
        contexts.append({
            'query': display_name,
            'username': username,
            'display_name': display_name,
            'db_path': db_path,
            'table_name': table_name,
            'is_group': '@chatroom' in username,
        })
    return contexts


def _collect_search_entries(conn, contexts, names, keyword, start_ts=None, end_ts=None, candidate_limit=20):
    collected = []
    failures = []
    id_to_username = _load_name2id_maps(conn)
    batch_size = _message_query_batch_size(candidate_limit)

    for ctx in contexts:
        try:
            fetch_offset = 0
            collected_before_table = len(collected)
            # 全局分页只需要每个分表最新的 offset+limit 条有效命中，无需把整表命中读进内存。
            while len(collected) - collected_before_table < candidate_limit:
                rows = _query_messages(
                    conn,
                    ctx['table_name'],
                    start_ts=start_ts,
                    end_ts=end_ts,
                    keyword=keyword,
                    limit=batch_size,
                    offset=fetch_offset,
                )
                if not rows:
                    break
                fetch_offset += len(rows)

                for row in rows:
                    formatted = _build_search_entry(row, ctx, names, id_to_username)
                    if formatted:
                        collected.append(formatted)
                        if len(collected) - collected_before_table >= candidate_limit:
                            break

                if len(rows) < batch_size:
                    break
        except Exception as e:
            failures.append(f"{ctx['display_name']}: {e}")

    return collected, failures


def _page_search_entries(entries, limit, offset):
    return _page_ranked_entries(entries, limit, offset)


def _search_single_chat(ctx, keyword, start_ts, end_ts, start_time, end_time, limit, offset):
    names = get_contact_names()
    candidate_limit = _candidate_page_size(limit, offset)

    entries, failures = _collect_chat_search_entries(
        ctx,
        names,
        keyword,
        start_ts=start_ts,
        end_ts=end_ts,
        candidate_limit=candidate_limit,
    )

    paged = _page_search_entries(entries, limit, offset)

    if not paged:
        if failures:
            return "查询失败: " + "；".join(failures)
        return f"未在 {ctx['display_name']} 中找到包含 \"{keyword}\" 的消息"

    header = f"在 {ctx['display_name']} 中搜索 \"{keyword}\" 找到 {len(paged)} 条结果（offset={offset}, limit={limit}）"
    if start_time or end_time:
        header += f"\n时间范围: {start_time or '最早'} ~ {end_time or '最新'}"
    if failures:
        header += "\n查询失败: " + "；".join(failures)
    return header + ":\n\n" + "\n\n".join(item[1] for item in paged) + _pagination_hint(len(paged), limit, offset)


def _search_multiple_chats(chat_names, keyword, start_ts, end_ts, start_time, end_time, limit, offset):
    try:
        resolved_contexts, unresolved, missing_tables = _resolve_chat_contexts(chat_names)
    except ValueError as e:
        return f"错误: {e}"

    if not resolved_contexts:
        details = []
        if unresolved:
            details.append("未找到联系人: " + "、".join(unresolved))
        if missing_tables:
            details.append("无消息表: " + "、".join(missing_tables))
        suffix = f"\n{chr(10).join(details)}" if details else ""
        return f"错误: 没有可查询的聊天对象{suffix}"

    names = get_contact_names()
    candidate_limit = _candidate_page_size(limit, offset)
    collected = []
    failures = []
    for ctx in resolved_contexts:
        chat_entries, chat_failures = _collect_chat_search_entries(
            ctx,
            names,
            keyword,
            start_ts=start_ts,
            end_ts=end_ts,
            candidate_limit=candidate_limit,
        )
        collected.extend(chat_entries)
        failures.extend(chat_failures)

    paged = _page_search_entries(collected, limit, offset)

    notes = []
    if unresolved:
        notes.append("未找到联系人: " + "、".join(unresolved))
    if missing_tables:
        notes.append("无消息表: " + "、".join(missing_tables))
    if failures:
        notes.append("查询失败: " + "；".join(failures))

    if not paged:
        header = f"在 {len(resolved_contexts)} 个聊天对象中未找到包含 \"{keyword}\" 的消息"
        if start_time or end_time:
            header += f"\n时间范围: {start_time or '最早'} ~ {end_time or '最新'}"
        if notes:
            header += "\n" + "\n".join(notes)
        return header

    header = (
        f"在 {len(resolved_contexts)} 个聊天对象中搜索 \"{keyword}\" 找到 {len(paged)} 条结果"
        f"（offset={offset}, limit={limit}）"
    )
    if start_time or end_time:
        header += f"\n时间范围: {start_time or '最早'} ~ {end_time or '最新'}"
    if notes:
        header += "\n" + "\n".join(notes)
    return header + ":\n\n" + "\n\n".join(item[1] for item in paged) + _pagination_hint(len(paged), limit, offset)


def _search_all_messages(keyword, start_ts, end_ts, start_time, end_time, limit, offset):
    names = get_contact_names()
    collected = []
    failures = []
    candidate_limit = _candidate_page_size(limit, offset)

    for rel_key in MSG_DB_KEYS:
        path = _cache.get(rel_key)
        if not path:
            continue

        try:
            with closing(sqlite3.connect(path)) as conn:
                contexts = _load_search_contexts_from_db(conn, path, names)
                db_entries, db_failures = _collect_search_entries(
                    conn,
                    contexts,
                    names,
                    keyword,
                    start_ts=start_ts,
                    end_ts=end_ts,
                    candidate_limit=candidate_limit,
                )
                collected.extend(db_entries)
                failures.extend(db_failures)
        except Exception as e:
            failures.append(f"{rel_key}: {e}")

    paged = _page_search_entries(collected, limit, offset)

    if not paged:
        header = f"未找到包含 \"{keyword}\" 的消息"
        if start_time or end_time:
            header += f"\n时间范围: {start_time or '最早'} ~ {end_time or '最新'}"
        if failures:
            header += "\n查询失败: " + "；".join(failures)
        return header

    header = f"搜索 \"{keyword}\" 找到 {len(paged)} 条结果（offset={offset}, limit={limit}）"
    if start_time or end_time:
        header += f"\n时间范围: {start_time or '最早'} ~ {end_time or '最新'}"
    if failures:
        header += "\n查询失败: " + "；".join(failures)
    return header + ":\n\n" + "\n\n".join(item[1] for item in paged) + _pagination_hint(len(paged), limit, offset)


# ============ MCP Server ============

mcp = FastMCP(
    "local-message-source",
    instructions=(
        "查询本机消息联系人和历史消息。优先使用 list_contacts 获取聊天 ID，"
        "再用 query_messages 按明确时间范围查询；跨聊天检索使用 search_messages。"
    ),
)


def _tool_text(name):
    """FastMCP 1.x tests call decorated functions directly; FastMCP 2.x may return tool results."""
    return name


_mcp_tool = mcp.tool


def _guarded_tool(name_or_fn=None, **decorator_kwargs):
    """注册带版本门禁的工具，同时保留可直接测试调用的 Python 函数。"""
    explicit_name = name_or_fn if isinstance(name_or_fn, str) else None

    def wrap(fn):
        @functools.wraps(fn)
        def guarded(*args, **kwargs):
            from wechat_version_guard import check_or_raise
            check_or_raise(_cfg, action=f"调用 MCP 工具 {fn.__name__}")
            return fn(*args, **kwargs)

        registration_kwargs = dict(decorator_kwargs)
        if explicit_name is not None:
            registration_kwargs["name"] = explicit_name
        # 直接传函数可避免 FastMCP 2.x 的无参 partial 再次调用已替换的 mcp.tool。
        _mcp_tool(guarded, **registration_kwargs)
        return guarded

    if callable(name_or_fn):
        return wrap(name_or_fn)
    return wrap


mcp.tool = _guarded_tool

# 新消息追踪
_last_check_state = {}  # {username: last_timestamp}


@mcp.tool()
def get_recent_sessions(limit: int = 20) -> str:
    """获取微信最近会话列表，包含最新消息摘要、未读数、时间等。
    用于了解最近有哪些人/群在聊天。

    Args:
        limit: 返回的会话数量，默认20
    """
    path = _cache.get(os.path.join("session", "session.db"))
    if not path:
        return "错误: 无法解密 session.db"

    names = get_contact_names()
    with closing(sqlite3.connect(path)) as conn:
        rows = conn.execute("""
            SELECT username, unread_count, summary, last_timestamp,
                   last_msg_type, last_msg_sender, last_sender_display_name
            FROM SessionTable
            WHERE last_timestamp > 0
            ORDER BY last_timestamp DESC
            LIMIT ?
        """, (limit,)).fetchall()

    results = []
    for r in rows:
        username, unread, summary, ts, msg_type, sender, sender_name = r
        display = names.get(username, username)
        is_group = '@chatroom' in username

        if isinstance(summary, bytes):
            try:
                summary = _zstd_dctx.decompress(summary).decode('utf-8', errors='replace')
            except Exception:
                summary = '(压缩内容)'
        if isinstance(summary, str) and ':\n' in summary:
            summary = summary.split(':\n', 1)[1]

        sender_display = ''
        if is_group and sender:
            sender_display = names.get(sender, sender_name or sender)

        time_str = datetime.fromtimestamp(ts).strftime('%m-%d %H:%M')

        entry = f"[{time_str}] {display}"
        if is_group:
            entry += " [群]"
        if unread and unread > 0:
            entry += f" ({unread}条未读)"
        entry += f"\n  {format_msg_type(msg_type)}: "
        if sender_display:
            entry += f"{sender_display}: "
        entry += str(summary or "(无内容)")

        results.append(entry)

    return f"最近 {len(results)} 个会话:\n\n" + "\n\n".join(results)


@mcp.tool()
def get_chat_history(chat_name: str, limit: int = 50, offset: int = 0, start_time: str = "", end_time: str = "", oldest_first: bool = False, msg_types: Optional[List[str]] = None) -> str:
    """查询指定联系人或群聊的历史消息。

    适用场景：
    - 查看某个联系人或群聊在指定时间范围内的上下文
    - 追踪群聊讨论、整理近期观点、核对消息来源
    - 通过 list_contacts/get_contacts 找到名称或 ID 后进一步查询

    参数说明：
    - chat_name: 聊天对象的名字、备注名或 ID，支持模糊匹配
    - limit: 返回消息数量；大范围查询建议分页
    - offset: 分页偏移量
    - start_time/end_time: 可选时间范围，格式 YYYY-MM-DD、YYYY-MM-DD HH:MM 或 YYYY-MM-DD HH:MM:SS
    - oldest_first: True 时从最早消息开始返回；默认返回最新消息页后按阅读顺序展示
    - msg_types: 消息类型过滤，可选 text/image/voice/video/file/emoji/location/namecard/voip/system

    返回：
    文本消息列表，每行包含发送时间、发送者和格式化后的消息内容。群聊结果会显示群 ID。

    注意：
    - 面向 Agent 的新调用优先使用 query_messages，它强制传入 start_time
    - 查询超过 7 天或 limit 很大时可能较慢，建议缩小时间范围或分页
    - 图片、语音、文件等非文本内容只返回摘要，详情需使用对应 decode 工具

    Args:
        chat_name: 聊天对象的名字、备注名或wxid，自动模糊匹配
        limit: 返回的消息数量，默认50；支持较大的值，建议配合 offset 分页使用
        offset: 分页偏移量，默认0
        start_time: 起始时间，支持 YYYY-MM-DD / YYYY-MM-DD HH:MM / YYYY-MM-DD HH:MM:SS
        end_time: 结束时间，支持 YYYY-MM-DD / YYYY-MM-DD HH:MM / YYYY-MM-DD HH:MM:SS
        oldest_first: 为 True 时返回最早的消息（默认 False 返回最新消息）
        msg_types: 按消息类型过滤，可选值: text, image, voice, video, file(=app),
            emoji, location, namecard, voip, system。传 None 或不传表示不过滤
    """
    try:
        _validate_pagination(limit, offset, limit_max=None)
        start_ts, end_ts = _parse_time_range(start_time, end_time)
    except ValueError as e:
        return f"错误: {e}"

    type_filter, type_err = _resolve_msg_types(msg_types)
    if type_err:
        return f"错误: {type_err}"

    ctx = _resolve_chat_context(chat_name)
    if not ctx:
        return f"找不到聊天对象: {chat_name}\n提示: 可以用 get_contacts(query='{chat_name}') 搜索联系人"
    if not ctx['db_path']:
        return f"找不到 {ctx['display_name']} 的消息记录（可能在未解密的DB中或无消息）"

    names = get_contact_names()
    lines, failures = _collect_chat_history_lines(
        ctx,
        names,
        start_ts=start_ts,
        end_ts=end_ts,
        limit=limit,
        offset=offset,
        oldest_first=oldest_first,
        type_filter=type_filter,
    )

    if not lines:
        if failures:
            return "查询失败: " + "；".join(failures)
        return f"{ctx['display_name']} 无消息记录"

    header = f"{ctx['display_name']} 的消息记录（返回 {len(lines)} 条，offset={offset}, limit={limit}）"
    if ctx['is_group']:
        header += f" [群聊, id={ctx['username']}]"
    elif ctx['username'] != ctx['display_name']:
        header += f" [id={ctx['username']}]"
    if start_time or end_time:
        header += f"\n时间范围: {start_time or '最早'} ~ {end_time or '最新'}"
    if msg_types:
        header += f"\n类型过滤: {', '.join(msg_types)}"
    if failures:
        header += "\n查询失败: " + "；".join(failures)
    return header + ":\n\n" + "\n".join(lines) + _pagination_hint(len(lines), limit, offset)


@mcp.tool()
def query_messages(
    chat_id: str,
    start_time: str,
    end_time: str = "",
    keyword: str = "",
    limit: int = 200,
    offset: int = 0,
) -> str:
    """查询指定联系人或群聊在明确时间范围内的历史消息。

    适用场景：
    - 追踪投研群讨论，例如“某投研群最近 3 天的券商观点”
    - 检索指定聊天内的标的、公司、行业或关键词提及
    - 获取一段时间内的消息供后续归纳分析，要求保留来源和时间

    参数：
    - chat_id: 群聊或联系人 ID/名称，通过 list_contacts 获取；群聊 ID 通常包含 @chatroom
    - start_time: 必填，起始时间，支持 YYYY-MM-DD、YYYY-MM-DD HH:MM 或 YYYY-MM-DD HH:MM:SS
    - end_time: 可选，结束时间；为空表示查询到最新消息
    - keyword: 可选关键词过滤，适合股票代码、公司名、行业词或人名
    - limit: 返回条数，默认 200；大范围查询建议分段或分页
    - offset: 分页偏移量，默认 0

    返回：
    结构化文本列表，每条包含 timestamp、sender 和 content 信息；标题包含聊天名称、ID、时间范围和分页信息。

    注意事项：
    - start_time 必须明确，避免无边界全量查询导致超时
    - 跨群分析请分别调用 query_messages 后再聚合
    - 仅本机已解密且有权限访问的数据可查询
    """
    if not start_time:
        return "错误: start_time 为必填参数，请指定明确起始时间，例如 2026-06-01 00:00:00"
    if keyword:
        return search_messages(
            keyword=keyword,
            chat_name=chat_id,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
            offset=offset,
        )
    return get_chat_history(
        chat_name=chat_id,
        limit=limit,
        offset=offset,
        start_time=start_time,
        end_time=end_time,
        oldest_first=True,
        msg_types=["text"],
    )


@mcp.tool()
def search_messages(
    keyword: str,
    chat_name: Optional[Union[str, List[str]]] = None,
    start_time: str = "",
    end_time: str = "",
    limit: int = 20,
    offset: int = 0,
) -> str:
    """跨聊天搜索消息内容，支持全库、指定单聊/群聊、多个聊天对象、时间范围和分页。

    适用场景：
    - 不确定目标群聊时，全局搜索某个标的代码、公司名、人物或事件
    - 比较多个群聊对同一主题的讨论热度
    - 在已知聊天范围内查找关键词，再用 query_messages 拉取上下文

    参数：
    - keyword: 必填搜索关键词，支持股票代码、公司名、行业词等
    - chat_name: 可选聊天对象名称或 ID；为空时全库搜索；也可传字符串列表做跨群搜索
    - start_time/end_time: 可选时间范围；投研类分析建议明确传入
    - limit: 返回结果数量，默认 20，最大 500
    - offset: 分页偏移量

    返回：
    搜索命中列表，每条包含时间、聊天名称、发送者和命中的文本摘要。

    与 query_messages 的区别：
    - search_messages 适合“先找在哪里提到过”
    - query_messages 适合“已知聊天和时间范围后拉取完整上下文”

    Args:
        keyword: 搜索关键词
        chat_name: 聊天对象名称，可为空、单个字符串或字符串列表
        start_time: 起始时间，可为空
        end_time: 结束时间，可为空
        limit: 返回的结果数量，默认20，最大500
        offset: 分页偏移量，默认0
    """
    if not keyword or len(keyword) < 1:
        return "请提供搜索关键词"

    chat_names = _normalize_chat_names(chat_name)

    try:
        _validate_pagination(limit, offset)
        start_ts, end_ts = _parse_time_range(start_time, end_time)
    except ValueError as e:
        return f"错误: {e}"

    if len(chat_names) == 1:
        ctx = _resolve_chat_context(chat_names[0])
        if not ctx:
            return f"找不到聊天对象: {chat_names[0]}\n提示: 可以用 get_contacts(query='{chat_names[0]}') 搜索联系人"
        if not ctx['db_path']:
            return f"找不到 {ctx['display_name']} 的消息记录（可能在未解密的DB中或无消息）"
        return _search_single_chat(
            ctx,
            keyword,
            start_ts,
            end_ts,
            start_time,
            end_time,
            limit,
            offset,
        )

    if len(chat_names) > 1:
        return _search_multiple_chats(
            chat_names,
            keyword,
            start_ts,
            end_ts,
            start_time,
            end_time,
            limit,
            offset,
        )

    return _search_all_messages(
        keyword,
        start_ts,
        end_ts,
        start_time,
        end_time,
        limit,
        offset,
    )

@mcp.tool()
def get_contacts(query: str = "", limit: int = 50) -> str:
    """搜索或列出联系人和群聊。

    适用场景：
    - 根据群名、联系人备注、昵称或 ID 查找可查询对象
    - 在调用 get_chat_history/query_messages 前确认聊天对象 ID

    参数：
    - query: 搜索关键词，匹配昵称、备注名或 ID；留空列出联系人和群聊
    - limit: 返回数量，默认 50

    返回：
    每行包含 username/ID、备注和昵称。群聊 ID 通常包含 @chatroom。

    Args:
        query: 搜索关键词（匹配昵称、备注名、wxid），留空列出所有
        limit: 返回数量，默认50
    """
    contacts = get_contact_full()
    if not contacts:
        return "错误: 无法加载联系人数据"

    if query:
        q = query.lower()
        filtered = [
            c for c in contacts
            if q in c['nick_name'].lower()
            or q in c['remark'].lower()
            or q in c['username'].lower()
        ]
    else:
        filtered = contacts

    total = len(filtered)
    filtered = filtered[:limit]

    if not filtered:
        return f"未找到匹配 \"{query}\" 的联系人"

    lines = []
    for c in filtered:
        line = c['username']
        if c['remark']:
            line += f"  备注: {c['remark']}"
        if c['nick_name']:
            line += f"  昵称: {c['nick_name']}"
        lines.append(line)

    header = f"找到 {len(filtered)} 个联系人"
    if query:
        header += f"（搜索: {query}）"
    result = header + ":\n\n" + "\n".join(lines)
    if total > limit:
        result += f"\n\n（共 {total} 个匹配，当前仅显示前 {limit} 个，可增大 limit 查看更多）"
    return result


@mcp.tool()
def list_contacts(query: str = "", limit: int = 100) -> str:
    """列出或搜索本机消息数据源中的联系人和群聊。

    适用场景：
    - 用户提供群名或联系人名称时，先调用本工具确认准确 chat_id
    - 需要枚举可分析的数据源，例如“有哪些投研群可以查询”
    - 后续 query_messages 的 chat_id 参数必须从这里获取或确认

    参数：
    - query: 可选搜索词，匹配备注、昵称、别名、描述和 ID；为空时列出前 limit 个
    - limit: 返回数量，默认 100；结果过多时请加 query 缩小范围

    返回字段：
    - id: 联系人或群聊 ID，传给 query_messages 的 chat_id
    - name: 优先使用备注名，其次昵称，最后 ID
    - type: group 或 contact
    - alias/description: 如本地数据库存在则返回辅助识别信息

    注意：
    - 群聊 ID 通常包含 @chatroom
    - 查不到目标时，请换用更短的关键词或直接用 get_contacts 兼容工具搜索
    """
    contacts = get_contact_full()
    if not contacts:
        return "错误: 无法加载联系人数据"

    q = (query or "").lower()
    if q:
        filtered = [
            c for c in contacts
            if q in (c.get("nick_name") or "").lower()
            or q in (c.get("remark") or "").lower()
            or q in (c.get("username") or "").lower()
            or q in (c.get("alias") or "").lower()
            or q in (c.get("description") or "").lower()
        ]
    else:
        filtered = contacts

    total = len(filtered)
    filtered = filtered[:limit]
    if not filtered:
        return f'未找到匹配 "{query}" 的联系人或群聊'

    lines = []
    for c in filtered:
        username = c.get("username") or ""
        display = c.get("remark") or c.get("nick_name") or username
        kind = "group" if "@chatroom" in username else "contact"
        parts = [f"id={username}", f"name={display}", f"type={kind}"]
        if c.get("alias"):
            parts.append(f"alias={c['alias']}")
        if c.get("description"):
            parts.append(f"description={c['description']}")
        lines.append(" | ".join(parts))

    header = f"返回 {len(filtered)} 个联系人/群聊"
    if query:
        header += f"（搜索: {query}）"
    result = header + ":\n\n" + "\n".join(lines)
    if total > limit:
        result += f"\n\n（共 {total} 个匹配，当前仅显示前 {limit} 个；请增加 limit 或使用 query 缩小范围）"
    return result


@mcp.tool()
def get_contact_info(contact_id: str) -> str:
    """获取单个联系人或群聊的本地元数据详情。

    适用场景：
    - 调用 query_messages 前确认某个 chat_id 对应的备注、昵称、类型
    - 分析输出需要标注群聊/联系人来源信息
    - 用户给出 wxid 或 @chatroom ID，需要转成人可读名称

    参数：
    - contact_id: 联系人或群聊 ID，也可传备注/昵称并进行模糊解析

    返回：
    id、name、type、remark、nick_name、alias、description、phone 等本地可用字段。

    注意：
    - 仅返回本地联系人数据库已有的元数据
    - 群聊成员详情不在本工具返回范围内
    """
    username = resolve_username(contact_id) or contact_id
    contacts = get_contact_full()
    for c in contacts:
        if c.get("username") == username:
            display = c.get("remark") or c.get("nick_name") or username
            kind = "group" if "@chatroom" in username else "contact"
            fields = [
                f"id: {username}",
                f"name: {display}",
                f"type: {kind}",
                f"remark: {c.get('remark') or ''}",
                f"nick_name: {c.get('nick_name') or ''}",
                f"alias: {c.get('alias') or ''}",
                f"description: {c.get('description') or ''}",
                f"phone: {c.get('phone') or ''}",
            ]
            return "\n".join(fields)
    return f"未找到联系人或群聊: {contact_id}。请先调用 list_contacts(query='{contact_id}') 确认 ID。"


@mcp.tool()
def get_contact_tags() -> str:
    """列出所有微信联系人标签及成员数量。"""
    tags = _load_contact_tags()
    if not tags:
        return "未找到标签数据（contact_label 表可能不存在）"

    sorted_tags = sorted(tags.values(), key=lambda t: t['sort_order'])
    total_assoc = sum(len(t['members']) for t in sorted_tags)

    lines = [f"共 {len(sorted_tags)} 个标签，{total_assoc} 个关联:\n"]
    for t in sorted_tags:
        lines.append(f"  [{t['name']}] {len(t['members'])}人")
    return "\n".join(lines)


@mcp.tool()
def get_tag_members(tag_name: str) -> str:
    """获取指定标签下的所有联系人。支持模糊匹配标签名。

    Args:
        tag_name: 标签名称，支持精确和模糊匹配
    """
    tags = _load_contact_tags()
    if not tags:
        return "未找到标签数据（contact_label 表可能不存在）"

    q = tag_name.strip().lower()

    # 精确匹配
    exact = [t for t in tags.values() if t['name'].lower() == q]
    if exact:
        matched = exact[0]
    else:
        # 模糊匹配 (contains)
        fuzzy = [t for t in tags.values() if q in t['name'].lower()]
        if not fuzzy:
            all_names = [t['name'] for t in sorted(tags.values(), key=lambda t: t['sort_order'])]
            return f"未找到匹配 \"{tag_name}\" 的标签。\n\n现有标签: {', '.join(all_names)}"
        if len(fuzzy) == 1:
            matched = fuzzy[0]
        else:
            names = [t['name'] for t in fuzzy]
            return f"找到 {len(fuzzy)} 个匹配的标签，请指定:\n" + "\n".join(f"  [{n}]" for n in names)

    members = matched['members']
    if not members:
        return f"标签 [{matched['name']}] 没有成员"

    lines = [f"标签 [{matched['name']}] 共 {len(members)} 人:\n"]
    for m in members:
        line = m['username']
        if m['display_name'] != m['username']:
            line += f"  {m['display_name']}"
        lines.append(f"  {line}")
    return "\n".join(lines)


@mcp.tool()
def get_new_messages() -> str:
    """获取自上次调用以来的新消息。首次调用返回最近的会话状态。"""
    global _last_check_state

    path = _cache.get(os.path.join("session", "session.db"))
    if not path:
        return "错误: 无法解密 session.db"

    names = get_contact_names()
    with closing(sqlite3.connect(path)) as conn:
        rows = conn.execute("""
            SELECT username, unread_count, summary, last_timestamp,
                   last_msg_type, last_msg_sender, last_sender_display_name
            FROM SessionTable
            WHERE last_timestamp > 0
            ORDER BY last_timestamp DESC
        """).fetchall()

    curr_state = {}
    for r in rows:
        username, unread, summary, ts, msg_type, sender, sender_name = r
        curr_state[username] = {
            'unread': unread, 'summary': summary, 'timestamp': ts,
            'msg_type': msg_type, 'sender': sender or '', 'sender_name': sender_name or '',
        }

    if not _last_check_state:
        _last_check_state = {u: s['timestamp'] for u, s in curr_state.items()}
        # 首次调用，返回有未读的会话
        unread_msgs = []
        for username, s in curr_state.items():
            if s['unread'] and s['unread'] > 0:
                display = names.get(username, username)
                is_group = '@chatroom' in username
                summary = s['summary']
                if isinstance(summary, bytes):
                    try:
                        summary = _zstd_dctx.decompress(summary).decode('utf-8', errors='replace')
                    except Exception:
                        summary = '(压缩内容)'
                if isinstance(summary, str) and ':\n' in summary:
                    summary = summary.split(':\n', 1)[1]
                time_str = datetime.fromtimestamp(s['timestamp']).strftime('%H:%M')
                tag = "[群]" if is_group else ""
                unread_msgs.append(f"[{time_str}] {display}{tag} ({s['unread']}条未读): {summary}")

        if unread_msgs:
            return f"当前 {len(unread_msgs)} 个未读会话:\n\n" + "\n".join(unread_msgs)
        return "当前无未读消息（已记录状态，下次调用将返回新消息）"

    # 对比上次状态
    new_msgs = []
    for username, s in curr_state.items():
        prev_ts = _last_check_state.get(username, 0)
        if s['timestamp'] > prev_ts:
            display = names.get(username, username)
            is_group = '@chatroom' in username
            summary = s['summary']
            if isinstance(summary, bytes):
                try:
                    summary = _zstd_dctx.decompress(summary).decode('utf-8', errors='replace')
                except Exception:
                    summary = '(压缩内容)'
            if isinstance(summary, str) and ':\n' in summary:
                summary = summary.split(':\n', 1)[1]

            sender_display = ''
            if is_group and s['sender']:
                sender_display = names.get(s['sender'], s['sender_name'] or s['sender'])

            time_str = datetime.fromtimestamp(s['timestamp']).strftime('%H:%M:%S')
            entry = f"[{time_str}] {display}"
            if is_group:
                entry += " [群]"
            entry += f": {format_msg_type(s['msg_type'])}"
            if sender_display:
                entry += f" ({sender_display})"
            entry += f" - {summary}"
            new_msgs.append((s['timestamp'], entry))

    _last_check_state = {u: s['timestamp'] for u, s in curr_state.items()}

    if not new_msgs:
        return "无新消息"

    new_msgs.sort(key=lambda x: x[0])
    entries = [m[1] for m in new_msgs]
    return f"{len(entries)} 条新消息:\n\n" + "\n".join(entries)


# ============ 图片解密 ============

_image_aes_key = _cfg.get("image_aes_key")  # V2 格式 AES key (从微信内存提取)
_image_xor_key = _cfg.get("image_xor_key", 0x88)
_image_resolver = ImageResolver(
    WECHAT_BASE_DIR, DECODED_IMAGE_DIR, _cache,
    aes_key=_image_aes_key, xor_key=_image_xor_key,
)


@mcp.tool()
def decode_image(chat_name: str, local_id: int) -> str:
    """解密微信聊天中的一张图片。

    先用 get_chat_history 查看消息，图片消息会显示 local_id，
    然后用此工具解密对应图片。

    Args:
        chat_name: 聊天对象的名字、备注名或wxid
        local_id: 图片消息的 local_id（从 get_chat_history 获取）
    """
    username = resolve_username(chat_name)
    if not username:
        return f"找不到聊天对象: {chat_name}"

    result = _image_resolver.decode_image(username, local_id)
    if result['success']:
        return (
            f"解密成功!\n"
            f"  文件: {result['path']}\n"
            f"  格式: {result['format']}\n"
            f"  大小: {result['size']:,} bytes\n"
            f"  MD5: {result['md5']}"
        )
    else:
        error = result['error']
        if 'md5' in result:
            error += f"\n  MD5: {result['md5']}"
        return f"解密失败: {error}"


@mcp.tool()
def decode_file_message(chat_name: str, local_id: int, create_time: int = 0) -> str:
    """获取微信聊天中外层文件消息（PDF/docx/xlsx 等）的本地副本路径。

    微信会把对方发来的文件下载到 ~/Library/.../msg/file/{YYYY-MM}/原文件名.{ext}
    （macOS）。本工具从消息记录解析出文件名/大小，在本地缓存中精确定位，
    然后返回原始路径，可直接交给 Read/PDF 工具读取。

    使用流程：先用 get_chat_history 找到 [文件] xxx.pdf (local_id=N, ts=T)，
    把 N 和 T 一起传给本工具。create_time(ts) 用于跨分片场景下唯一定位。

    Args:
        chat_name: 聊天对象的名字、备注名或wxid
        local_id: 文件消息的 local_id（从 get_chat_history 获取）
        create_time: 消息的 unix 时间戳，从 get_chat_history 输出 ts=N 部分获取。
            用于在 local_id 跨分片冲突时唯一定位；传 0 时若多个分片含同 local_id 会报歧义错误
    """
    try:
        local_id = int(local_id)
        create_time = int(create_time)
    except (TypeError, ValueError):
        return "错误: local_id 和 create_time 必须是整数"

    username = resolve_username(chat_name)
    if not username:
        return f"找不到聊天对象: {chat_name}"

    # 同一 chat 的消息可能分散在多个 message_N.db 分片中。扫所有分片收集 row，
    # 多于一条就报歧义错误（避免 silent decoding wrong message）。
    shards = _find_msg_tables_for_user(username)
    if not shards:
        return f"找不到 {chat_name} 的消息表"

    # 扫所有分片收集 row。如果调用者传了 create_time，用 (local_id, create_time)
    # 精确匹配；否则只按 local_id 收集，多匹配时报歧义并提示加 create_time。
    matches = []
    for shard in shards:
        if not _is_safe_msg_table_name(shard['table_name']):
            continue
        with closing(sqlite3.connect(shard['db_path'])) as conn:
            if create_time:
                candidate_row = conn.execute(
                    f"SELECT local_type, create_time, message_content, WCDB_CT_message_content "
                    f"FROM [{shard['table_name']}] WHERE local_id=? AND create_time=?",
                    (local_id, create_time)
                ).fetchone()
            else:
                candidate_row = conn.execute(
                    f"SELECT local_type, create_time, message_content, WCDB_CT_message_content "
                    f"FROM [{shard['table_name']}] WHERE local_id=?",
                    (local_id,)
                ).fetchone()
        if candidate_row:
            matches.append((shard['db_path'], candidate_row))
    if not matches:
        if create_time:
            return f"找不到 (local_id={local_id}, create_time={create_time}) 的消息（已扫描 {len(shards)} 个分片）"
        return f"找不到 local_id={local_id} 的消息（已扫描 {len(shards)} 个分片）"
    if len(matches) > 1:
        details = []
        for db_p, r in matches:
            ct = r[1]
            ts_str = datetime.fromtimestamp(ct).isoformat() if ct else '?'
            details.append(f"{os.path.basename(db_p)} create_time={ct} ({ts_str})")
        return (
            f"local_id={local_id} 在 {len(matches)} 个分片中都存在，无法唯一定位:\n  "
            + '\n  '.join(details)
            + f"\n请加 create_time 参数：decode_file_message(chat_name, local_id={local_id}, create_time=N)"
        )

    _, row = matches[0]
    local_type, create_time, content, ct_compress = row
    base_type, _ = _split_msg_type(local_type)
    if base_type != 49:
        return (
            f"不是文件消息（local_type={local_type}，base_type={base_type}），"
            f"文件消息应为 base_type=49 且 appmsg type=6"
        )

    xml_text = _decompress_content(content, ct_compress)
    if not xml_text:
        return "消息 content 为空或无法解码"

    # 复用项目内现有 helper 剥离群聊 sender 前缀，避免自己写启发式
    is_group = username.endswith('@chatroom')
    _, xml_text = _parse_message_content(xml_text, local_type, is_group)

    root = _parse_app_message_outer(xml_text)
    if root is None:
        return "无法解析消息 XML"

    appmsg = root.find('.//appmsg')
    if appmsg is None:
        return "消息中没有 appmsg 段（可能不是文件类型）"

    # 必须是 appmsg type=6 (文件)，否则可能是链接/小程序/合并转发等带 title 的卡片，
    # 按 title/size 全盘搜会误命中无关本地文件并伪装成"找到了"。
    app_type_in_msg = _parse_int(_collapse_text(appmsg.findtext('type') or ''), 0)
    if app_type_in_msg != 6:
        return (
            f"不是文件消息（appmsg type={app_type_in_msg}）。"
            f"文件消息要求 appmsg type=6；type=19 请用 decode_record_item，"
            f"type=5/33/36/44 等是链接/小程序，没有可下载的本地文件"
        )

    raw_title = _collapse_text(appmsg.findtext('title') or '')
    fileext = _collapse_text(appmsg.findtext('.//fileext') or '')
    totallen = _parse_int(_collapse_text(appmsg.findtext('.//totallen') or ''), 0)
    # md5 字段在 type=6 外层（不是 appattach 子节点）—— 用于强校验候选文件归属
    expected_md5 = _collapse_text(appmsg.findtext('md5') or '').lower()

    # 没有 appattach 节点 = 不是真正的文件消息（type=6 必带 appattach）
    if appmsg.find('appattach') is None:
        return "消息没有 appattach 节点（可能 schema 异常或不是真文件消息）"

    if not raw_title:
        return "消息中没有文件名 (title)"

    # title 来自不可信的 message XML，对方可能发恶意消息（含绝对路径或 ../）。
    # 必须 sanitize 成 safe basename 才能拼路径 + glob，否则有 path-traversal 风险。
    title = _safe_basename(raw_title)
    if not title:
        return f"消息中的文件名 {raw_title!r} 不安全（含绝对路径/路径分隔符/..），拒绝处理"

    # 性能优化：先按消息时间精确定位 msg/file/{YYYY-MM}/，命中即返回；
    # 否则才退回 walk 全盘 os.walk（msg/attach 含数十万小文件，全盘扫描可达数秒）
    candidates = []
    msg_file_dir = os.path.join(WECHAT_BASE_DIR, 'msg/file')
    if create_time and os.path.isdir(msg_file_dir):
        # 同名文件可能落到收到消息的当月、上一月或下一月（罕见跨月边界）
        ts_dt = datetime.fromtimestamp(create_time)
        candidate_months = {
            ts_dt.strftime('%Y-%m'),
            (ts_dt - timedelta(days=31)).strftime('%Y-%m'),
            (ts_dt + timedelta(days=31)).strftime('%Y-%m'),
        }
        escaped_stem = glob.escape(os.path.splitext(title)[0])
        ext = os.path.splitext(title)[1]
        for ym in candidate_months:
            month_dir = os.path.join(msg_file_dir, ym)
            if not os.path.isdir(month_dir):
                continue
            # 精确匹配 + 同名 (1)(2) 后缀变体
            for pattern in (
                glob.escape(title),
                f"{escaped_stem}*{glob.escape(ext)}" if ext else f"{escaped_stem}*",
            ):
                for hit in glob.glob(os.path.join(month_dir, pattern)):
                    # 有 totallen 时立刻 size 验证：避免月扫命中"同名但 size 不对"的副本
                    # 阻塞 walk 兜底，最终返回错误文件
                    if totallen:
                        try:
                            if os.path.getsize(hit) != totallen:
                                continue
                        except OSError:
                            continue
                    if hit not in candidates:
                        candidates.append(hit)

    # 退路：未命中或没 create_time 时只 walk msg/file（slow path 兜底）。
    # 文件名匹配严格化：只接受精确匹配或 wechat 自动加副本的 "(N)" 后缀变体，
    # 不做 stem 子串匹配——避免 "某某论文.pdf" 被当成 "论文.pdf"。
    if not candidates:
        d = os.path.join(WECHAT_BASE_DIR, 'msg/file')
        stem, ext = os.path.splitext(title)
        copy_pattern = re.compile(
            r'^' + re.escape(stem) + r' ?\(\d+\)' + re.escape(ext) + r'$'
        )
        if os.path.isdir(d):
            for root_dir, _, files in os.walk(d):
                for f in files:
                    if f.startswith('.'):
                        continue
                    full = os.path.join(root_dir, f)
                    is_exact = (f == title)
                    is_copy_variant = bool(copy_pattern.match(f))
                    if not (is_exact or is_copy_variant):
                        continue
                    if totallen:
                        try:
                            if os.path.getsize(full) != totallen:
                                continue
                        except OSError:
                            continue
                    candidates.append(full)

    if not candidates:
        return (
            f"在本地缓存中找不到 {title}\n"
            f"  期望路径模式: {WECHAT_BASE_DIR}/msg/file/YYYY-MM/{title}\n"
            f"  可能原因：从未在 PC/Mac 微信打开过 / 已被清理"
        )

    # 严格 size 过滤（如果 totallen 已知，不匹配的全淘汰）
    if totallen:
        candidates = [c for c in candidates if os.path.getsize(c) == totallen]
        if not candidates:
            return (
                f"在本地缓存中找不到 {title} (期望 size={totallen:,})\n"
                f"  说明：找到了同名文件但 size 都不匹配——可能从未真正下载完整 / 已被清理"
            )

    # 路径绑定策略：有 md5 → cryptographic verify；没 md5 → heuristic +
    # warning。本工具是用户主动通过 MCP 调用，path 只在本地对话显示，所以
    # 没 md5 时不强制 fail-closed。
    cache_root = os.path.join(WECHAT_BASE_DIR, 'msg')
    md5_verified = False
    if expected_md5 and len(expected_md5) == 32:
        # 用 md5 过滤候选——同 md5 = 真同一文件副本。
        md5_match = []
        md5_errors = []
        for c in candidates:
            if not _path_under_root(c, cache_root):
                md5_errors.append(f"{c}: 不在 {cache_root} 下，跳过")
                continue
            actual_md5, err = _md5_file_chunked(c)
            if err:
                md5_errors.append(f"{c}: {err}")
                continue
            if actual_md5 == expected_md5:
                md5_match.append(c)
                break  # 多候选共享同 md5 = 同一文件副本，第一个命中即停
        if not md5_match:
            info = (
                f"⚠️ 候选文件 md5 都不匹配，拒绝返回错文件:\n"
                f"  期望 md5: {expected_md5}\n"
                f"  说明：找到 {len(candidates)} 个同名同 size 的本地文件但 md5 都不对。"
                f"目标文件可能未在 wechat 客户端打开过，或已被清理。"
            )
            if md5_errors:
                info += "\n  校验异常：\n    " + "\n    ".join(md5_errors)
            return info
        candidates = md5_match
        md5_verified = True

    # 没 md5 时多 candidates 仍 fail-closed（避免 silent mtime pick）
    if len(candidates) > 1 and not md5_verified:
        details = []
        for c in candidates:
            try:
                mt = datetime.fromtimestamp(os.path.getmtime(c)).isoformat()
            except OSError:
                mt = '?'
            details.append(f"{c} (mtime={mt})")
        return (
            f"在本地缓存找到 {len(candidates)} 个匹配的副本，无法唯一定位"
            f"（同名同 size 多份，且消息 XML 没含 md5 用于强校验）:\n  "
            + '\n  '.join(details)
            + f"\n请人工 inspect mtime / 上下文区分"
        )

    chosen = candidates[0]
    if not _path_under_root(chosen, cache_root):
        return f"匹配到的路径 {chosen!r} 不在 {cache_root} 下，拒绝返回（可能是 symlink 攻击）"

    binding_note = (
        "✅ md5 校验通过，路径与消息唯一绑定"
        if md5_verified else
        f"⚠️  消息 XML 没含 md5，路径基于 (filename+size) 启发式匹配——"
        f"如果同 chat 缓存里另有同名同 size 的不相关文件，可能返回错副本，请人工验证。"
    )
    return (
        f"找到本地文件:\n"
        f"  路径: {chosen}\n"
        f"  大小: {os.path.getsize(chosen):,} bytes\n"
        f"  扩展名: {fileext or os.path.splitext(title)[1].lstrip('.') or '?'}\n"
        f"  期望大小: {totallen:,} bytes\n"
        f"  {binding_note}"
    )


@mcp.tool()
def decode_record_item(chat_name: str, local_id: int, item_index: int, create_time: int = 0) -> str:
    """获取合并转发聊天记录中某个内嵌文件/图片的本地副本路径。

    使用流程：
    1. 先用 get_chat_history 找到 [聊天记录] xxx (local_id=N, ts=T) 卡片，记下 N 和 T，
       以及展开行里 [item_index] 前缀（0-based）
    2. 用本工具拿本地路径，create_time 传 history 里的 ts 部分
    3. 如果未下载，工具会精确告诉你去 wechat 客户端点击合并卡片里的第几项触发下载

    注意：合并转发里的内嵌文件只有在用户**点击查看**后 wechat 才会下载到本地。
    没点过的 dataitem 用本工具会得到"未下载"提示。

    Args:
        chat_name: 聊天对象的名字、备注名或wxid
        local_id: 合并转发消息（带"[聊天记录]"标记）的 local_id
        item_index: dataitem 在 datalist 里的 0-based 索引（history 输出里的 [N] 前缀）
        create_time: 消息的 unix 时间戳；用于跨分片唯一定位，传 0 时多匹配会报歧义
    """
    try:
        local_id = int(local_id)
        item_index = int(item_index)
        create_time = int(create_time)
    except (TypeError, ValueError):
        return "错误: local_id / item_index / create_time 必须是整数"

    username = resolve_username(chat_name)
    if not username:
        return f"找不到聊天对象: {chat_name}"

    # 多分片扫描 + ambiguity 检测（避免 silent decoding wrong message，参考 decode_file_message）
    shards = _find_msg_tables_for_user(username)
    if not shards:
        return f"找不到 {chat_name} 的消息表"

    matches = []
    for shard in shards:
        if not _is_safe_msg_table_name(shard['table_name']):
            continue
        with closing(sqlite3.connect(shard['db_path'])) as conn:
            if create_time:
                candidate_row = conn.execute(
                    f"SELECT local_type, create_time, message_content, WCDB_CT_message_content "
                    f"FROM [{shard['table_name']}] WHERE local_id=? AND create_time=?",
                    (local_id, create_time)
                ).fetchone()
            else:
                candidate_row = conn.execute(
                    f"SELECT local_type, create_time, message_content, WCDB_CT_message_content "
                    f"FROM [{shard['table_name']}] WHERE local_id=?",
                    (local_id,)
                ).fetchone()
        if candidate_row:
            matches.append((shard['table_name'], candidate_row))
    if not matches:
        if create_time:
            return f"找不到 (local_id={local_id}, create_time={create_time}) 的消息（已扫描 {len(shards)} 个分片）"
        return f"找不到 local_id={local_id} 的消息（已扫描 {len(shards)} 个分片）"
    if len(matches) > 1:
        details = []
        for tn, r in matches:
            ts_str = datetime.fromtimestamp(r[1]).isoformat() if r[1] else '?'
            details.append(f"table={tn[:12]}... create_time={r[1]} ({ts_str})")
        return (
            f"local_id={local_id} 在 {len(matches)} 个分片中都存在，无法唯一定位:\n  "
            + '\n  '.join(details)
            + f"\n请加 create_time 参数：decode_record_item(chat_name, local_id={local_id}, item_index={item_index}, create_time=N)"
        )

    table_name, row = matches[0]
    local_type, _create_time, content, ct_compress = row
    base_type, _ = _split_msg_type(local_type)
    if base_type != 49:
        return (
            f"不是合并转发消息（local_type={local_type}, base_type={base_type}），"
            f"合并转发应为 base_type=49 + appmsg type=19"
        )

    xml_text = _decompress_content(content, ct_compress)
    if not xml_text:
        return "消息 content 为空或无法解码"

    # 复用项目内现有 helper 剥离群聊 sender 前缀，避免自己写启发式
    is_group = username.endswith('@chatroom')
    _, xml_text = _parse_message_content(xml_text, local_type, is_group)

    root = _parse_app_message_outer(xml_text)
    if root is None:
        return "无法解析消息 XML（可能不是合并转发消息）"
    appmsg = root.find('.//appmsg')
    if appmsg is None:
        return "消息中没有 appmsg 段"

    app_type = _parse_int(_collapse_text(appmsg.findtext('type') or ''), 0)
    if app_type != 19:
        return (
            f"不是合并转发消息（appmsg type={app_type}），"
            f"合并转发应为 type=19。请用 decode_file_message 处理外层独立文件"
        )

    record_node = appmsg.find('recorditem')
    if record_node is None or not record_node.text:
        return "消息中没有 recorditem（datalist 还未加载，请在 wechat 中点开此卡片让客户端拉取）"

    inner = _parse_xml_root(record_node.text, max_len=_RECORD_XML_PARSE_MAX_LEN)
    if inner is None:
        return "无法解析 recorditem 内嵌 XML"

    datalist = inner.find('datalist')
    items = list(datalist.findall('dataitem')) if datalist is not None else []
    if not items:
        return "datalist 为空（合并记录还未加载内容）"
    if item_index < 0 or item_index >= len(items):
        return f"item_index={item_index} 超出范围（共 {len(items)} 条 dataitem，0-based）"

    item = items[item_index]
    datatype = (item.get('datatype') or '').strip()
    raw_datatitle = _collapse_text(item.findtext('datatitle') or '')
    # datatitle 来自不可信 XML，sanitize 防 path traversal
    datatitle = _safe_basename(raw_datatitle) if raw_datatitle else ''
    if raw_datatitle and not datatitle:
        return f"该 dataitem 的 datatitle {raw_datatitle!r} 不安全（含绝对路径/分隔符/..），拒绝处理"
    datasize = _parse_int(_collapse_text(item.findtext('datasize') or ''), 0)
    datafmt = _collapse_text(item.findtext('datafmt') or '')
    sourcename = _collapse_text(item.findtext('sourcename') or '')
    # fullmd5 是文件内容唯一标识，用于把候选绑定到这条 record，避免误命中
    # 同 chat 内别条 record 的同名同 size 文件。
    expected_md5 = _collapse_text(item.findtext('fullmd5') or '').lower()

    type_label = _RECORD_DATATYPE_LABEL.get(datatype, f'datatype={datatype}')

    if datatype == '1':
        text_content = _collapse_text(item.findtext('datadesc') or '')
        return (
            f"该 dataitem 是文本，无需下载:\n"
            f"  发送者: {sourcename}\n"
            f"  内容: {text_content}"
        )

    # 仅以下 datatype 在 wechat 缓存里有真本地 binary（图片/语音/视频/文件）；
    # 其他类型如链接/位置/名片/小程序/视频号/嵌套聊天记录等只是 metadata，
    # 没有可下载的本地副本。不在白名单里的 datatype 直接拒绝，避免 wildcard
    # sub='*' 通配命中无关 record 的同名文件。
    subdir_map = _RECORD_BINARY_SUBDIR
    if datatype not in subdir_map:
        return (
            f"该 dataitem 类型 [{type_label}] 没有本地 binary 文件，无需下载\n"
            f"  发送者: {sourcename}\n"
            f"  标题: {datatitle or '(无)'}\n"
            f"  说明：仅 datatype=2/4/5/8（图片/语音/视频/文件）有可下载内容；"
            f"链接/位置/名片/小程序/视频号/嵌套聊天记录等是 metadata-only。"
            f"\n如果你需要这条 dataitem 的 metadata 详情，看 get_chat_history 输出里"
            f"已展开的 [{item_index}] 行内容即可。"
        )

    table_hash = table_name.replace('Msg_', '', 1)
    attach_dir = os.path.join(WECHAT_BASE_DIR, 'msg/attach', table_hash)

    candidates = []
    if os.path.isdir(attach_dir):
        import glob as glob_mod
        sub = subdir_map.get(datatype, '*')
        idx_str = str(item_index)

        # datatype=2 图片走 flat 文件命名 (Img/0_t / Img/0 / Img/0.{ext})，
        # 不像文件类的 F/{idx}/{filename}。
        if datatype == '2':
            flat_patterns = [
                f"{idx_str}_t",
                idx_str,
                f"{idx_str}.*",
                f"{idx_str}_*",
            ]
            for fp in flat_patterns:
                for hit in glob.glob(os.path.join(attach_dir, '*/Rec/*', sub, fp)):
                    if datasize:
                        try:
                            if os.path.getsize(hit) != datasize:
                                continue
                        except OSError:
                            continue
                    if hit not in candidates:
                        candidates.append(hit)

        # 文件 / 视频 / 语音类: F|V|A/{idx}/{filename}
        if datatype != '2' and datatitle:
            escaped_title = glob.escape(datatitle)
            for hit in glob.glob(os.path.join(attach_dir, '*/Rec/*', sub, idx_str, escaped_title)):
                if datasize:
                    try:
                        if os.path.getsize(hit) != datasize:
                            continue
                    except OSError:
                        continue
                if hit not in candidates:
                    candidates.append(hit)

        # size only 兜底：仅在 datatitle 缺失且非 image（image 已上面处理）时启用
        if not candidates and not datatitle and datasize and datatype != '2':
            for hit in glob.glob(os.path.join(attach_dir, '*/Rec/*', sub, idx_str, '*')):
                try:
                    if os.path.getsize(hit) == datasize:
                        candidates.append(hit)
                except OSError:
                    pass

    if not candidates:
        return (
            f"在本地缓存中找不到此 dataitem（很可能未在 wechat 客户端点击查看过）\n"
            f"  消息: {chat_name} 的 local_id={local_id}\n"
            f"  dataitem[{item_index}]: {sourcename}: [{type_label}] {datatitle or '(无标题)'}\n"
            f"  期望大小: {datasize:,} bytes\n"
            f"  期望路径模式: {attach_dir}/YYYY-MM/Rec/*/{subdir_map.get(datatype, '?')}/{item_index}/{datatitle}\n"
            f"  解决方法: 在 wechat 客户端打开此合并记录卡片，点击第 {item_index + 1} 项让客户端下载，再试"
        )

    # 注意：早 ambiguity check（在 md5 filter 之前）已经被删除——它会让有 fullmd5
    # 但多 candidates 的合理 case silent 失败。md5 filter 后再做歧义判断（见下方）。
    # 威胁模型：本工具是用户主动通过 MCP 调用 + path 只在本地显示。
    # 跟 decode_file_message 一致路线：有 md5 强校验，没 md5 fallback 到
    # heuristic + warning（实用 over 严格）。
    cache_root = os.path.join(WECHAT_BASE_DIR, 'msg')
    md5_verified = False
    if expected_md5 and len(expected_md5) == 32:
        md5_match = []
        md5_errors = []
        for c in candidates:
            if not _path_under_root(c, cache_root):
                md5_errors.append(f"{c}: 不在 {cache_root} 下，跳过")
                continue
            actual_md5, err = _md5_file_chunked(c)
            if err:
                md5_errors.append(f"{c}: {err}")
                continue
            if actual_md5 == expected_md5:
                md5_match.append(c)
                break  # 多候选共享同 md5 = 同一文件副本，第一个命中即停
        if not md5_match:
            info = (
                f"⚠️ 候选文件 md5 都不匹配，拒绝返回错文件:\n"
                f"  期望 md5: {expected_md5}\n"
                f"  说明：候选 {len(candidates)} 个，md5 都不对。"
                f"目标 dataitem 可能未在 wechat 客户端点开过，请点击第 {item_index + 1} 项触发下载。"
            )
            if md5_errors:
                info += "\n  校验异常：\n    " + "\n    ".join(md5_errors)
            return info
        candidates = md5_match
        md5_verified = True

    # 没 fullmd5 时多 candidates 仍 fail-closed
    if len(candidates) > 1 and not md5_verified:
        details = []
        for c in candidates:
            try:
                mt = datetime.fromtimestamp(os.path.getmtime(c)).isoformat()
            except OSError:
                mt = '?'
            details.append(f"{c} (mtime={mt})")
        return (
            f"找到 {len(candidates)} 个匹配的本地副本，无法唯一定位"
            f"（同位置同名同 size 多份，且 dataitem XML 没含 fullmd5 用于强校验）:\n  "
            + '\n  '.join(details)
            + f"\n请人工 inspect mtime / 上下文区分"
        )

    chosen = candidates[0]
    if not _path_under_root(chosen, cache_root):
        return f"匹配到的路径 {chosen!r} 不在 {cache_root} 下，拒绝返回（可能是 symlink 攻击）"

    binding_note = (
        "✅ md5 校验通过，路径与 dataitem 唯一绑定"
        if md5_verified else
        f"⚠️  此 dataitem XML 没含 fullmd5，路径基于 (item_index+filename+size) 启发式匹配——"
        f"如果同 chat 内多条合并卡片碰巧含同位置同名同 size 的文件，可能返回别条 record 的副本，请人工验证。"
    )
    return (
        f"找到本地文件:\n"
        f"  路径: {chosen}\n"
        f"  大小: {os.path.getsize(chosen):,} bytes\n"
        f"  期望大小: {datasize:,} bytes\n"
        f"  发送者: {sourcename}\n"
        f"  类型: [{type_label}] {datatitle or '(无标题)'}\n"
        f"  {binding_note}"
    )


@mcp.tool()
def decode_transfer(chat_name: str, local_id: int, create_time: int = 0) -> str:
    """读取微信转账消息（appmsg type=2000）的结构化信息。

    返回方向（发起/收款/退还）、金额、备注、付款人/收款人 wxid、交易号、
    发起/失效时间。仅 1v1 聊天有转账消息（微信不支持群转账）。

    使用流程：先用 get_chat_history 找到 [转账·xxx] 行 (local_id=N, ts=T)，
    把 N 和 T 一起传进来。create_time(ts) 用于跨分片场景下唯一定位。

    Args:
        chat_name: 聊天对象的名字、备注名或wxid
        local_id: 转账消息的 local_id（从 get_chat_history 获取）
        create_time: 消息的 unix 时间戳，从 get_chat_history 输出 ts=N 部分获取。
            用于在 local_id 跨分片冲突时唯一定位；传 0 时若多个分片含同 local_id 会报歧义错误
    """
    try:
        local_id = int(local_id)
        create_time = int(create_time)
    except (TypeError, ValueError):
        return "错误: local_id 和 create_time 必须是整数"

    username = resolve_username(chat_name)
    if not username:
        return f"找不到聊天对象: {chat_name}"

    # 多分片扫描 + ambiguity 检测，跟 decode_file_message 一致
    shards = _find_msg_tables_for_user(username)
    if not shards:
        return f"找不到 {chat_name} 的消息表"

    matches = []
    for shard in shards:
        if not _is_safe_msg_table_name(shard['table_name']):
            continue
        with closing(sqlite3.connect(shard['db_path'])) as conn:
            if create_time:
                candidate_row = conn.execute(
                    f"SELECT local_type, create_time, message_content, WCDB_CT_message_content "
                    f"FROM [{shard['table_name']}] WHERE local_id=? AND create_time=?",
                    (local_id, create_time)
                ).fetchone()
            else:
                candidate_row = conn.execute(
                    f"SELECT local_type, create_time, message_content, WCDB_CT_message_content "
                    f"FROM [{shard['table_name']}] WHERE local_id=?",
                    (local_id,)
                ).fetchone()
        if candidate_row:
            matches.append((shard['db_path'], candidate_row))

    if not matches:
        if create_time:
            return f"找不到 (local_id={local_id}, create_time={create_time}) 的消息（已扫描 {len(shards)} 个分片）"
        return f"找不到 local_id={local_id} 的消息（已扫描 {len(shards)} 个分片）"
    if len(matches) > 1:
        details = []
        for db_p, r in matches:
            ct = r[1]
            ts_str = datetime.fromtimestamp(ct).isoformat() if ct else '?'
            details.append(f"{os.path.basename(db_p)} create_time={ct} ({ts_str})")
        return (
            f"local_id={local_id} 在 {len(matches)} 个分片中都存在，无法唯一定位:\n  "
            + '\n  '.join(details)
            + f"\n请加 create_time 参数：decode_transfer(chat_name, local_id={local_id}, create_time=N)"
        )

    _, row = matches[0]
    local_type, msg_create_time, content, ct_compress = row
    base_type, _ = _split_msg_type(local_type)
    if base_type != 49:
        return (
            f"不是转账消息（local_type={local_type}, base_type={base_type}），"
            f"转账消息应为 base_type=49 + appmsg type=2000"
        )

    xml_text = _decompress_content(content, ct_compress)
    if not xml_text:
        return "消息 content 为空或无法解码"

    is_group = username.endswith('@chatroom')
    _, xml_text = _parse_message_content(xml_text, local_type, is_group)

    root = _parse_app_message_outer(xml_text)
    if root is None:
        return "无法解析消息 XML"
    appmsg = root.find('.//appmsg')
    if appmsg is None:
        return "消息中没有 appmsg 段（不像转账）"

    app_type = _parse_int(_collapse_text(appmsg.findtext('type') or ''), 0)
    if app_type != 2000:
        return (
            f"不是转账消息（appmsg type={app_type}）。"
            f"转账要求 appmsg type=2000；type=6 是文件，type=19 是合并转发，"
            f"请用对应的 decode_file_message / decode_record_item 工具"
        )

    info = _extract_transfer_info(appmsg)
    if info is None:
        return "消息是 type=2000 但缺 <wcpayinfo> 节点（schema 异常）"

    def _fmt_ts(ts_str):
        ts = _parse_int(ts_str, 0)
        if not ts:
            return ''
        try:
            return datetime.fromtimestamp(ts).isoformat()
        except (ValueError, OSError, OverflowError):
            return f'(无效 ts={ts_str})'

    direction = info['paysubtype_label'] or '(未知)'
    raw_paysubtype = info['paysubtype'] or '?'
    title = _collapse_text(appmsg.findtext('title') or '') or '微信转账'
    des = _collapse_text(appmsg.findtext('des') or '')

    lines = [f"转账消息: {title}"]
    if des:
        lines.append(f"  描述: {des}")
    lines.append(f"  方向: {direction} (paysubtype={raw_paysubtype})")
    if info['fee_desc']:
        lines.append(f"  金额: {info['fee_desc']}")
    if info['pay_memo']:
        lines.append(f"  备注: {info['pay_memo']}")
    if info['payer_username']:
        lines.append(f"  付款方 wxid: {info['payer_username']}")
    if info['receiver_username']:
        lines.append(f"  收款方 wxid: {info['receiver_username']}")
    begin_ts = _fmt_ts(info['begin_transfer_time'])
    if begin_ts:
        lines.append(f"  发起时间: {begin_ts}")
    invalid_ts = _fmt_ts(info['invalid_time'])
    if invalid_ts:
        lines.append(f"  失效时间: {invalid_ts}")
    if info['transfer_id']:
        lines.append(f"  转账 ID: {info['transfer_id']}")
    if info['transcation_id']:
        lines.append(f"  支付交易号: {info['transcation_id']}")
    if info['pay_msg_id']:
        lines.append(f"  paymsgid: {info['pay_msg_id']}")
    return "\n".join(lines)


@mcp.tool()
def decode_refer(chat_name: str, local_id: int, create_time: int = 0) -> str:
    """读取微信引用回复消息（appmsg type=57）的结构化信息。

    返回回复正文、被引用消息的发送者/类型/摘要/svrid/createtime。被引用消息的
    type 决定摘要风格：1 文本展开原文，3/34/43/47/48/50 给 [图片]/[语音]/...
    标签，49 嵌套 appmsg 解一层 inner type 给 [链接] xxx。svrid 可用于回查
    原消息（在 history / export_chat 输出里搜）。

    使用流程：先用 get_chat_history 找到 [引用消息] 行 (local_id=N, ts=T)，
    把 N 和 T 一起传进来。create_time(ts) 用于跨分片场景下唯一定位。

    Args:
        chat_name: 聊天对象的名字、备注名或 wxid
        local_id: 引用消息的 local_id（从 get_chat_history 获取）
        create_time: 消息的 unix 时间戳，从 get_chat_history 输出 ts=N 部分获取。
            用于在 local_id 跨分片冲突时唯一定位；传 0 时若多个分片含同 local_id 会报歧义错误
    """
    try:
        local_id = int(local_id)
        create_time = int(create_time)
    except (TypeError, ValueError):
        return "错误: local_id 和 create_time 必须是整数"

    username = resolve_username(chat_name)
    if not username:
        return f"找不到聊天对象: {chat_name}"

    shards = _find_msg_tables_for_user(username)
    if not shards:
        return f"找不到 {chat_name} 的消息表"

    matches = []
    for shard in shards:
        if not _is_safe_msg_table_name(shard['table_name']):
            continue
        with closing(sqlite3.connect(shard['db_path'])) as conn:
            if create_time:
                candidate_row = conn.execute(
                    f"SELECT local_type, create_time, message_content, WCDB_CT_message_content "
                    f"FROM [{shard['table_name']}] WHERE local_id=? AND create_time=?",
                    (local_id, create_time)
                ).fetchone()
            else:
                candidate_row = conn.execute(
                    f"SELECT local_type, create_time, message_content, WCDB_CT_message_content "
                    f"FROM [{shard['table_name']}] WHERE local_id=?",
                    (local_id,)
                ).fetchone()
        if candidate_row:
            matches.append((shard['db_path'], candidate_row))

    if not matches:
        if create_time:
            return f"找不到 (local_id={local_id}, create_time={create_time}) 的消息（已扫描 {len(shards)} 个分片）"
        return f"找不到 local_id={local_id} 的消息（已扫描 {len(shards)} 个分片）"
    if len(matches) > 1:
        details = []
        for db_p, r in matches:
            ct = r[1]
            ts_str = datetime.fromtimestamp(ct).isoformat() if ct else '?'
            details.append(f"{os.path.basename(db_p)} create_time={ct} ({ts_str})")
        return (
            f"local_id={local_id} 在 {len(matches)} 个分片中都存在，无法唯一定位:\n  "
            + '\n  '.join(details)
            + f"\n请加 create_time 参数：decode_refer(chat_name, local_id={local_id}, create_time=N)"
        )

    _, row = matches[0]
    local_type, msg_create_time, content, ct_compress = row
    base_type, _ = _split_msg_type(local_type)
    if base_type != 49:
        return (
            f"不是引用消息（local_type={local_type}, base_type={base_type}），"
            f"引用消息应为 base_type=49 + appmsg type=57"
        )

    xml_text = _decompress_content(content, ct_compress)
    if not xml_text:
        return "消息 content 为空或无法解码"

    is_group = username.endswith('@chatroom')
    _, xml_text = _parse_message_content(xml_text, local_type, is_group)

    root = _parse_app_message_outer(xml_text)
    if root is None:
        return "无法解析消息 XML"
    appmsg = root.find('.//appmsg')
    if appmsg is None:
        return "消息中没有 appmsg 段（不像引用回复）"

    app_type = _parse_int(_collapse_text(appmsg.findtext('type') or ''), 0)
    if app_type != 57:
        return (
            f"不是引用消息（appmsg type={app_type}）。"
            f"引用回复要求 appmsg type=57；type=6 是文件、type=19 是合并转发、"
            f"type=2000 是转账，请用对应的 decode_file_message / decode_record_item / "
            f"decode_transfer 工具"
        )

    info = _extract_refer_info(appmsg)
    if info is None:
        return "消息是 type=57 但缺 <refermsg> 节点（schema 异常）"

    refer_type_label = _REFER_INNER_TYPE_LABEL.get(info['refer_type'], '')
    summary = _summarize_refer_content(info['refer_type'], info['refer_content'])
    sender_label = _resolve_quote_sender_label(
        info['refer_fromusr'], info['refer_displayname'],
        is_group, username, chat_name, get_contact_names()
    )

    def _fmt_ts(ts_str):
        ts = _parse_int(ts_str, 0)
        if not ts:
            return ''
        try:
            return datetime.fromtimestamp(ts).isoformat()
        except (ValueError, OSError, OverflowError):
            return f'(无效 ts={ts_str})'

    lines = [f"引用回复消息: {info['reply_text'] or '(无回复正文)'}"]
    if sender_label:
        lines.append(f"  被引用消息发送者: {sender_label}")
    if info['refer_displayname']:
        lines.append(f"  被引用消息显示名: {info['refer_displayname']}")
    if info['refer_fromusr']:
        lines.append(f"  被引用消息 from: {info['refer_fromusr']}")
    if info['refer_chatusr']:
        lines.append(f"  被引用消息 chatusr (群内发送者 wxid): {info['refer_chatusr']}")
    raw_type = info['refer_type'] or '?'
    type_display = (
        f"{refer_type_label} (refer_type={raw_type})"
        if refer_type_label else f"refer_type={raw_type}"
    )
    lines.append(f"  被引用消息类型: {type_display}")
    lines.append(f"  被引用消息摘要: {summary}")
    refer_ts = _fmt_ts(info['refer_createtime'])
    if refer_ts:
        lines.append(f"  被引用消息创建时间: {refer_ts}")
    if info['refer_svrid']:
        lines.append(f"  被引用消息 server_id: {info['refer_svrid']}")

    return "\n".join(lines)


@mcp.tool()
def decode_location(chat_name: str, local_id: int, create_time: int = 0) -> str:
    """读取微信位置消息 (base_type=48) 的结构化信息。

    返回 POI 名、地址、品类、电话、营业时间、价格档位、城市/区划码、POI id、
    经纬度等。get_chat_history 渲染的 ``[位置·xxx] poiname @ address`` 只挑了
    3 个信号；本工具给所有字段。

    使用流程：先用 get_chat_history 找到 [位置·xxx] 行 (local_id=N, ts=T)，
    把 N 和 T 一起传进来。create_time(ts) 用于跨分片场景下唯一定位。

    Args:
        chat_name: 聊天对象的名字、备注名或 wxid
        local_id: 位置消息的 local_id (从 get_chat_history 获取)
        create_time: 消息的 unix 时间戳，从 get_chat_history 输出 ts=N 部分获取。
            用于在 local_id 跨分片冲突时唯一定位；传 0 时若多个分片含同 local_id 会报歧义错误
    """
    try:
        local_id = int(local_id)
        create_time = int(create_time)
    except (TypeError, ValueError):
        return "错误: local_id 和 create_time 必须是整数"

    username = resolve_username(chat_name)
    if not username:
        return f"找不到聊天对象: {chat_name}"

    shards = _find_msg_tables_for_user(username)
    if not shards:
        return f"找不到 {chat_name} 的消息表"

    matches = []
    for shard in shards:
        if not _is_safe_msg_table_name(shard['table_name']):
            continue
        with closing(sqlite3.connect(shard['db_path'])) as conn:
            if create_time:
                candidate_row = conn.execute(
                    f"SELECT local_type, create_time, message_content, WCDB_CT_message_content "
                    f"FROM [{shard['table_name']}] WHERE local_id=? AND create_time=?",
                    (local_id, create_time)
                ).fetchone()
            else:
                candidate_row = conn.execute(
                    f"SELECT local_type, create_time, message_content, WCDB_CT_message_content "
                    f"FROM [{shard['table_name']}] WHERE local_id=?",
                    (local_id,)
                ).fetchone()
        if candidate_row:
            matches.append((shard['db_path'], candidate_row))

    if not matches:
        if create_time:
            return f"找不到 (local_id={local_id}, create_time={create_time}) 的消息 (已扫描 {len(shards)} 个分片)"
        return f"找不到 local_id={local_id} 的消息 (已扫描 {len(shards)} 个分片)"
    if len(matches) > 1:
        details = []
        for db_p, r in matches:
            ct = r[1]
            ts_str = datetime.fromtimestamp(ct).isoformat() if ct else '?'
            details.append(f"{os.path.basename(db_p)} create_time={ct} ({ts_str})")
        return (
            f"local_id={local_id} 在 {len(matches)} 个分片中都存在，无法唯一定位:\n  "
            + '\n  '.join(details)
            + f"\n请加 create_time 参数：decode_location(chat_name, local_id={local_id}, create_time=N)"
        )

    _, row = matches[0]
    local_type, _msg_create_time, content, ct_compress = row
    base_type, _ = _split_msg_type(local_type)
    if base_type != 48:
        return (
            f"不是位置消息 (local_type={local_type}, base_type={base_type})，"
            f"位置消息应为 base_type=48"
        )

    xml_text = _decompress_content(content, ct_compress)
    if not xml_text:
        return "消息 content 为空或无法解码"

    is_group = username.endswith('@chatroom')
    _, xml_text = _parse_message_content(xml_text, local_type, is_group)

    info = _extract_location_info(xml_text)
    if info is None:
        return "消息是 type=48 但缺 <location> 节点 (schema 异常)"

    lines = ["位置消息:"]
    if info['poiname']:
        lines.append(f"  POI 名: {info['poiname']}")
    if info['label']:
        lines.append(f"  地址: {info['label']}")
    if info['poiCategoryTips']:
        lines.append(f"  品类: {info['poiCategoryTips']}")
    if info['poiPhone']:
        lines.append(f"  电话: {info['poiPhone']}")
    if info['poiBusinessHour']:
        lines.append(f"  营业时间: {info['poiBusinessHour']}")
    if info['poiPriceTips']:
        lines.append(f"  价格档位: {info['poiPriceTips']}")
    if info['cityname']:
        lines.append(f"  城市: {info['cityname']}")
    if info['adcode']:
        lines.append(f"  行政区划码: {info['adcode']}")
    if info['buildingId']:
        lines.append(f"  buildingId: {info['buildingId']}")
    if info['floorName']:
        lines.append(f"  楼层: {info['floorName']}")
    if info['poiid']:
        lines.append(f"  POI id: {info['poiid']}")
    if info['isFromPoiList']:
        lines.append(f"  来源: {info['isFromPoiList']} (true/1=用户从 POI 列表选择，false/0=手扔图钉)")
    if info['lat'] is not None and info['lng'] is not None:
        lines.append(f"  经纬度: ({info['lat']:.6f}, {info['lng']:.6f})  # 微信 x→纬度，y→经度")
    # defensive 字段：本 corpus 实测 0% 非空，但别家账号可能填；仅在非空时展示
    if info['infourl']:
        lines.append(f"  infourl: {info['infourl']}")
    if info['version']:
        lines.append(f"  version: {info['version']}")
    return "\n".join(lines)


@mcp.tool()
def get_chat_images(chat_name: str, limit: int = 20, offset: int = 0, start_time: str = "", end_time: str = "") -> str:
    """列出某个聊天中的图片消息。

    返回图片的时间、local_id、MD5、文件大小等信息。
    可以配合 decode_image 工具解密指定图片。

    Args:
        chat_name: 聊天对象的名字、备注名或wxid
        limit: 返回数量，默认20
        offset: 分页偏移量，默认0
        start_time: 起始时间，支持 YYYY-MM-DD / YYYY-MM-DD HH:MM / YYYY-MM-DD HH:MM:SS
        end_time: 结束时间，支持 YYYY-MM-DD / YYYY-MM-DD HH:MM / YYYY-MM-DD HH:MM:SS
    """
    try:
        _validate_pagination(limit, offset)
        start_ts, end_ts = _parse_time_range(start_time, end_time)
    except ValueError as e:
        return f"错误: {e}"

    username = resolve_username(chat_name)
    if not username:
        return f"找不到聊天对象: {chat_name}"

    names = get_contact_names()
    display_name = names.get(username, username)

    # 同 chat 的消息会分散在多个 message_N.db shard 里 (上限 ~100MB/shard 时滚动到下一个);
    # 单 shard 查找会漏掉其他 shard 的图片。其他工具 (get_chat_history / search_messages /
    # decode_image) 早已用复数版本 scan 全部 shard, 这里对齐一致。
    shards = _find_msg_tables_for_user(username)
    if not shards:
        return f"找不到 {display_name} 的消息记录"

    # 每个 shard 取 limit+offset 张候选, 合并后按 create_time DESC 全局排序, 切片
    # [offset : offset+limit] 出本页。单 shard 至少凑得起本页, 避免某 shard 缺数据
    # 时本页变短。
    candidate_limit = limit + offset
    all_images = []
    for shard in shards:
        shard_images = _image_resolver.list_chat_images(
            shard['db_path'], shard['table_name'], username,
            limit=candidate_limit, start_ts=start_ts, end_ts=end_ts,
        )
        all_images.extend(shard_images)

    if not all_images:
        return f"{display_name} 无图片消息"

    all_images.sort(key=lambda img: img['create_time'], reverse=True)
    paged = all_images[offset:offset + limit]

    lines = []
    for img in paged:
        time_str = datetime.fromtimestamp(img['create_time']).strftime('%Y-%m-%d %H:%M')
        line = f"[{time_str}] local_id={img['local_id']}"
        if img.get('md5'):
            line += f"  MD5={img['md5']}"
        if img.get('size'):
            size_kb = img['size'] / 1024
            line += f"  {size_kb:.0f}KB"
        if not img.get('md5'):
            line += "  (无资源信息)"
        lines.append(line)

    id_tag = f", id={username}" if username != display_name else ""
    header = f"{display_name} 的 {len(lines)} 张图片（offset={offset}, limit={limit}{id_tag}）"
    if start_time or end_time:
        header += f"\n时间范围: {start_time or '最早'} ~ {end_time or '最新'}"
    return header + ":\n\n" + "\n".join(lines) + _pagination_hint(len(lines), limit, offset)


def iter_decryptable_databases(target_db: Optional[str] = None):
    """Yield configured database keys that can be decrypted by the MCP cache."""
    target = (target_db or "").lower()
    seen = set()
    for rel_key in sorted(ALL_KEYS):
        if rel_key in seen:
            continue
        seen.add(rel_key)
        rel_path = rel_key.replace('\\', os.sep).replace('/', os.sep)
        if target and target not in rel_key.lower() and target not in os.path.basename(rel_path).lower():
            continue
        db_path = os.path.join(DB_DIR, rel_path)
        if os.path.exists(db_path):
            yield rel_key, db_path


def predecrypt_databases(target_db: Optional[str] = None) -> dict:
    """Warm the persistent MCP decrypted DB cache before the server receives queries."""
    dbs = list(iter_decryptable_databases(target_db))
    stats = {"total": len(dbs), "success": 0, "failed": 0, "skipped": 0}
    if not dbs:
        print("未找到可预解密的数据库")
        return stats

    print(f"找到 {len(dbs)} 个数据库，开始预解密到 MCP 缓存...")
    for idx, (rel_key, db_path) in enumerate(dbs, 1):
        size_mb = os.path.getsize(db_path) / 1024 / 1024
        print(f"[{idx}/{len(dbs)}] {rel_key} ({size_mb:.1f}MB) ...", end=" ")
        started = time.time()
        try:
            cached_path = _cache.get(rel_key)
            if cached_path:
                elapsed = time.time() - started
                print(f"OK -> {cached_path} ({elapsed:.1f}s)")
                stats["success"] += 1
            else:
                print("SKIP (无密钥或源文件不可用)")
                stats["skipped"] += 1
        except Exception as e:
            print(f"FAILED: {e}")
            stats["failed"] += 1

    print(
        f"预解密完成: {stats['success']} 成功, {stats['failed']} 失败, "
        f"{stats['skipped']} 跳过, 共 {stats['total']} 个"
    )
    return stats


def _build_mcp_http_app():
    """Build the FastMCP HTTP app across FastMCP 2.x API variants."""
    if hasattr(mcp, "http_app"):
        return mcp.http_app()
    if hasattr(mcp, "streamable_http_app"):
        return mcp.streamable_http_app()
    raise RuntimeError("FastMCP does not expose http_app() or streamable_http_app()")


def serve(host: str = "127.0.0.1", port: int = 8765, enforce_version: bool = True):
    """Start the MCP server over streamable-http at /mcp."""
    import uvicorn

    if enforce_version:
        from wechat_version_guard import enforce_or_exit
        enforce_or_exit(_cfg, action="启动 MCP Server")

    app = _build_mcp_http_app()
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    serve()
