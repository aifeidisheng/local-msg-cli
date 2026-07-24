/*
 * find_all_keys_macos.c - macOS WeChat memory key scanner
 *
 * Scans WeChat process memory for SQLCipher encryption keys in the
 * x'<key_hex><salt_hex>' format used by WeChat 4.x on macOS.
 *
 * Prerequisites:
 *   - WeChat must be ad-hoc signed (or SIP disabled)
 *   - Must run as root (sudo)
 *
 * Build:
 *   cc -O2 -o find_all_keys_macos find_all_keys_macos.c -framework Foundation
 *
 * Usage:
 *   sudo ./find_all_keys_macos [--output /path/to/all_keys.json]
 *       [--home /Users/name] [--owner-uid uid --owner-gid gid] [pid]
 *   If pid is omitted, automatically finds WeChat PID. The installed
 *   management CLI supplies an output path in its private data directory.
 *
 * Output: JSON compatible with decrypt_db.py. Defaults to ./all_keys.json.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <dirent.h>
#include <ftw.h>
#include <pwd.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <mach/mach.h>
#include <mach/mach_vm.h>
#include <CommonCrypto/CommonCryptor.h>
#include <CommonCrypto/CommonHMAC.h>
#include <CommonCrypto/CommonKeyDerivation.h>
#include "wechat_risk_guard_macos.h"

#define MAX_KEYS 256
#define KEY_SIZE 32
#define SALT_SIZE 16
#define DB_PAGE_SIZE 4096
#define HMAC_SIZE 64
#define RESERVE_SIZE 80
#define HEX_PATTERN_LEN 96  /* 64 hex (key) + 32 hex (salt) */
#define CHUNK_SIZE (2 * 1024 * 1024)

typedef struct {
    char key_hex[65];
    char salt_hex[33];
    char full_pragma[100];
} key_entry_t;

typedef struct {
    char salt_hex[33];
    char name[256];
    unsigned char page1[DB_PAGE_SIZE];
    int has_page1;
} db_entry_t;

/* Forward declarations */
static int read_db_page1(const char *path, unsigned char *page1_out);
static int verify_key_for_db(const char *key_hex, const db_entry_t *db);
static int hex_to_bytes(const char *hex, unsigned char *out, size_t out_len);

/* nftw callback state for collecting DB files */
#define MAX_DBS 256
static db_entry_t g_dbs[MAX_DBS];
static int g_db_count = 0;
static int nftw_collect_db(const char *fpath, const struct stat *sb,
                           int typeflag, struct FTW *ftwbuf) {
    (void)sb; (void)ftwbuf;
    if (typeflag != FTW_F) return 0;
    size_t len = strlen(fpath);
    if (len < 3 || strcmp(fpath + len - 3, ".db") != 0) return 0;
    if (g_db_count >= MAX_DBS) return 0;

    if (read_db_page1(fpath, g_dbs[g_db_count].page1) != 0) return 0;
    for (int i = 0; i < SALT_SIZE; i++)
        sprintf(g_dbs[g_db_count].salt_hex + i * 2, "%02x", g_dbs[g_db_count].page1[i]);
    g_dbs[g_db_count].salt_hex[32] = '\0';
    g_dbs[g_db_count].has_page1 = 1;
    /* Extract relative path from db_storage/ */
    const char *rel = strstr(fpath, "db_storage/");
    if (rel) rel += strlen("db_storage/");
    else {
        rel = strrchr(fpath, '/');
        rel = rel ? rel + 1 : fpath;
    }
    strncpy(g_dbs[g_db_count].name, rel, 255);
    g_dbs[g_db_count].name[255] = '\0';
    printf("  %s: salt=%s\n", g_dbs[g_db_count].name, g_dbs[g_db_count].salt_hex);
    g_db_count++;
    return 0;
}

/* Load pre-discovered DB salts from a JSON file produced by the Python installer.
 * Expected format: [{"name": "relative/path.db", "salt": "hex32", "page1": "hex8192"}, ...]
 * This eliminates the need for the elevated scanner to have Full Disk Access. */
