import functools
import platform
import subprocess
import sys
import os
import glob
import json
import hashlib
import multiprocessing
import time
from config import load_config, save_config_updates
from Crypto.Cipher import AES


def find_v2_ciphertext(attach_dir):
    v2_magic = b'\x07\x08V2\x08\x07'
    pattern = os.path.join(attach_dir, "*", "*", "Img", "*_t.dat")
    dat_files = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)

    for f in dat_files[:100]:
        try:
            with open(f, 'rb') as fp:
                header = fp.read(31)
            if header[:6] == v2_magic and len(header) >= 31:
                return header[15:31], os.path.basename(f)
        except Exception:
            continue
    return None, None


def find_xor_key(attach_dir):
    v2_magic = b'\x07\x08V2\x08\x07'
    pattern = os.path.join(attach_dir, "*", "*", "Img", "*_t.dat")
    dat_files = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)

    tail_counts = {}
    for f in dat_files[:32]:
        try:
            sz = os.path.getsize(f)
            with open(f, 'rb') as fp:
                head = fp.read(6)
                fp.seek(sz - 2)
                tail = fp.read(2)
            if head == v2_magic and len(tail) == 2:
                key = (tail[0], tail[1])
                tail_counts[key] = tail_counts.get(key, 0) + 1
        except Exception:
            continue

    if not tail_counts:
        return None

    most_common = max(tail_counts, key=tail_counts.get)
    x, y = most_common
    xor_key = x ^ 0xFF
    if (y ^ 0xD9) == xor_key:
        return xor_key
    return None


def try_key(key_bytes, ciphertext):
    try:
        cipher = AES.new(key_bytes, AES.MODE_ECB)
        dec = cipher.decrypt(ciphertext)
        if dec[:3] == b'\xFF\xD8\xFF': return 'JPEG'
        if dec[:4] == b'\x89PNG': return 'PNG'
        if dec[:4] == b'RIFF': return 'WEBP'
        if dec[:4] == b'wxgf': return 'WXGF'
        if dec[:3] == b'GIF': return 'GIF'
    except Exception:
        pass
    return None


def _brute_worker(start_i, end_i, xor_key, bin_suffix, base_wxid_bytes, ciphertext_16, result_queue):
    for i in range(start_i, end_i):
        uin = (i << 8) | xor_key
        uin_bytes = str(uin).encode('ascii')
        
        if hashlib.md5(uin_bytes).digest()[:2] == bin_suffix:
            h_aes = hashlib.md5(uin_bytes + base_wxid_bytes).hexdigest()
            aes_key_16 = h_aes[:16].encode('ascii')
            
            if try_key(aes_key_16, ciphertext_16):
                result_queue.put((uin, aes_key_16.decode('ascii')))
                return


