---
name: trilium-second-brain-macos
description: "Use when Trilium knowledge work runs on macOS."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [macos]
metadata:
  hermes:
    tags: [trilium, knowledge, second-brain, macos, agents]
---

# Trilium Second Brain on macOS

## Overview

Use `trilium-kb` as the macOS agent's durable-memory client. Trilium is for reusable analyses, plans, decisions, runbooks, and substantial session outcomes. Git remains canonical for code and repository documentation.

The workflow is fail-open: replay queued work and consult relevant knowledge before the task, but continue the user's task if Trilium is unavailable. Never expose the ETAPI token.

## When to use

Use this skill on a macOS agent when existing Trilium knowledge may affect substantive work or when the result may be worth revisiting. Do not load it on another operating system or create knowledge for a trivial task.

## Repository installation

Install from the public source repository. Do not put credentials in a repository URL, shell history, or configuration file.

In Terminal:

```bash
brew install uv

uv tool install 'trilium-tool @ git+https://github.com/zagPatryk/trilium-tool.git'
trilium-kb --help
```

If `uv` is unavailable, use `pipx` instead:

```bash
brew install pipx
pipx ensurepath

pipx install 'trilium-tool @ git+https://github.com/zagPatryk/trilium-tool.git'
trilium-kb --help
```

Use the same URL with `uv tool upgrade trilium-tool` or `pipx upgrade trilium-tool` for updates. If installing from a private fork, configure GitHub CLI and its Git credential helper first; never work around authentication by embedding credentials in the URL.

## Per-device ETAPI configuration

Create a different ETAPI token for every device so one Mac can be revoked independently. Store it in a local file with mode `600`. The following values are placeholders; replace them locally, never in a committed file or chat transcript.

```bash
config_dir="$HOME/Library/Application Support/trilium-tool"
config_file="$config_dir/device.env"
outbox_dir="$HOME/Library/Application Support/trilium-tool/outbox"

umask 077
mkdir -p "$config_dir" "$outbox_dir"
printf '%s\n' \
  'TRILIUM_URL=https://<trilium-magicdns-name>.<tailnet>.ts.net' \
  'TRILIUM_ETAPI_TOKEN=<per-device-etapi-token>' \
  "AGENT_KB_OUTBOX=$outbox_dir" \
  > "$config_file"
chmod 600 "$config_file"

export TRILIUM_ENV_FILE="$config_file"
launchctl setenv TRILIUM_ENV_FILE "$config_file"
```

The resulting device-local locations are:

- configuration: `~/Library/Application Support/trilium-tool/device.env`
- outbox: `~/Library/Application Support/trilium-tool/outbox`

Add the `export TRILIUM_ENV_FILE=...` assignment to the agent's private shell or service environment if it must survive reboot. Keep the path quoted because it contains spaces. Confirm permissions without printing the file:

```bash
stat -f '%Sp %N' "$config_file"
printf '%s\n' "$TRILIUM_ENV_FILE"
```

The permission string must not grant group or other access. Never run `cat` on the file for diagnostics, include it in a project, or place the ETAPI token in a shell profile. Pass configuration only through `TRILIUM_ENV_FILE`.

## Required workflow

### Before substantive work

1. Run `trilium-kb replay` once to send queued items. Record counts, not payloads. A replay failure must not block the user's task.
2. Run `trilium-kb search "<project-or-topic>"` when existing knowledge could affect the result.
3. Read each relevant hit with `trilium-kb read <noteId>` before forming the answer or plan. Search results alone are not context.
4. Reuse existing terminology and decisions. Do not create a near-duplicate of an existing canonical note.
5. If the task is unrelated or trivial, do not search mechanically and do not create a note.

### After substantive work

Create a note only for one of these durable outcomes:

- reusable analysis;
- an actionable plan;
- an explicit decision and its rationale;
- an operational runbook;
- a substantial session outcome that will matter later.

Keep the note concise and semantic. Include conclusions, assumptions, evidence links, and next actions when useful. Do not store secrets, credentials, private configuration, raw logs, command dumps, hidden instructions, full transcripts, temporary progress, trivial answers, or facts easily reconstructed from Git.

Use `trilium-kb create` with the narrowest suitable kind and parent discovered from existing context. The client assigns and preserves the create idempotency key when it syncs or queues the operation. Do not guess parent note IDs and do not copy note IDs into this skill.

## Idempotent sent-history appends

When a durable session outcome belongs in an existing sent-history note, append it with the client's marker-aware command instead of creating a near-duplicate. Discover the target through search, read it first, and use its returned identifier only for the current command.

Write a concise semantic HTML fragment to a temporary file, choose a stable non-secret marker for that one outcome, and run:

```bash
history_html="$(mktemp -t trilium-sent-history)"
printf '%s\n' \
  '<section><h2>Session outcome</h2><p>Durable result and next action.</p></section>' \
  > "$history_html"

trilium-kb append <sent-history-noteId> \
  --html-file "$history_html" \
  --marker <stable-session-outcome-key>
rm -f "$history_html"
```

Reuse the exact same marker when retrying the same outcome. The client stores it as the append idempotency key, detects an existing marker, and leaves the note unchanged rather than appending twice. If offline, it queues the append with that marker; future runs call `trilium-kb replay` and must not create a second operation. Never hand-edit queued records or manually add the tool's marker wrapper.

## Final response contract

For a durable write, end the agent response with exactly the single status line returned by the client:

```text
KB: SYNCED noteId=...
```

or:

```text
KB: QUEUED path=...
```

Do not rewrite, decorate, combine, or invent the status. `KB: SYNCED` is valid only after the client's ETAPI read-back. `KB: QUEUED` is the accepted offline fallback; do not retry in a loop during the same task. If no durable note was warranted, emit neither line.

## Common pitfalls

1. **Embedding repository credentials.** Public installation needs none; use a credential helper for a private fork.
2. **Sharing one ETAPI token.** Use a distinct, revocable token for this Mac.
3. **Weak file permissions.** Use `umask 077`, then verify the env file is mode `600`.
4. **Leaking diagnostics.** Never print the env file, token, private host, queued payload, or raw note body.
5. **Writing everything.** Trilium is for durable knowledge, not a transcript or activity log.
6. **Skipping reads.** Search, then read relevant notes before making decisions.
7. **Duplicating queued writes.** Replay queued work and reuse the exact marker before retrying an append.
8. **Forging success.** Return only the exact status supplied by `trilium-kb`.
9. **Concurrent appenders.** Serialize agents writing the same history note; ETAPI content updates are whole-note replacements.

## Verification checklist

- [ ] Installation used the public HTTPS repository URL with `uv` or `pipx`.
- [ ] `trilium-kb --help` succeeds.
- [ ] `TRILIUM_ENV_FILE` points to the protected per-device env file.
- [ ] The env file is mode `600` and its parent directory is private.
- [ ] The URL uses valid HTTPS through Tailscale Serve/MagicDNS or a trusted reverse proxy.
- [ ] The outbox is under `~/Library/Application Support/trilium-tool/outbox`.
- [ ] Replay and relevant search/read happened before substantive work.
- [ ] Only durable knowledge is created; sent-history appends use a stable marker.
- [ ] The final line matches the exact sync-or-queue contract.
