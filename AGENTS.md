# Agent Instructions

## End-user installation

When a user asks to install, configure, initialize, or connect this MCP on
macOS, treat the request as an end-user installation unless the user explicitly
asks to develop or debug the source code.

For an end-user installation:

1. Run `./install.sh`. This is the only supported bootstrap entry point.
2. Do not create a `.venv` in the checkout and do not run `setup.sh`,
   `setup.py`, `main.py init`, `service.py install`, or a manually compiled
   `find_all_keys_macos` from the checkout.
3. After `install.sh` succeeds, ask for separate confirmation before the
   sensitive initialization step.
4. Run the installed management command without `sudo`:

   ```bash
   "$HOME/Library/Application Support/WeChatDecryptLight/bin/wechat-decrypt-light" initialize --json
   ```

5. Use only the command's `error_code` and `next_action` to recover from an
   initialization failure. Do not replace the installed workflow with manual
   key extraction.
6. Register `http://127.0.0.1:8765/mcp` only when the returned service status is
   `ready` and `query_ready` is `true`.
7. After registration, call `data_source_status` for a no-user-data connectivity
   check. Do not query contacts or messages merely to verify installation.

The source-development workflow is allowed only when the user explicitly asks
to modify, test, or debug this repository. In that case, use
`./setup.sh --development` and the source commands documented in the
development section of `README.md`.