def find_image_key_offline(cfg):
    print("\n" + "=" * 60)
    print("  尝试提取图片 AES 密钥")
    print("=" * 60)

    db_dir = cfg.get("db_dir", "")
    if not db_dir:
        print("未配置 db_dir")
        return
        
    base_dir = os.path.dirname(db_dir)
    attach_dir = os.path.join(base_dir, 'msg', 'attach')
    
    folder = os.path.basename(base_dir)
    base_wxid, suffix = "", ""
    if '_' in folder:
        parts = folder.rsplit('_', 1)
        if len(parts) == 2 and len(parts[1]) == 4:
            base_wxid, suffix = parts
            
    if not base_wxid or not suffix:
        print(f"[!] 目录名不符合 wxid_..._suffix 格式: {folder}，跳过爆破")
        return
        
    print(f"[*] 解析到 wxid={base_wxid}, suffix={suffix}")
    
    xor_key = find_xor_key(attach_dir)
    if xor_key is None:
        print("[!] 找不到足够的 _t.dat 文件推导 XOR key，跳过爆破")
        print("    请先在微信中查看 2-3 张图片，让缩略图缓存到本地后再重试。")
        return
    print(f"[*] 找到 XOR key: 0x{xor_key:02x}")

    ciphertext, ct_file = find_v2_ciphertext(attach_dir)
    if not ciphertext:
        print("[!] 找不到 V2 加密的图片文件，跳过爆破")
        print("    请先在微信中查看 2-3 张图片，让缩略图缓存到本地后再重试。")
        return
        
    print(f"[*] 启动多进程 UIN 空间爆破...")
    t0 = time.time()
    
    bin_suffix = bytes.fromhex(suffix)
    base_wxid_bytes = base_wxid.encode('ascii')
    
    cpu_count = multiprocessing.cpu_count()
    total = 1 << 24
    chunk = total // cpu_count
    
    result_queue = multiprocessing.Queue()
    processes = []
    
    for i in range(cpu_count):
        start, end = i * chunk, (i + 1) * chunk if i != cpu_count - 1 else total
        p = multiprocessing.Process(
            target=_brute_worker,
            args=(start, end, xor_key, bin_suffix, base_wxid_bytes, ciphertext, result_queue)
        )
        p.start()
        processes.append(p)
    
    found = None
    try:
        while any(p.is_alive() for p in processes):
            if not result_queue.empty():
                found = result_queue.get()
                break
            time.sleep(0.1)
    finally:
        for p in processes:
            p.terminate()
        for p in processes:
            p.join(timeout=1)

    elapsed = time.time() - t0
    if found:
        print(f"[+] 爆破成功! UIN={found[0]}, 耗时={elapsed:.1f}s")
        aes_key = found[1]
        print(f"    image_aes_key = {aes_key}")
        
        cfg['image_aes_key'] = aes_key
        cfg['image_xor_key'] = xor_key
        config_file = save_config_updates({
            "image_aes_key": aes_key,
            "image_xor_key": xor_key,
        })
        print(f"[+] 已保存到 {config_file}")
    else:
        print(f"[-] 未能在 UIN 空间找到有效密钥 (耗时={elapsed:.1f}s)")
        print("    可能原因: 目录名被重命名过，或者不是标准账号目录。")


@functools.lru_cache(maxsize=1)
def _load_impl():
    system = platform.system().lower()
    if system == "windows":
        import find_all_keys_windows as impl
        return impl
    if system == "linux":
        import find_all_keys_linux as impl
        return impl
    if system == "darwin":
        return None  # macOS 通过 _run_macos_scanner() 处理
    raise RuntimeError(
        f"当前平台暂不支持通过 find_all_keys.py 提取内存数据库密钥: {platform.system()}"
    )


def get_pids():
    impl = _load_impl()
    if impl is None:
        raise RuntimeError("macOS 使用 C 版扫描器，不提供 get_pids()")
    return impl.get_pids()


def _generate_db_salts(db_dir):
    """扫描 db_dir 下的 .db 文件，读取前 16 字节作为 salt，生成临时 JSON。

    Python 以当前用户身份运行，可以直接访问 ~/Library/Containers/...，
    不需要 Full Disk Access。这避免了 sudo 运行 C 扫描器时的 TCC 限制。
    """
    import tempfile

    entries = []
    for root, _dirs, files in os.walk(db_dir):
        for fname in files:
            if not fname.endswith(".db"):
                continue
            fpath = os.path.join(root, fname)
            try:
                with open(fpath, "rb") as f:
                    header = f.read(16)
                if len(header) < 16:
                    continue
                # 跳过未加密的数据库
                if header[:15] == b"SQLite format 3":
                    continue
                salt_hex = header.hex()
                # 提取相对路径 (从 db_storage/ 开始)
                rel = os.path.relpath(fpath, db_dir)
                entries.append({"name": rel, "salt": salt_hex})
            except OSError:
                continue

    if not entries:
        return None

    # 写入临时文件
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix="db_salts_", delete=False
    )
    json.dump(entries, tmp, indent=2)
    tmp.close()
    print(f"[+] 预计算 {len(entries)} 个数据库 salt -> {tmp.name}")
    return tmp.name