static int load_db_salts_from_file(const char *path) {
    FILE *f = fopen(path, "r");
    if (!f) {
        fprintf(stderr, "Cannot open --db-salts file: %s\n", path);
        return -1;
    }
    /* Simple JSON array parser - entries are small and well-structured */
    fseek(f, 0, SEEK_END);
    long fsize = ftell(f);
    if (fsize <= 0 || fsize > 4 * 1024 * 1024) { fclose(f); return -1; }
    fseek(f, 0, SEEK_SET);
    char *buf = malloc(fsize + 1);
    if (!buf) { fclose(f); return -1; }
    if ((long)fread(buf, 1, fsize, f) != fsize) { free(buf); fclose(f); return -1; }
    buf[fsize] = '\0';
    fclose(f);

    /* Parse entries: find "name": "..." and "salt": "..." pairs */
    const char *p = buf;
    while ((p = strstr(p, "\"name\"")) != NULL && g_db_count < MAX_DBS) {
        /* Find name value */
        const char *colon = strchr(p + 6, ':');
        if (!colon) break;
        const char *q1 = strchr(colon + 1, '"');
        if (!q1) break;
        const char *q2 = strchr(q1 + 1, '"');
        if (!q2) break;
        size_t nlen = q2 - q1 - 1;
        if (nlen >= 256) nlen = 255;
        memcpy(g_dbs[g_db_count].name, q1 + 1, nlen);
        g_dbs[g_db_count].name[nlen] = '\0';

        /* Find salt value after name */
        const char *sp = strstr(q2, "\"salt\"");
        if (!sp) break;
        const char *sc = strchr(sp + 6, ':');
        if (!sc) break;
        const char *s1 = strchr(sc + 1, '"');
        if (!s1) break;
        const char *s2 = strchr(s1 + 1, '"');
        if (!s2 || (s2 - s1 - 1) != 32) { p = q2 + 1; continue; }
        memcpy(g_dbs[g_db_count].salt_hex, s1 + 1, 32);
        g_dbs[g_db_count].salt_hex[32] = '\0';

        /* Secure installer mode requires the encrypted page itself so the
         * memory candidate can be authenticated, not merely salt-matched. */
        const char *entry_end = strchr(s2, '}');
        const char *pp = strstr(s2, "\"page1\"");
        if (!entry_end || !pp || pp > entry_end) {
            p = q2 + 1;
            continue;
        }
        const char *pc = strchr(pp + 7, ':');
        const char *p1 = pc ? strchr(pc + 1, '"') : NULL;
        const char *p2 = p1 ? strchr(p1 + 1, '"') : NULL;
        if (!p2 || p2 > entry_end || (p2 - p1 - 1) != DB_PAGE_SIZE * 2 ||
            hex_to_bytes(p1 + 1, g_dbs[g_db_count].page1, DB_PAGE_SIZE) != 0) {
            p = q2 + 1;
            continue;
        }
        g_dbs[g_db_count].has_page1 = 1;

        printf("  %s: salt=%s (pre-discovered)\n",
               g_dbs[g_db_count].name, g_dbs[g_db_count].salt_hex);
        g_db_count++;
        p = s2 + 1;
    }
    free(buf);
    return g_db_count > 0 ? 0 : -1;
}

static int is_hex_char(unsigned char c) {
    return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
}

static pid_t find_wechat_pid(void) {
    FILE *fp = popen("pgrep -x WeChat", "r");
    if (!fp) return -1;
    char buf[64];
    pid_t pid = -1;
    if (fgets(buf, sizeof(buf), fp))
        pid = atoi(buf);
    pclose(fp);
    return pid;
}

/* Read and retain DB page 1 for cryptographic candidate validation. */
static int read_db_page1(const char *path, unsigned char *page1_out) {
    FILE *f = fopen(path, "rb");
    if (!f) return -1;
    if (fread(page1_out, 1, DB_PAGE_SIZE, f) != DB_PAGE_SIZE) { fclose(f); return -1; }
    fclose(f);
    /* Check if unencrypted */
    if (memcmp(page1_out, "SQLite format 3", 15) == 0) return -1;
    return 0;
}

