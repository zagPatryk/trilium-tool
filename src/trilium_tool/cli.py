"""Command-line interface for trilium-tool."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import urllib.error

from .core import (
    ALLOWED_KINDS,
    KnowledgeWriter,
    Outbox,
    TriliumClient,
    ValidationError,
    load_config,
)


def _parse_pairs(items: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in items:
        name, separator, value = item.partition("=")
        if not separator or not name.strip() or not value.strip():
            raise ValidationError(f"Expected NAME=VALUE, got: {item}")
        result[name] = value
    return result


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trilium-tool",
        description="Safe Trilium ETAPI knowledge client with a durable outbox",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    search = commands.add_parser("search", help="Search Trilium notes")
    search.add_argument("query")

    read = commands.add_parser("read", help="Read note metadata and HTML")
    read.add_argument("note_id")

    create = commands.add_parser("create", help="Create or queue a knowledge note")
    create.add_argument("--parent", required=True)
    create.add_argument("--title", required=True)
    create.add_argument("--kind", required=True, choices=sorted(ALLOWED_KINDS))
    create.add_argument("--html-file", required=True, type=pathlib.Path)
    create.add_argument("--label", action="append", default=[], metavar="NAME=VALUE")
    create.add_argument(
        "--relation", action="append", default=[], metavar="NAME=NOTE_ID"
    )

    append = commands.add_parser(
        "append", help="Append or queue a marked HTML fragment"
    )
    append.add_argument("note_id")
    append.add_argument("--html-file", required=True, type=pathlib.Path)
    append.add_argument("--marker", required=True)

    commands.add_parser("replay", help="Replay queued creates and appends")
    return parser


def _run(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    config = load_config()
    client = TriliumClient(config.url, config.token)
    writer = KnowledgeWriter(client, Outbox(config.outbox))

    if args.command == "search":
        results = [
            {key: note.get(key) for key in ("noteId", "title", "type")}
            for note in client.search(args.query)
        ]
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0

    if args.command == "read":
        print(json.dumps(client.read(args.note_id), ensure_ascii=False, indent=2))
        return 0

    if args.command == "create":
        labels = {
            "status": "draft",
            "createdBy": config.actor,
            "language": config.language,
            "schemaVersion": "1",
        }
        labels.update(_parse_pairs(args.label))
        payload = {
            "parentNoteId": args.parent,
            "title": args.title,
            "kind": args.kind,
            "html": args.html_file.read_text(encoding="utf-8"),
            "labels": labels,
            "relations": _parse_pairs(args.relation),
        }
        result = writer.create(payload)
        if result["status"] == "SYNCED":
            print(
                f"KB: SYNCED noteId={result['noteId']} "
                f"idempotencyKey={result['idempotencyKey']}"
            )
        else:
            print(
                f"KB: QUEUED path={result['path']} "
                f"idempotencyKey={result['idempotencyKey']}"
            )
        return 0

    if args.command == "append":
        result = writer.append(
            args.note_id,
            args.html_file.read_text(encoding="utf-8"),
            args.marker,
        )
        if result["status"] == "SYNCED":
            suffix = " unchanged=true" if result["unchanged"] else ""
            print(f"KB: SYNCED noteId={result['noteId']}{suffix}")
        else:
            print(f"KB: QUEUED path={result['path']}")
        return 0

    if args.command == "replay":
        result = writer.replay()
        print(
            f"KB: REPLAY sent={result['sent']} "
            f"deduplicated={result['deduplicated']} failed={result['failed']}"
        )
        return 1 if result["failed"] else 0

    return 2


def main(argv: list[str] | None = None) -> int:
    try:
        return _run(argv)
    except (
        json.JSONDecodeError,
        OSError,
        TypeError,
        UnicodeError,
        ValidationError,
        urllib.error.URLError,
        RuntimeError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
