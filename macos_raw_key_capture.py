"""Verified raw-key capture for supported Apple Silicon WeChat builds.

WeChat 4.1.12 no longer leaves the SQLCipher key in the ASCII
``x'<key><salt>'`` form consumed by the legacy Mach VM scanner.  This module
locates the build-specific raw-key call in ``wechat.dylib``, captures only its
32-byte argument, and persists a candidate only after the existing SQLCipher
page-1 HMAC check authenticates it against a local database.

The locator is deliberately fail-closed: both the complete WeChat build and a
unique instruction signature must match before Frida attaches to the process.
"""

from __future__ import annotations

import json
import mmap
import os
import struct
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from key_scan_common import collect_db_files, verify_enc_key


_CPU_TYPE_ARM64 = 0x0100000C
_LC_SEGMENT_64 = 0x19
_MH_MAGIC_64 = 0xFEEDFACF

# Tencent's current public macOS release is displayed as 4.1.12 while its
# release feed identifies the artifact as 4.1.12.29, build 269341.  Info.plist
# variants seen publicly use either short-version spelling, so the build is the
# mandatory discriminator.
SUPPORTED_BUILDS = {
    ("4.1.12", "269341"),
    ("4.1.12.29", "269341"),
}

# Instruction sequence immediately before the raw-key call in the supported
# 4.1.12 arm64 wechat.dylib.  The following BL is decoded instead of pinning an
# ASLR address.
_KEY_CALL_SIGNATURES = (
    bytes.fromhex(
        "69 7c 40 93"
        " aa 7c 40 93"
        " 08 7d 40 93"
        " e8 03 00 f9"
        " 40 00 80 52"
        " e1 03 02 aa"
        " e2 03 09 aa"
        " e3 03 04 aa"
        " e4 03 0a aa"
        " a5 00 80 52"
    ),
)


class RawKeyCaptureError(RuntimeError):
    """An actionable, non-secret raw-key capture failure."""


@dataclass(frozen=True)
class DatabaseEntry:
    relative_path: str
    absolute_path: str
    size: int
    salt: str
    page1: bytes
    account_index: int


@dataclass(frozen=True)
class CaptureResult:
    ready: bool
    candidate_count: int
    matched_paths: frozenset[str]
    complete_account_index: int | None

    @property
    def complete(self) -> bool:
        return self.complete_account_index is not None


def supports_build(short_version: object, build_version: object) -> bool:
    return (str(short_version or ""), str(build_version or "")) in SUPPORTED_BUILDS