static int hex_to_bytes(const char *hex, unsigned char *out, size_t out_len) {
    for (size_t i = 0; i < out_len; i++) {
        unsigned char hi = hex[i * 2], lo = hex[i * 2 + 1];
        int h = (hi >= '0' && hi <= '9') ? hi - '0' :
                (hi >= 'a' && hi <= 'f') ? hi - 'a' + 10 :
                (hi >= 'A' && hi <= 'F') ? hi - 'A' + 10 : -1;
        int l = (lo >= '0' && lo <= '9') ? lo - '0' :
                (lo >= 'a' && lo <= 'f') ? lo - 'a' + 10 :
                (lo >= 'A' && lo <= 'F') ? lo - 'A' + 10 : -1;
        if (h < 0 || l < 0) return -1;
        out[i] = (unsigned char)((h << 4) | l);
    }
    return 0;
}

/* Verify the SQLCipher page-1 HMAC used by the Python decryptor. */
static int verify_key_for_db(const char *key_hex, const db_entry_t *db) {
    if (!db->has_page1) return 0;
    unsigned char enc_key[KEY_SIZE];
    unsigned char mac_salt[SALT_SIZE];
    unsigned char mac_key[KEY_SIZE];
    unsigned char digest[HMAC_SIZE];
    unsigned char hmac_data[(DB_PAGE_SIZE - RESERVE_SIZE) + 4];
    if (hex_to_bytes(key_hex, enc_key, KEY_SIZE) != 0) return 0;
    for (int i = 0; i < SALT_SIZE; i++)
        mac_salt[i] = (unsigned char)(db->page1[i] ^ 0x3a);
    if (CCKeyDerivationPBKDF(kCCPBKDF2, (const char *)enc_key, KEY_SIZE,
                             mac_salt, SALT_SIZE, kCCPRFHmacAlgSHA512,
                             2, mac_key, KEY_SIZE) != kCCSuccess) {
        return 0;
    }
    memcpy(hmac_data, db->page1 + SALT_SIZE, DB_PAGE_SIZE - RESERVE_SIZE);
    hmac_data[DB_PAGE_SIZE - RESERVE_SIZE + 0] = 1;
    hmac_data[DB_PAGE_SIZE - RESERVE_SIZE + 1] = 0;
    hmac_data[DB_PAGE_SIZE - RESERVE_SIZE + 2] = 0;
    hmac_data[DB_PAGE_SIZE - RESERVE_SIZE + 3] = 0;
    CCHmac(kCCHmacAlgSHA512, mac_key, KEY_SIZE,
           hmac_data, sizeof(hmac_data), digest);
    unsigned char diff = 0;
    for (int i = 0; i < HMAC_SIZE; i++)
        diff |= (unsigned char)(digest[i] ^ db->page1[DB_PAGE_SIZE - HMAC_SIZE + i]);
    return diff == 0;
}

