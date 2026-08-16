# trilium-tool

`trilium-tool` is a small, dependency-free Python command-line client for working with a Trilium Notes knowledge base. It supports searching and reading notes, creating notes, safely appending content, and replaying queued writes after a connection becomes available.

Python 3.11 or newer is required. The alias `trilium-kb` provides the same command-line interface as `trilium-tool`.

## Install

### GitHub with `uv` or `pipx`

Install directly from the public repository:

```sh
uv tool install "trilium-tool @ git+https://github.com/OWNER/REPOSITORY.git"
# or
pipx install "trilium-tool @ git+https://github.com/OWNER/REPOSITORY.git"
```

Replace `OWNER/REPOSITORY` with the repository location. To update an existing `uv` installation, run `uv tool upgrade trilium-tool`; with `pipx`, run `pipx upgrade trilium-tool`. If you install from a private fork, configure Git authentication through SSH or a system credential helper; never put a token in a command, URL, shell history, or requirements file.

### Install from source

```sh
git clone git@github.com:OWNER/REPOSITORY.git
cd REPOSITORY
uv tool install .
```

Alternatively, create and activate a Python 3.11+ virtual environment and run:

```sh
python -m pip install .
```

## Configuration

The client reads configuration from the environment:

| Variable | Purpose |
| --- | --- |
| `TRILIUM_URL` | Base URL of the Trilium server. |
| `TRILIUM_ETAPI_TOKEN` | ETAPI authentication token. Treat it as a secret. |
| `TRILIUM_ENV_FILE` | Optional path to an environment file containing configuration. |
| `TRILIUM_ACTOR` | Optional actor name recorded for write operations. |
| `TRILIUM_LANGUAGE` | Optional language tag used for note content or metadata. |
| `AGENT_KB_OUTBOX` | Optional platform-specific outbox path override. |

Explicit process environment values take precedence when an environment file is used. Keep the environment file readable only by your account and outside version control.

### macOS and Linux

Add exports to your shell profile, such as `~/.zshrc`, or place them in a protected environment file:

```sh
export TRILIUM_URL="https://trilium.example.com"
export TRILIUM_ETAPI_TOKEN="read-from-your-secret-manager"
export TRILIUM_ENV_FILE="$HOME/.config/trilium-tool/env"
export TRILIUM_ACTOR="local-cli"
export TRILIUM_LANGUAGE="en"
```

A configuration file at `~/.config/trilium-tool/env` can contain:

```dotenv
TRILIUM_URL=https://trilium.example.com
TRILIUM_ETAPI_TOKEN=read-from-your-secret-manager
TRILIUM_ACTOR=local-cli
TRILIUM_LANGUAGE=en
```

Protect it with `chmod 600 ~/.config/trilium-tool/env`.

### Windows PowerShell

Set values for the current PowerShell session:

```powershell
$env:TRILIUM_URL = "https://trilium.example.com"
$env:TRILIUM_ETAPI_TOKEN = "read-from-your-secret-manager"
$env:TRILIUM_ENV_FILE = "$HOME\.config\trilium-tool\env"
$env:TRILIUM_ACTOR = "local-cli"
$env:TRILIUM_LANGUAGE = "en"
```

Or save non-secret defaults with `setx` and load the token from Windows Credential Manager when starting a session. An environment file can use the same `KEY=value` form shown above. Restrict its ACL to your Windows account.

## Commands

Run `trilium-tool --help` or `trilium-tool COMMAND --help` for the authoritative option list. `trilium-kb` may be substituted in every example.

### Search

Search the knowledge base and print matching notes:

```sh
trilium-tool search "deployment checklist"
```

### Read

Read a note by its identifier:

```sh
trilium-tool read NOTE_IDENTIFIER
```

### Create

Create a note under a parent note from a UTF-8 HTML file:

```sh
trilium-tool create \
  --parent PARENT_IDENTIFIER \
  --title "Release notes" \
  --kind analysis \
  --html-file release-notes.html \
  --label status=draft \
  --relation project=PROJECT_IDENTIFIER
```

`--label NAME=VALUE` and `--relation NAME=NOTE_IDENTIFIER` may each be repeated. Valid kinds are `project`, `area`, `resource`, `analysis`, `plan`, `decision`, `runbook`, `session-outcome`, `incident`, and `moc`. HTML is checked locally and active content such as scripts, iframes, and inline event handlers is rejected.

### Append

Append a UTF-8 HTML fragment to an existing note. Choose a stable, operation-specific marker so retries remain idempotent:

```sh
trilium-tool append NOTE_IDENTIFIER \
  --html-file follow-up.html \
  --marker release-follow-up
```

### Replay

Replay pending operations from the local outbox:

```sh
trilium-tool replay
```

## Outbox and idempotency

When a write cannot be delivered, the tool persists the operation in a local outbox and can replay it later. The default location follows each platform's user-data convention:

- Windows: `%LOCALAPPDATA%\trilium-tool\outbox`
- macOS: `~/Library/Application Support/trilium-tool/outbox`
- Linux and other Unix-like systems: `$XDG_DATA_HOME/trilium-tool/outbox`, or `~/.local/share/trilium-tool/outbox` when `XDG_DATA_HOME` is unset

Set `AGENT_KB_OUTBOX` to override the location on any platform. Use `replay` after connectivity is restored.

Write operations carry idempotency information so retrying or replaying the same operation does not intentionally create duplicate changes. Preserve the outbox between retries, do not hand-edit queued records, and use a stable actor value where practical.

Use a marker for exactly one immutable append payload. Reusing it with different content is rejected. Serialize writes when multiple agents target the same note: Trilium's content endpoint replaces the complete note body and does not expose a compare-and-swap precondition.

The outbox serializes operation data required for replay, but **never serializes credentials**. In particular, `TRILIUM_ETAPI_TOKEN` and authorization headers are not written to queued records. Authentication is read again from the runtime environment during replay. Because note content may still be sensitive, protect the local outbox with normal account-level filesystem permissions and do not commit it.

## Security

- Grant the Trilium token only the permissions needed by this client.
- Supply secrets through a secret manager or protected environment file.
- Do not embed credentials in Git URLs, command arguments, logs, screenshots, or queued operations.
- Keep TLS enabled and verify the server URL before sending data.
- Review queued content before moving an outbox between machines.

## Development

Run the full standard-library test suite from the repository root:

```sh
uv run python -m unittest discover -s tests
```

Check lint and formatting:

```sh
uvx ruff check .
uvx ruff format --check .
```

Build source and wheel distributions:

```sh
uv build
```

The runtime package uses only the Python standard library.
