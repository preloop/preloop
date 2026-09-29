# Windows (WSL2)

**Windows binaries are available; WSL is the recommended way to run Preloop.**

Every Preloop release publishes Windows CLI binaries for `amd64` and `arm64`,
and the OSS stack runs on Docker Desktop with the WSL2 backend. What we do not
claim is *native* Windows support: the `.exe` is built and published by CI but
is not yet covered by an end-to-end Windows test suite, and several
host-integration features degrade (see [Known limits](#known-limits)). Run the
CLI inside WSL and everything behaves exactly as it does on Linux.

## Run the OSS stack

Preloop's release Docker Compose file has no bind mounts into the host, no
`network_mode: host`, and does not mount the Docker socket, so it runs
unmodified under Docker Desktop.

1. Install [WSL2](https://learn.microsoft.com/windows/wsl/install) and a Linux
   distribution (Ubuntu is the default and is well tested).
2. Install Docker Desktop and enable the **WSL2 backend**
   (*Settings → General → Use the WSL 2 based engine*), then enable integration
   for your distribution under *Settings → Resources → WSL Integration*.
3. Open your WSL shell — **not** PowerShell or Git Bash — and run the normal
   installer:

    ```bash
    curl -fsSL https://preloop.ai/install/oss | sh
    ```

From here, follow [Install the OSS Stack](installation.md); nothing else differs.

!!! warning "Keep the install directory on the WSL filesystem"
    Install to a WSL path such as `~/.preloop-oss` (the default). Do **not** set
    `INSTALL_DIR` to a location under `/mnt/c` or any other Windows drive mount.

    The TLS overlay bind-mounts `./tls/active.conf`, `./certbot/conf`, and
    `./certbot/www` relative to the install directory. Across the `/mnt/c`
    boundary these paths get inconsistent permissions and case handling, and
    certbot cannot reliably write or re-read its certificates. Directory I/O on
    `/mnt/c` is also dramatically slower.

## Install the CLI

### Inside WSL (recommended)

Use the standard installer — it is an ordinary Linux install:

```bash
curl -fsSL https://preloop.ai/install/cli | sh
```

!!! info "Agents installed on the Windows side"
    The CLI treats WSL as a first-class environment, but it can only onboard
    agents it can actually see. An agent installed on Windows is not on the WSL
    `PATH`, and `preloop agents discover` will say so. Install the agent inside
    WSL, or add its Windows install directory to your WSL `PATH`. See
    [CLI Reference → WSL](../guide/cli.md#wsl).

### Native Windows binary

Download `preloop-windows-amd64.exe` or `preloop-windows-arm64.exe` from the
[GitHub releases page](https://github.com/preloop/preloop/releases/latest)
(use `arm64` on Snapdragon/Copilot+ devices, `amd64` otherwise).

Put it somewhere on your `PATH` and rename it to `preloop.exe`:

```powershell
$dir = "$env:LOCALAPPDATA\Programs\Preloop"
New-Item -ItemType Directory -Force -Path $dir
Move-Item .\preloop-windows-amd64.exe "$dir\preloop.exe"

# Add to PATH for future sessions
[Environment]::SetEnvironmentVariable(
  "Path",
  [Environment]::GetEnvironmentVariable("Path", "User") + ";$dir",
  "User"
)
```

Open a new terminal, then verify:

```powershell
preloop version
```

!!! warning "Do not use the shell installer under Git Bash"
    `install/cli` is a POSIX shell script. Under Git Bash it cannot write to
    `/usr/local/bin` and falls back to `~/.local/bin` — a directory that is
    **not** on the Windows `PATH`, so `preloop` appears to install successfully
    and then is not found. Download the `.exe` and set `PATH` as above instead.

!!! note "MSYS path rewriting"
    Git Bash rewrites arguments that look like absolute POSIX paths into Windows
    paths, which breaks commands that pass container-side paths to Docker. The
    installer sets `MSYS_NO_PATHCONV=1` where it matters; if you run
    `docker compose` commands from our docs by hand under Git Bash and get a
    path like `C:/Program Files/Git/var/...`, prefix the command with
    `MSYS_NO_PATHCONV=1`.

## Known limits

These affect the **native Windows binary only**. Running the CLI inside WSL
avoids all of them.

| Area | Behaviour on native Windows |
|------|-----------------------------|
| Claude Desktop discovery | Supported — the CLI reads `%APPDATA%\Claude\claude_desktop_config.json`. |
| Credential probing | Auth probes that read POSIX keychains and shell-based credential helpers do not resolve on Windows; some agents will report an unverified credential and need manual re-verification. |
| OpenClaw runtime management | Runtime install and lifecycle management assume POSIX process and path semantics; manage OpenClaw from inside WSL. |
| Managed agent launchers | Not available. Onboarding can generate a wrapper script that launches an agent with Preloop's environment pre-applied; it is emitted as a `bash` script into `~/.local/bin`, which Windows cannot execute. MCP firewall and model-gateway routing still work — only the generated launcher is skipped. |
| Agents installed under WSL | Not visible to the Windows binary (and vice versa) — the two have separate home directories and `PATH`s. |

!!! danger "Token files are not ACL-restricted on Windows"
    Preloop writes durable bearer tokens to disk with mode `0600` so that they
    are not world-readable. On Windows, Go's `os.Chmod` only toggles the
    read-only attribute and **does not** restrict NTFS ACLs, so that protection
    silently does not apply — the token file inherits whatever ACL its parent
    directory grants.

    If you use the native Windows binary, keep Preloop's config directory on a
    volume only your user account can read, or run the CLI inside WSL where the
    `0600` mode is enforced. Proper ACL hardening is tracked for a future
    release.

## Reporting Windows issues

Windows is newly supported and we want the bug reports. Please include your
Windows version, whether you are on WSL or the native binary, and the output of
`preloop version` in a [GitHub issue](https://github.com/preloop/preloop/issues).