int main(int argc, char *argv[]) {
    pid_t pid = -1;
    const char *out_path = "all_keys.json";
    const char *requested_home = NULL;
    const char *db_salts_path = NULL;  /* Pre-discovered salts from Python */
    uid_t owner_uid = (uid_t)-1;
    gid_t owner_gid = (gid_t)-1;
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--output") == 0) {
            if (++i >= argc) {
                fprintf(stderr, "--output requires a path\n");
                return 64;
            }
            out_path = argv[i];
        } else if (strcmp(argv[i], "--home") == 0) {
            if (++i >= argc) {
                fprintf(stderr, "--home requires a path\n");
                return 64;
            }
            requested_home = argv[i];
        } else if (strcmp(argv[i], "--db-salts") == 0) {
            if (++i >= argc) {
                fprintf(stderr, "--db-salts requires a path\n");
                return 64;
            }
            db_salts_path = argv[i];
        } else if (strcmp(argv[i], "--owner-uid") == 0) {
            if (++i >= argc) {
                fprintf(stderr, "--owner-uid requires a numeric uid\n");
                return 64;
            }
            owner_uid = (uid_t)strtoul(argv[i], NULL, 10);
        } else if (strcmp(argv[i], "--owner-gid") == 0) {
            if (++i >= argc) {
                fprintf(stderr, "--owner-gid requires a numeric gid\n");
                return 64;
            }
            owner_gid = (gid_t)strtoul(argv[i], NULL, 10);
        } else if (strcmp(argv[i], "--pid") == 0) {
            if (++i >= argc) {
                fprintf(stderr, "--pid requires a process id\n");
                return 64;
            }
            pid = atoi(argv[i]);
        } else {
            pid = atoi(argv[i]);
        }
    }
    if (pid <= 0)
        pid = find_wechat_pid();

    if (pid <= 0) {
        fprintf(stderr, "WeChat not running or invalid PID\n");
        return 1;
    }

    printf("============================================================\n");
    printf("  macOS WeChat Memory Key Scanner (C version)\n");
    printf("============================================================\n");
    printf("WeChat PID: %d\n", pid);
    fflush(stdout);

    if (enforce_wechat_pid_version_guard(&pid, 1) != 0) {
        return 2;
    }

    /* Get task port */
    mach_port_t task;
    kern_return_t kr = task_for_pid(mach_task_self(), pid, &task);
    if (kr != KERN_SUCCESS) {
        fprintf(stderr, "task_for_pid failed: %d\n", kr);
        fprintf(stderr, "Make sure: (1) running as root, (2) WeChat is ad-hoc signed\n");
        return 1;
    }
    printf("Got task port: %u\n", task);

    /* Resolve real user's HOME (sudo may change HOME to /var/root) */
    const char *home = requested_home ? requested_home : getenv("HOME");
    const char *sudo_user = getenv("SUDO_USER");
    if (sudo_user) {
        struct passwd *pw = getpwnam(sudo_user);
        if (pw && pw->pw_dir)
            home = pw->pw_dir;
    }
    if (!home) home = "/root";
    printf("User home: %s\n", home);

    /* Collect DB salts: prefer pre-discovered salts from Python (no FDA needed),
     * fall back to recursive disk walk (requires Full Disk Access). */
    printf("\nScanning for DB files...\n");
    if (db_salts_path) {
        printf("  Loading pre-discovered salts from: %s\n", db_salts_path);
        if (load_db_salts_from_file(db_salts_path) != 0) {
            fprintf(stderr, "Failed to load --db-salts file; falling back to disk scan\n");
            db_salts_path = NULL;  /* fall through to nftw */
        }
    }
    if (!db_salts_path) {
        /* Legacy path: walk filesystem directly (requires FDA on macOS) */
        char db_base_dir[512];
        snprintf(db_base_dir, sizeof(db_base_dir),
            "%s/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files",
            home);

        DIR *xdir = opendir(db_base_dir);
        if (xdir) {
            struct dirent *ent;
            while ((ent = readdir(xdir)) != NULL) {
                if (ent->d_name[0] == '.') continue;
                char storage_path[768];
                snprintf(storage_path, sizeof(storage_path),
                    "%s/%s/db_storage", db_base_dir, ent->d_name);
                struct stat st;
                if (stat(storage_path, &st) == 0 && S_ISDIR(st.st_mode)) {
                    nftw(storage_path, nftw_collect_db, 20, FTW_PHYS);
                }
            }
            closedir(xdir);
        }
    }
    printf("Found %d encrypted DBs\n", g_db_count);

    /* Scan memory for x' patterns */
    printf("\nScanning memory for keys...\n");
    key_entry_t keys[MAX_KEYS];
    int key_count = 0;
    int candidate_count = 0;
    size_t total_scanned = 0;
    int region_count = 0;

    mach_vm_address_t addr = 0;
    while (1) {
        mach_vm_size_t size = 0;
        vm_region_basic_info_data_64_t info;
        mach_msg_type_number_t info_count = VM_REGION_BASIC_INFO_COUNT_64;
        mach_port_t obj_name;

        kr = mach_vm_region(task, &addr, &size, VM_REGION_BASIC_INFO_64,
                           (vm_region_info_t)&info, &info_count, &obj_name);
        if (kr != KERN_SUCCESS) break;
        if (size == 0) { addr++; continue; }  /* guard against infinite loop */

        if ((info.protection & (VM_PROT_READ | VM_PROT_WRITE)) ==
            (VM_PROT_READ | VM_PROT_WRITE)) {
            region_count++;

            mach_vm_address_t ca = addr;
            while (ca < addr + size) {
                mach_vm_size_t cs = addr + size - ca;
                if (cs > CHUNK_SIZE) cs = CHUNK_SIZE;

                vm_offset_t data;
                mach_msg_type_number_t dc;
                kr = mach_vm_read(task, ca, cs, &data, &dc);
                if (kr == KERN_SUCCESS) {
                    unsigned char *buf = (unsigned char *)data;
                    total_scanned += dc;

                    for (size_t i = 0; i + HEX_PATTERN_LEN + 3 < dc; i++) {
                        if (buf[i] == 'x' && buf[i + 1] == '\'') {
                            /* Check if followed by 96 hex chars and closing ' */
                            int valid = 1;
                            for (int j = 0; j < HEX_PATTERN_LEN; j++) {
                                if (!is_hex_char(buf[i + 2 + j])) { valid = 0; break; }
                            }
                            if (!valid) continue;
                            if (buf[i + 2 + HEX_PATTERN_LEN] != '\'') continue;
                            candidate_count++;

                            /* Extract key and salt hex */
                            char key_hex[65], salt_hex[33];
                            memcpy(key_hex, buf + i + 2, 64);
                            key_hex[64] = '\0';
                            memcpy(salt_hex, buf + i + 2 + 64, 32);
                            salt_hex[32] = '\0';

                            /* Convert to lowercase for comparison */
                            for (int j = 0; key_hex[j]; j++)
                                if (key_hex[j] >= 'A' && key_hex[j] <= 'F')
                                    key_hex[j] += 32;
                            for (int j = 0; salt_hex[j]; j++)
                                if (salt_hex[j] >= 'A' && salt_hex[j] <= 'F')
                                    salt_hex[j] += 32;

                            /* Authenticate the candidate against page 1 before
                             * retaining it.  Salt equality alone is not proof
                             * of a usable SQLCipher key. */
                            int authenticated = 0;
                            for (int j = 0; j < g_db_count; j++) {
                                if (strcmp(salt_hex, g_dbs[j].salt_hex) == 0 &&
                                    verify_key_for_db(key_hex, &g_dbs[j])) {
                                    authenticated = 1;
                                    break;
                                }
                            }
                            if (!authenticated) continue;

                            /* Deduplicate authenticated candidates by key+salt. */
                            int dup = 0;
                            for (int k = 0; k < key_count; k++) {
                                if (strcmp(keys[k].key_hex, key_hex) == 0 &&
                                    strcmp(keys[k].salt_hex, salt_hex) == 0) {
                                    dup = 1; break;
                                }
                            }
                            if (dup) continue;

                            if (key_count < MAX_KEYS) {
                                strcpy(keys[key_count].key_hex, key_hex);
                                strcpy(keys[key_count].salt_hex, salt_hex);
                                snprintf(keys[key_count].full_pragma, sizeof(keys[key_count].full_pragma),
                                    "x'%s%s'", key_hex, salt_hex);
                                key_count++;
                            }
                        }
                    }
                    mach_vm_deallocate(mach_task_self(), data, dc);
                }
                /* Advance with overlap to catch patterns spanning chunk boundaries.
                 * Pattern is x'<96 hex chars>' = 99 bytes total. */
                if (cs > HEX_PATTERN_LEN + 3)
                    ca += cs - (HEX_PATTERN_LEN + 3);
                else
                    ca += cs;
            }
        }
        addr += size;
    }

    printf("\nScan complete: %zuMB scanned, %d regions, %d candidate patterns, %d unique keys\n",
           total_scanned / 1024 / 1024, region_count, candidate_count, key_count);

    /* Match keys to DBs */
    printf("\n%-25s %-66s %s\n", "Database", "Key", "Salt");
    printf("%-25s %-66s %s\n",
        "-------------------------",
        "------------------------------------------------------------------",
        "--------------------------------");

    int matched = 0;
    for (int i = 0; i < key_count; i++) {
        const char *db = NULL;
        for (int j = 0; j < g_db_count; j++) {
            if (strcmp(keys[i].salt_hex, g_dbs[j].salt_hex) == 0 &&
                verify_key_for_db(keys[i].key_hex, &g_dbs[j])) {
                db = g_dbs[j].name;
                matched++;
                break;
            }
        }
        printf("  %s: %s\n", db ? db : "(unknown)", db ? "matched" : "unmatched");
    }
    printf("\nMatched %d/%d keys to known DBs\n", matched, key_count);

    if (g_db_count == 0) {
        fprintf(stderr,
            "\n[WARNING] 未扫描到任何加密数据库文件 (g_db_count=0)。\n"
            "  可能原因: 运行 sudo 时终端未获得 Full Disk Access，\n"
            "  无法读取 ~/Library/Containers/com.tencent.xinWeChat/ 目录。\n"
            "  解决方案: 使用 --db-salts 参数传入预计算的数据库 salt 文件，\n"
            "  或在系统设置中为终端授予 Full Disk Access 后重试。\n");
    } else if (matched == 0 && key_count > 0) {
        fprintf(stderr,
            "\n[WARNING] 在内存中找到 %d 个密钥，但无法匹配到任何数据库。\n"
            "  可能原因: 数据库 salt 与内存中的密钥不对应。\n"
            "  请确认微信已登录且消息数据库已生成。\n", key_count);
    }

    /* Save JSON: { "rel/path.db": { "enc_key": "hex" }, ... }
     * Uses forward slashes (native macOS paths, valid JSON without escaping).
     * Unlink existing file first to allow re-runs (replaces O_EXCL).
     */
    if (matched == 0) {
        fprintf(stderr, "\n[ERROR] 无有效密钥可写入，跳过文件输出。\n");
        return 4;
    }
    unlink(out_path);  /* Remove existing file to allow re-creation */
    int out_fd = open(out_path, O_WRONLY | O_CREAT | O_NOFOLLOW, 0600);
    if (out_fd < 0) {
        perror("Unable to open key output file");
        return 3;
    }
    if (fchmod(out_fd, 0600) != 0) {
        perror("Unable to protect key output file");
        close(out_fd);
        return 3;
    }
    const char *sudo_uid = getenv("SUDO_UID");
    const char *sudo_gid = getenv("SUDO_GID");
    if (owner_uid == (uid_t)-1 && sudo_uid)
        owner_uid = (uid_t)strtoul(sudo_uid, NULL, 10);
    if (owner_gid == (gid_t)-1 && sudo_gid)
        owner_gid = (gid_t)strtoul(sudo_gid, NULL, 10);
    if (geteuid() == 0 && owner_uid != (uid_t)-1 && owner_gid != (gid_t)-1) {
        if (fchown(out_fd, owner_uid, owner_gid) != 0) {
            perror("Unable to restore key file ownership");
            close(out_fd);
            return 3;
        }
    }
    FILE *fp = fdopen(out_fd, "w");
    if (fp) {
        fprintf(fp, "{\n");
        int first = 1;
        /* Iterate databases, rather than keys, so duplicate salts never
         * produce duplicate JSON object names. */
        for (int j = 0; j < g_db_count; j++) {
            const char *key = NULL;
            for (int i = 0; i < key_count; i++) {
                if (strcmp(keys[i].salt_hex, g_dbs[j].salt_hex) == 0 &&
                    verify_key_for_db(keys[i].key_hex, &g_dbs[j])) {
                    key = keys[i].key_hex;
                    break;
                }
            }
            if (!key) continue;
            fprintf(fp, "%s  \"%s\": {\"enc_key\": \"%s\", \"salt\": \"%s\"}",
                first ? "" : ",\n", g_dbs[j].name, key, g_dbs[j].salt_hex);
            first = 0;
        }
        fprintf(fp, "\n}\n");
        fclose(fp);
        printf("Saved to %s\n", out_path);
    } else {
        perror("Unable to write key output file");
        close(out_fd);
        return 3;
    }

    return 0;
}
