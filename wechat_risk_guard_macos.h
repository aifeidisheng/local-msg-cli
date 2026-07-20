#ifndef WECHAT_RISK_GUARD_MACOS_H
#define WECHAT_RISK_GUARD_MACOS_H

#include <errno.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

/*
 * Native scanners are directly executable, so Python entry-point guards are
 * insufficient. Before task_for_pid(), invoke the colocated fail-closed guard
 * against the exact PIDs that will be inspected.
 */
static int wechat_guard_executable_dir(char *out, size_t out_size) {
    char raw_path[PATH_MAX];
    char resolved[PATH_MAX];
    uint32_t raw_size = (uint32_t)sizeof(raw_path);
    if (_NSGetExecutablePath(raw_path, &raw_size) != 0) {
        fprintf(stderr, "[version] cannot resolve scanner executable path\n");
        return -1;
    }
    if (!realpath(raw_path, resolved)) {
        fprintf(stderr, "[version] cannot resolve scanner path: %s\n", strerror(errno));
        return -1;
    }
    char *slash = strrchr(resolved, '/');
    if (!slash) {
        fprintf(stderr, "[version] invalid scanner path: %s\n", resolved);
        return -1;
    }
    *slash = '\0';
    if (snprintf(out, out_size, "%s", resolved) >= (int)out_size) {
        fprintf(stderr, "[version] scanner directory path is too long\n");
        return -1;
    }
    return 0;
}

static int enforce_wechat_pid_version_guard(const pid_t *pids, int count) {
    if (!pids || count <= 0 || count > 64) {
        fprintf(stderr, "[version] no valid WeChat PID supplied; refusing risky action\n");
        return -1;
    }

    char app_dir[PATH_MAX];
    char guard_script[PATH_MAX];
    char venv_python[PATH_MAX];
    if (wechat_guard_executable_dir(app_dir, sizeof(app_dir)) != 0) return -1;
    if (snprintf(guard_script, sizeof(guard_script), "%s/wechat_risk_actions.py", app_dir)
            >= (int)sizeof(guard_script) || access(guard_script, R_OK) != 0) {
        fprintf(stderr,
                "[version] guard script missing beside scanner: %s\n"
                "[version] refusing to access WeChat process memory\n",
                guard_script);
        return -1;
    }

    const char *python = "/usr/bin/python3";
    if (snprintf(venv_python, sizeof(venv_python), "%s/.venv/bin/python3", app_dir)
            < (int)sizeof(venv_python) && access(venv_python, X_OK) == 0) {
        python = venv_python;
    } else if (access(python, X_OK) != 0) {
        fprintf(stderr, "[version] Python 3 not found; refusing risky action\n");
        return -1;
    }

    char pid_text[64][32];
    char *args[68];
    args[0] = (char *)python;
    args[1] = guard_script;
    args[2] = "check-pids";
    for (int i = 0; i < count; i++) {
        snprintf(pid_text[i], sizeof(pid_text[i]), "%d", pids[i]);
        args[3 + i] = pid_text[i];
    }
    args[3 + count] = NULL;

    pid_t child = fork();
    if (child < 0) {
        fprintf(stderr, "[version] cannot start version guard: %s\n", strerror(errno));
        return -1;
    }
    if (child == 0) {
        setenv("WECHAT_DECRYPT_APP_DIR", app_dir, 1);
        execv(python, args);
        fprintf(stderr, "[version] cannot execute version guard: %s\n", strerror(errno));
        _exit(127);
    }

    int status = 0;
    if (waitpid(child, &status, 0) < 0) {
        fprintf(stderr, "[version] cannot wait for version guard: %s\n", strerror(errno));
        return -1;
    }
    if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
        fprintf(stderr, "[version] WeChat version guard rejected process memory access\n");
        return -1;
    }
    return 0;
}

#endif