def _run_macos_scanner(cfg):
    """在 macOS 上自动编排 C 扫描器：预计算 db-salts → 调用 sudo scanner → 验证输出。"""
    project_dir = os.path.dirname(os.path.abspath(__file__))
    scanner_bin = os.path.join(project_dir, "find_all_keys_macos")
    keys_file = cfg["keys_file"]

    # 1. 检查 C 扫描器二进制是否存在
    if not os.path.isfile(scanner_bin):
        src = os.path.join(project_dir, "find_all_keys_macos.c")
        if os.path.isfile(src):
            print("[*] 编译 C 版扫描器...")
            ret = subprocess.run(
                ["cc", "-O2", "-o", scanner_bin, src, "-framework", "Foundation"],
                cwd=project_dir,
            )
            if ret.returncode != 0:
                raise RuntimeError(
                    "C 扫描器编译失败。请手动执行:\n"
                    f"  cc -O2 -o {scanner_bin} {src} -framework Foundation"
                )
            print("[+] 编译成功")
        else:
            raise RuntimeError(f"找不到 C 扫描器: {scanner_bin}")

    # 2. 预计算 db-salts（Python 用户身份可访问 Container）
    db_dir = cfg.get("db_dir", "")
    salts_file = _generate_db_salts(db_dir) if db_dir else None
    if not salts_file:
        raise RuntimeError(
            f"无法从 db_dir 读取任何加密数据库的 salt。\n"
            f"  db_dir = {db_dir}\n"
            "  请确认微信已登录且数据库已生成。"
        )

    # 3. 构建命令：sudo scanner --db-salts <path> --output <abs_path>
    cmd = [
        "sudo", scanner_bin,
        "--db-salts", salts_file,
        "--output", os.path.abspath(keys_file),
    ]
    print()
    print(f"[*] 调用 C 扫描器提取数据库密钥...")
    print(f"    命令: {' '.join(cmd)}")
    print(f"    (需要 sudo 密码)")
    print()

    try:
        result = subprocess.run(cmd, cwd=project_dir)
    finally:
        # 清理临时 salts 文件
        try:
            os.unlink(salts_file)
        except OSError:
            pass

    if result.returncode != 0:
        raise RuntimeError(
            f"C 扫描器退出码 {result.returncode}。\n"
            "  常见原因:\n"
            "  - 退出码 1: 微信未运行或未用 ad-hoc 签名\n"
            "  - 退出码 2: 版本门禁不通过\n"
            "  - 退出码 3: 无法写入密钥文件\n"
            "  - 退出码 4: 找到密钥但无法匹配到数据库\n"
            "  请检查上方输出的详细错误信息。"
        )

    # 4. 验证输出文件
    if not os.path.exists(keys_file):
        raise RuntimeError("C 扫描器执行完成但未生成密钥文件")

    with open(keys_file, encoding="utf-8") as f:
        keys = json.load(f)
    from key_utils import strip_key_metadata
    valid_keys = strip_key_metadata(keys)
    if not valid_keys:
        raise RuntimeError(
            "C 扫描器生成的密钥文件为空（0 个有效密钥）。\n"
            "  请确认微信已完全启动并加载了聊天列表。"
        )

    print(f"[+] 成功提取 {len(valid_keys)} 个数据库密钥 -> {keys_file}")


def main():
    cfg = load_config()

    from wechat_version_guard import enforce_risky_action_or_exit
    enforce_risky_action_or_exit(cfg, action="获取微信密钥")

    find_image_key_offline(cfg)

    impl = _load_impl()
    if impl is None:
        # macOS: 自动编排 C 扫描器
        _run_macos_scanner(cfg)
    else:
        impl.main()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    try:
        main()
    except RuntimeError as exc:
        print(f"\n[ERROR] {exc}")
        sys.exit(1)