def wechat_dylib_path(app_path: str | Path) -> Path:
    app = Path(app_path)
    candidates = (
        app / "Contents" / "Resources" / "wechat.dylib",
        app / "Contents" / "Frameworks" / "wechat.dylib",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise RawKeyCaptureError("当前微信缺少可识别的 wechat.dylib 加密模块")


def _arm64_slice(mm: mmap.mmap) -> tuple[int, int]:
    if len(mm) < 32:
        raise RawKeyCaptureError("wechat.dylib 文件不完整")
    magic = mm[:4]
    if magic == b"\xcf\xfa\xed\xfe":
        if struct.unpack_from("<I", mm, 4)[0] != _CPU_TYPE_ARM64:
            raise RawKeyCaptureError("微信加密模块不是 Apple Silicon 版本")
        return 0, len(mm)
    if magic not in (b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"):
        raise RawKeyCaptureError("无法识别 wechat.dylib 的 Mach-O 格式")

    is_64 = magic == b"\xca\xfe\xba\xbf"
    count = struct.unpack_from(">I", mm, 4)[0]
    entry_size = 32 if is_64 else 20
    cursor = 8
    for _ in range(count):
        if cursor + entry_size > len(mm):
            break
        cpu_type = struct.unpack_from(">I", mm, cursor)[0]
        offset, size = struct.unpack_from(">QQ" if is_64 else ">II", mm, cursor + 8)
        if cpu_type == _CPU_TYPE_ARM64:
            if offset + size > len(mm):
                raise RawKeyCaptureError("wechat.dylib 的 arm64 切片已损坏")
            return int(offset), int(size)
        cursor += entry_size
    raise RawKeyCaptureError("wechat.dylib 中没有 arm64 切片")


def _segments(
    mm: mmap.mmap, slice_offset: int, slice_size: int
) -> list[tuple[int, int, int, int]]:
    slice_end = slice_offset + slice_size
    if slice_offset + 32 > slice_end:
        raise RawKeyCaptureError("wechat.dylib 的 arm64 头部已损坏")
    if struct.unpack_from("<I", mm, slice_offset)[0] != _MH_MAGIC_64:
        raise RawKeyCaptureError("仅支持 64 位 Apple Silicon 微信")
    command_count = struct.unpack_from("<I", mm, slice_offset + 16)[0]
    cursor = slice_offset + 32
    result = []
    for _ in range(command_count):
        if cursor + 8 > slice_end:
            raise RawKeyCaptureError("wechat.dylib 的加载命令已损坏")
        command, command_size = struct.unpack_from("<II", mm, cursor)
        if command_size < 8 or cursor + command_size > slice_end:
            raise RawKeyCaptureError("wechat.dylib 的加载命令已损坏")
        if command == _LC_SEGMENT_64 and command_size >= 72:
            result.append(
                (
                    struct.unpack_from("<Q", mm, cursor + 40)[0],
                    struct.unpack_from("<Q", mm, cursor + 48)[0],
                    struct.unpack_from("<Q", mm, cursor + 24)[0],
                    struct.unpack_from("<Q", mm, cursor + 32)[0],
                )
            )
        cursor += command_size
    return result


def _fileoff_to_vmaddr(fileoff: int, segments: Iterable[tuple[int, int, int, int]]) -> int:
    for segment_fileoff, file_size, vm_address, _vm_size in segments:
        if segment_fileoff <= fileoff < segment_fileoff + file_size:
            return vm_address + fileoff - segment_fileoff
    raise RawKeyCaptureError("无法把密钥调用位置映射到运行时地址")


def _decode_arm64_bl(instruction: int, pc: int) -> int:
    if instruction & 0xFC000000 != 0x94000000:
        raise RawKeyCaptureError("密钥调用特征之后不是 arm64 BL 指令")
    immediate = instruction & 0x03FFFFFF
    if immediate & (1 << 25):
        immediate -= 1 << 26
    return pc + immediate * 4


def locate_key_hook(dylib_path: str | Path) -> int:
    path = Path(dylib_path)
    if not path.is_file():
        raise RawKeyCaptureError(f"未找到微信加密模块: {path}")
    with path.open("rb") as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        slice_offset, slice_size = _arm64_slice(mm)
        segments = _segments(mm, slice_offset, slice_size)
        slice_end = slice_offset + slice_size
        targets: set[int] = set()
        for signature in _KEY_CALL_SIGNATURES:
            cursor = slice_offset
            while True:
                absolute = mm.find(signature, cursor, slice_end)
                if absolute < 0:
                    break
                call_fileoff = absolute - slice_offset + len(signature)
                if slice_offset + call_fileoff + 4 > slice_end:
                    cursor = absolute + 1
                    continue
                instruction = struct.unpack_from("<I", mm, slice_offset + call_fileoff)[0]
                if instruction & 0xFC000000 == 0x94000000:
                    call_vmaddr = _fileoff_to_vmaddr(call_fileoff, segments)
                    targets.add(_decode_arm64_bl(instruction, call_vmaddr))
                cursor = absolute + 1
    if len(targets) == 1:
        return targets.pop()
    if not targets:
        raise RawKeyCaptureError("当前微信构建的数据库密钥函数尚未识别")
    raise RawKeyCaptureError("检测到多个数据库密钥函数候选，已停止以避免误操作")


def collect_account_databases(account_dirs: Sequence[Path]) -> list[DatabaseEntry]:
    entries: list[DatabaseEntry] = []
    multiple = len(account_dirs) > 1
    for index, account in enumerate(account_dirs):
        try:
            db_files, _salt_map = collect_db_files(str(account))
        except OSError as exc:
            raise RawKeyCaptureError("无法读取用于密钥验证的微信数据库") from exc
        prefix = f"__account_{index:03d}__/" if multiple else ""
        entries.extend(
            DatabaseEntry(prefix + rel.replace("\\", "/"), path, size, salt, page1, index)
            for rel, path, size, salt, page1 in db_files
        )
    return entries


def _account_complete(entries: Sequence[DatabaseEntry], matched: set[str]) -> int | None:
    multiple = any(entry.relative_path.startswith("__account_") for entry in entries)
    for index in sorted({entry.account_index for entry in entries}):
        account_matched = {
            entry.relative_path
            for entry in entries
            if entry.account_index == index and entry.relative_path in matched
        }
        prefix = f"__account_{index:03d}__/" if multiple else ""
        contact = prefix + "contact/contact.db"
        has_message = any(
            path.startswith(prefix + "message/message_")
            and path.endswith(".db")
            and path[len(prefix + "message/message_") : -3].isdigit()
            for path in account_matched
        )
        if contact in account_matched and has_message:
            return index
    return None


def _atomic_write_verified(output_path: Path, records: dict[str, object]) -> None:
    output_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(output_path.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(records, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, output_path)
        os.chmod(output_path, 0o600)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _save_verified_candidate(
    entries: Sequence[DatabaseEntry],
    candidate: bytes,
    output_path: Path,
) -> set[str]:
    if len(candidate) != 32:
        return set()
    try:
        existing = json.loads(output_path.read_text(encoding="utf-8"))
        records = existing if isinstance(existing, dict) else {}
    except (OSError, json.JSONDecodeError):
        records = {}
    matched: set[str] = set()
    for entry in entries:
        if not verify_enc_key(candidate, entry.page1):
            continue
        matched.add(entry.relative_path)
        records[entry.relative_path] = {
            "enc_key": candidate.hex(),
            "salt": entry.salt,
            "size_mb": round(entry.size / 1024 / 1024, 1),
        }
    if matched:
        _atomic_write_verified(output_path, records)
    return matched


_FRIDA_AGENT = r"""
const targetPath = __TARGET_PATH__;
const hookOffset = ptr('__HOOK_OFFSET__');
let installed = false;

Process.attachModuleObserver({
  onAdded(module) {
    if (installed || module.path !== targetPath)
      return;
    Interceptor.attach(module.base.add(hookOffset), {
      onEnter(args) {
        try {
          if (args[2].toUInt32() !== 32 || args[1].isNull())
            return;
          send({ type: 'candidate' }, args[1].readByteArray(32));
        } catch (_) {
        }
      }
    });
    installed = true;
    send({ type: 'ready' });
  }
});
"""


def capture_verified_keys(
    *,
    pid: int,
    dylib_path: str | Path,
    hook_offset: int,
    account_dirs: Sequence[Path],
    output_path: str | Path,
    timeout: float = 120.0,
) -> CaptureResult:
    try:
        import frida
    except ImportError as exc:
        raise RawKeyCaptureError("安装环境缺少 4.1.12 密钥捕获组件") from exc

    entries = collect_account_databases(account_dirs)
    if not entries:
        raise RawKeyCaptureError("没有找到可验证的微信数据库")
    output = Path(output_path)
    source = (
        _FRIDA_AGENT.replace("__TARGET_PATH__", json.dumps(str(Path(dylib_path).resolve())))
        .replace("__HOOK_OFFSET__", hex(hook_offset))
    )
    finished = threading.Event()
    ready = threading.Event()
    seen: set[bytes] = set()
    matched: set[str] = set()
    state: dict[str, object] = {"error": None, "complete_account": None}

    def handle_message(message, data) -> None:
        if message.get("type") == "error":
            state["error"] = message.get("description") or "运行时密钥捕获失败"
            finished.set()
            return
        payload = message.get("payload") or {}
        if payload.get("type") == "ready":
            ready.set()
            return
        if payload.get("type") != "candidate" or data is None:
            return
        candidate = bytes(data)
        if len(candidate) != 32 or candidate in seen:
            return
        seen.add(candidate)
        try:
            matched.update(_save_verified_candidate(entries, candidate, output))
            complete_account = _account_complete(entries, matched)
            if complete_account is not None:
                state["complete_account"] = complete_account
                finished.set()
        except Exception:
            state["error"] = "已验证密钥无法安全写入本地文件"
            finished.set()

    try:
        device = frida.get_local_device()
        session = device.attach(pid)
    except Exception as exc:
        raise RawKeyCaptureError("无法安全附加到微信进程") from exc

    script = None
    try:
        try:
            script = session.create_script(source)
            script.on("message", handle_message)
            script.load()
            finished.wait(timeout)
        except Exception as exc:
            raise RawKeyCaptureError("运行时密钥监听启动失败") from exc
    finally:
        if script is not None:
            try:
                script.unload()
            except Exception:
                pass
        try:
            session.detach()
        except Exception:
            pass

    if state["error"]:
        raise RawKeyCaptureError(str(state["error"]))
    return CaptureResult(
        ready=ready.is_set(),
        candidate_count=len(seen),
        matched_paths=frozenset(matched),
        complete_account_index=state["complete_account"],
    )
