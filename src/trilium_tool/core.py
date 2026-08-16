"""Core Trilium ETAPI operations and durable outbox support."""

from __future__ import annotations

import datetime
import hashlib
import html
import json
import os
import pathlib
import platform
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any


class ValidationError(ValueError):
    """Raised when input does not satisfy the local safety contract."""


ALLOWED_KINDS = {
    "project",
    "area",
    "resource",
    "analysis",
    "plan",
    "decision",
    "runbook",
    "session-outcome",
    "incident",
    "moc",
}
RESERVED_LABELS = {"idempotencyKey", "type"}
_FORBIDDEN_ELEMENTS = {
    "base",
    "button",
    "embed",
    "form",
    "iframe",
    "input",
    "link",
    "meta",
    "object",
    "script",
    "select",
    "style",
    "textarea",
}
_FORBIDDEN_TAG = re.compile(
    rf"<\s*/?\s*(?:{'|'.join(sorted(_FORBIDDEN_ELEMENTS))})\b",
    re.IGNORECASE,
)
_INLINE_EVENT_HANDLER = re.compile(r"\bon[a-z][\w:.-]*\s*=", re.IGNORECASE)
_MARKER_ATTRIBUTE = "data-trilium-tool-marker"
_URL_ATTRIBUTES = {"action", "formaction", "href", "poster", "src", "xlink:href"}


def _origin(url: str) -> tuple[str, str | None, int | None]:
    parsed = urllib.parse.urlsplit(url)
    default_port = {"http": 80, "https": 443}.get(parsed.scheme.casefold())
    return parsed.scheme.casefold(), parsed.hostname, parsed.port or default_port


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Allow redirects only within the configured Trilium origin."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        if _origin(req.full_url) != _origin(newurl):
            raise urllib.error.HTTPError(
                newurl,
                code,
                "Refusing cross-origin redirect",
                headers,
                fp,
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _SafetyParser(HTMLParser):
    def _check(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in _FORBIDDEN_ELEMENTS:
            raise ValidationError("HTML contains a forbidden element")
        for name, value in attrs:
            lowered = name.casefold()
            if lowered.startswith("on") and len(lowered) > 2 and lowered[2].isalpha():
                raise ValidationError("HTML contains an inline event handler")
            if lowered in {"srcdoc", "style"}:
                raise ValidationError("HTML contains an active attribute")
            if lowered in _URL_ATTRIBUTES and value is not None:
                normalized = re.sub(r"[\x00-\x20]+", "", value).casefold()
                if normalized.startswith(("data:", "javascript:", "vbscript:")):
                    raise ValidationError("HTML contains an active URL")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._check(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._check(tag, attrs)


class _MarkerParser(HTMLParser):
    def __init__(self, marker: str) -> None:
        super().__init__(convert_charrefs=True)
        self.marker = marker
        self.found = False
        self.content_sha256: str | None = None

    def _check(self, attrs: list[tuple[str, str | None]]) -> None:
        if any(
            name.casefold() == _MARKER_ATTRIBUTE and value == self.marker
            for name, value in attrs
        ):
            self.found = True
            self.content_sha256 = next(
                (
                    value
                    for name, value in attrs
                    if name.casefold() == "data-trilium-tool-sha256"
                ),
                None,
            )

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._check(attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._check(attrs)


def validate_html(content: str) -> None:
    """Reject empty or active HTML fragments."""
    if not isinstance(content, str) or not content.strip():
        raise ValidationError("HTML payload is empty")
    if _FORBIDDEN_TAG.search(content) or _INLINE_EVENT_HANDLER.search(content):
        raise ValidationError("HTML contains active content")
    parser = _SafetyParser(convert_charrefs=True)
    parser.feed(content)
    parser.close()


def _validate_pairs(value: object, field_name: str) -> None:
    if not isinstance(value, dict):
        raise ValidationError(f"{field_name} must be an object")
    for name, item in value.items():
        if not isinstance(name, str) or not name.strip():
            raise ValidationError(f"{field_name} names must be nonempty strings")
        if not isinstance(item, str) or not item.strip():
            raise ValidationError(f"{field_name} values must be nonempty strings")


def validate_payload(payload: dict[str, Any]) -> None:
    """Validate a schema-v1 create payload."""
    if not isinstance(payload, dict):
        raise ValidationError("Payload must be an object")
    for field_name in ("parentNoteId", "title", "html"):
        value = payload.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"Missing or empty field: {field_name}")
    if payload.get("kind") not in ALLOWED_KINDS:
        raise ValidationError("Unknown knowledge note kind")
    labels = payload.get("labels", {})
    relations = payload.get("relations", {})
    _validate_pairs(labels, "Labels")
    _validate_pairs(relations, "Relations")
    if RESERVED_LABELS & labels.keys():
        raise ValidationError("Reserved labels cannot be overridden")
    validate_html(payload["html"])


def _validate_append(note_id: str, fragment: str, marker: str) -> None:
    if not isinstance(note_id, str) or not note_id.strip():
        raise ValidationError("Note ID is empty")
    if not isinstance(marker, str) or not marker.strip():
        raise ValidationError("Append marker is empty")
    validate_html(fragment)


def _is_transient_http_error(error: urllib.error.HTTPError) -> bool:
    return error.code in {408, 425, 429} or 500 <= error.code <= 599


def _note_id_for_idempotency(key: str) -> str:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return f"tk_{digest[:29]}"


def _marker_state(content: str, marker: str) -> tuple[bool, str | None]:
    parser = _MarkerParser(marker)
    parser.feed(content)
    parser.close()
    return parser.found, parser.content_sha256


def _append_content(content: str, fragment: str, marker: str) -> str:
    escaped_marker = html.escape(marker, quote=True)
    content_sha256 = hashlib.sha256(fragment.encode("utf-8")).hexdigest()
    wrapper = (
        f'<section {_MARKER_ATTRIBUTE}="{escaped_marker}" '
        f'data-trilium-tool-sha256="{content_sha256}">{fragment}</section>'
    )
    separator = "" if not content or content.endswith(("\n", "\r")) else "\n"
    return f"{content}{separator}{wrapper}"


@dataclass(frozen=True)
class Config:
    """Runtime configuration with the credential omitted from representations."""

    url: str
    token: str = field(repr=False)
    actor: str = "agent"
    language: str = "en"
    outbox: pathlib.Path = pathlib.Path("outbox")


def _expand_path(value: str, home: pathlib.Path) -> pathlib.Path:
    if value == "~":
        return home
    if value.startswith("~/") or value.startswith("~\\"):
        return home / value[2:]
    return pathlib.Path(value).expanduser()


def default_outbox_path(
    environ: Mapping[str, str] | None = None,
    *,
    platform_name: str | None = None,
    home: pathlib.Path | None = None,
) -> pathlib.Path:
    """Return the platform-native default outbox directory."""
    values = os.environ if environ is None else environ
    resolved_home = pathlib.Path.home() if home is None else home
    explicit = values.get("AGENT_KB_OUTBOX")
    if explicit:
        return _expand_path(explicit, resolved_home)

    system_name = platform.system() if platform_name is None else platform_name
    if system_name == "Windows":
        local_app_data = values.get("LOCALAPPDATA")
        root = (
            _expand_path(local_app_data, resolved_home)
            if local_app_data
            else resolved_home / "AppData" / "Local"
        )
    elif system_name == "Darwin":
        root = resolved_home / "Library" / "Application Support"
    else:
        xdg_data_home = values.get("XDG_DATA_HOME")
        root = (
            _expand_path(xdg_data_home, resolved_home)
            if xdg_data_home
            else resolved_home / ".local" / "share"
        )
    return root / "trilium-tool" / "outbox"


def _load_env_file(path: pathlib.Path) -> dict[str, str]:
    if not path.is_file():
        raise ValidationError(f"TRILIUM_ENV_FILE does not exist: {path}")
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_config(
    environ: Mapping[str, str] | None = None,
    *,
    platform_name: str | None = None,
    home: pathlib.Path | None = None,
) -> Config:
    """Load configuration from an explicit env file, then environment overrides."""
    environment = dict(os.environ if environ is None else environ)
    resolved_home = pathlib.Path.home() if home is None else home
    values: dict[str, str] = {}
    env_file = environment.get("TRILIUM_ENV_FILE")
    if env_file:
        values.update(_load_env_file(_expand_path(env_file, resolved_home)))
    values.update(environment)

    url = values.get("TRILIUM_URL", "").strip()
    token = values.get("TRILIUM_ETAPI_TOKEN", "").strip()
    if not url or not token:
        raise ValidationError("Missing TRILIUM_URL or TRILIUM_ETAPI_TOKEN")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValidationError("TRILIUM_URL must be an HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValidationError("TRILIUM_URL must not contain credentials")

    actor = values.get("TRILIUM_ACTOR", "").strip() or "agent"
    language = values.get("TRILIUM_LANGUAGE", "").strip() or "en"
    return Config(
        url=url.rstrip("/"),
        token=token,
        actor=actor,
        language=language,
        outbox=default_outbox_path(
            values, platform_name=platform_name, home=resolved_home
        ),
    )


class TriliumClient:
    """Small ETAPI client with explicit read-back verification."""

    def __init__(self, base_url: str, token: str, timeout: float = 10) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_SameOriginRedirectHandler())

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        raw_body: str | None = None,
        raw_response: bool = False,
    ) -> Any:
        if payload is not None and raw_body is not None:
            raise ValueError("A request cannot have both JSON and raw bodies")
        data: bytes | None = None
        content_type: str | None = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            content_type = "application/json"
        elif raw_body is not None:
            data = raw_body.encode("utf-8")
            content_type = "text/plain; charset=utf-8"

        request = urllib.request.Request(self.base_url + path, data=data, method=method)
        request.add_header("Authorization", self.token)
        if content_type:
            request.add_header("Content-Type", content_type)
        with self._opener.open(request, timeout=self.timeout) as response:
            body = response.read()
        if raw_response:
            return body.decode("utf-8")
        if not body:
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Invalid JSON response from Trilium") from exc

    def search(self, query: str) -> list[dict[str, Any]]:
        if not isinstance(query, str) or not query.strip():
            raise ValidationError("Search query is empty")
        encoded = urllib.parse.urlencode({"search": query, "limit": 100})
        response = self._request("GET", f"/etapi/notes?{encoded}")
        try:
            results = response["results"]
        except (KeyError, TypeError) as exc:
            raise RuntimeError("Invalid Trilium search response") from exc
        if not isinstance(results, list) or not all(
            isinstance(item, dict) for item in results
        ):
            raise RuntimeError("Invalid Trilium search response")
        return results

    def read_content(self, note_id: str) -> str:
        if not isinstance(note_id, str) or not note_id.strip():
            raise ValidationError("Note ID is empty")
        encoded_id = urllib.parse.quote(note_id, safe="")
        return self._request(
            "GET", f"/etapi/notes/{encoded_id}/content", raw_response=True
        )

    def read(self, note_id: str) -> dict[str, Any]:
        if not isinstance(note_id, str) or not note_id.strip():
            raise ValidationError("Note ID is empty")
        encoded_id = urllib.parse.quote(note_id, safe="")
        note = self._request("GET", f"/etapi/notes/{encoded_id}")
        if not isinstance(note, dict):
            raise RuntimeError("Invalid Trilium note response")
        note["content"] = self.read_content(note_id)
        return note

    def find_by_idempotency(self, key: str) -> str | None:
        for note in self.search(f"#idempotencyKey={key}"):
            attributes = note.get("attributes", [])
            if any(
                attribute.get("type") == "label"
                and attribute.get("name") == "idempotencyKey"
                and attribute.get("value") == key
                for attribute in attributes
                if isinstance(attribute, dict)
            ):
                return note.get("noteId")
        return None

    def create(self, payload: dict[str, Any], idempotency_key: str) -> str:
        validate_payload(payload)
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValidationError("Idempotency key is empty")
        note_id = _note_id_for_idempotency(idempotency_key)
        try:
            self.read(note_id)
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            created = self._request(
                "POST",
                "/etapi/create-note",
                payload={
                    "parentNoteId": payload["parentNoteId"],
                    "title": payload["title"],
                    "type": "text",
                    "content": payload["html"],
                    "noteId": note_id,
                },
            )
            try:
                created_note_id = created["note"]["noteId"]
            except (KeyError, TypeError) as response_error:
                raise RuntimeError(
                    "Invalid Trilium create response"
                ) from response_error
            if created_note_id != note_id:
                raise RuntimeError("Trilium ignored the deterministic note ID")
        return self.complete_create(note_id, payload, idempotency_key)

    def complete_create(
        self,
        note_id: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> str:
        """Complete and verify a deterministic or previously matched create."""
        validate_payload(payload)
        if not isinstance(note_id, str) or not note_id.strip():
            raise ValidationError("Note ID is empty")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValidationError("Idempotency key is empty")
        readback = self.read(note_id)

        if (
            readback.get("title") != payload["title"]
            or readback.get("content") != payload["html"]
        ):
            raise RuntimeError("Idempotent read-back collision or content mismatch")

        labels = {
            "idempotencyKey": idempotency_key,
            "type": payload["kind"],
            **payload.get("labels", {}),
        }
        attributes = [("label", name, value) for name, value in labels.items()]
        attributes.extend(
            ("relation", name, value)
            for name, value in payload.get("relations", {}).items()
        )
        actual = {
            (attribute.get("type"), attribute.get("name"), attribute.get("value"))
            for attribute in readback.get("attributes", [])
            if isinstance(attribute, dict)
        }
        for position, (attribute_type, name, value) in enumerate(attributes, start=1):
            if (attribute_type, name, value) in actual:
                continue
            self._request(
                "POST",
                "/etapi/attributes",
                payload={
                    "noteId": note_id,
                    "type": attribute_type,
                    "name": name,
                    "value": value,
                    "position": position * 10,
                    "isInheritable": False,
                },
            )

        readback = self.read(note_id)
        actual = {
            (attribute.get("type"), attribute.get("name"), attribute.get("value"))
            for attribute in readback.get("attributes", [])
            if isinstance(attribute, dict)
        }
        if (
            readback.get("title") != payload["title"]
            or readback.get("content") != payload["html"]
            or not set(attributes).issubset(actual)
        ):
            raise RuntimeError("Trilium read-back verification failed")
        return note_id

    def append(self, note_id: str, fragment: str, marker: str) -> bool:
        """Append a marked wrapper, returning False when it already exists."""
        _validate_append(note_id, fragment, marker)
        current = self.read_content(note_id)
        marker_found, existing_sha256 = _marker_state(current, marker)
        fragment_sha256 = hashlib.sha256(fragment.encode("utf-8")).hexdigest()
        if marker_found:
            if existing_sha256 is not None and existing_sha256 != fragment_sha256:
                raise ValidationError(
                    "Append marker already exists with different content"
                )
            return False
        updated = _append_content(current, fragment, marker)
        encoded_id = urllib.parse.quote(note_id, safe="")
        self._request("PUT", f"/etapi/notes/{encoded_id}/content", raw_body=updated)
        if self.read_content(note_id) != updated:
            raise RuntimeError("Trilium append read-back verification failed")
        return True


class Outbox:
    """Durable one-file-per-operation queue."""

    def __init__(self, directory: pathlib.Path) -> None:
        self.directory = directory

    @staticmethod
    def _tighten_permissions(path: pathlib.Path, mode: int) -> None:
        if os.name != "nt":
            path.chmod(mode)

    def _ensure_directory(self, directory: pathlib.Path) -> None:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._tighten_permissions(directory, 0o700)

    def _enqueue(
        self,
        operation: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> pathlib.Path:
        self._ensure_directory(self.directory)
        now = datetime.datetime.now(datetime.timezone.utc)
        created_at = now.isoformat(timespec="seconds")
        record = {
            "schemaVersion": 1,
            "operation": operation,
            "idempotencyKey": idempotency_key,
            "createdAt": created_at,
            "contentSha256": hashlib.sha256(
                payload["html"].encode("utf-8")
            ).hexdigest(),
            "payload": payload,
        }
        unique = uuid.uuid4().hex
        filename = f"{now.strftime('%Y%m%dT%H%M%S%fZ')}-{unique}.json"
        destination = self.directory / filename
        temporary = self.directory / f".{filename}.{uuid.uuid4().hex}.tmp"
        temporary.write_text(
            json.dumps(record, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._tighten_permissions(temporary, 0o600)
        os.replace(temporary, destination)
        self._tighten_permissions(destination, 0o600)
        return destination

    def enqueue(
        self,
        payload: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> pathlib.Path:
        validate_payload(payload)
        key = str(uuid.uuid4()) if idempotency_key is None else idempotency_key
        if not isinstance(key, str) or not key.strip():
            raise ValidationError("Idempotency key is empty")
        return self._enqueue("create", payload, key)

    def enqueue_append(self, note_id: str, fragment: str, marker: str) -> pathlib.Path:
        _validate_append(note_id, fragment, marker)
        payload = {"noteId": note_id, "html": fragment, "marker": marker}
        return self._enqueue("append", payload, marker)

    def records(self) -> list[pathlib.Path]:
        if not self.directory.exists():
            return []
        self._tighten_permissions(self.directory, 0o700)
        records = sorted(self.directory.glob("*.json"))
        for path in records:
            self._tighten_permissions(path, 0o600)
        return records

    def load(self, path: pathlib.Path) -> dict[str, Any]:
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict) or record.get("schemaVersion") != 1:
            raise ValidationError("Unsupported outbox schema")
        operation = record.get("operation", "create")
        if operation not in {"create", "append"}:
            raise ValidationError("Unsupported outbox operation")
        # Schema-v1 records from before append support omitted the operation.
        # Normalize them so replay can continue through the current code path.
        record["operation"] = operation
        key = record.get("idempotencyKey")
        payload = record.get("payload")
        if not isinstance(key, str) or not key.strip() or not isinstance(payload, dict):
            raise ValidationError("Invalid outbox record")
        if operation == "create":
            validate_payload(payload)
        else:
            _validate_append(
                payload.get("noteId"), payload.get("html"), payload.get("marker")
            )
            if payload["marker"] != key:
                raise ValidationError(
                    "Append marker does not match its idempotency key"
                )
        expected_hash = hashlib.sha256(payload["html"].encode("utf-8")).hexdigest()
        if record.get("contentSha256") != expected_hash:
            raise ValidationError("Outbox content hash mismatch")
        return record

    def mark_sent(self, path: pathlib.Path) -> pathlib.Path:
        sent_directory = self.directory / "sent"
        self._ensure_directory(sent_directory)
        destination = sent_directory / path.name
        os.replace(path, destination)
        self._tighten_permissions(destination, 0o600)
        return destination


class KnowledgeWriter:
    """Write through to Trilium, or durably queue transient failures."""

    def __init__(self, client: TriliumClient, outbox: Outbox) -> None:
        self.client = client
        self.outbox = outbox

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        validate_payload(payload)
        key = str(uuid.uuid4())
        try:
            note_id = self.client.create(payload, key)
            return {"status": "SYNCED", "noteId": note_id, "idempotencyKey": key}
        except urllib.error.HTTPError as exc:
            if not _is_transient_http_error(exc):
                raise
            path = self.outbox.enqueue(payload, idempotency_key=key)
            return {"status": "QUEUED", "path": str(path), "idempotencyKey": key}
        except (urllib.error.URLError, TimeoutError, ConnectionError, RuntimeError):
            path = self.outbox.enqueue(payload, idempotency_key=key)
            return {"status": "QUEUED", "path": str(path), "idempotencyKey": key}

    def append(self, note_id: str, fragment: str, marker: str) -> dict[str, Any]:
        _validate_append(note_id, fragment, marker)
        try:
            changed = self.client.append(note_id, fragment, marker)
            return {"status": "SYNCED", "noteId": note_id, "unchanged": not changed}
        except urllib.error.HTTPError as exc:
            if not _is_transient_http_error(exc):
                raise
            path = self.outbox.enqueue_append(note_id, fragment, marker)
            return {"status": "QUEUED", "path": str(path), "marker": marker}
        except (urllib.error.URLError, TimeoutError, ConnectionError, RuntimeError):
            path = self.outbox.enqueue_append(note_id, fragment, marker)
            return {"status": "QUEUED", "path": str(path), "marker": marker}

    def replay(self) -> dict[str, int]:
        result = {"sent": 0, "deduplicated": 0, "failed": 0}
        for path in self.outbox.records():
            try:
                record = self.outbox.load(path)
                operation = record["operation"]
                payload = record["payload"]
                if operation == "create":
                    key = record["idempotencyKey"]
                    existing_note_id = self.client.find_by_idempotency(key)
                    if existing_note_id:
                        self.client.complete_create(existing_note_id, payload, key)
                        result["deduplicated"] += 1
                    else:
                        self.client.create(payload, key)
                        result["sent"] += 1
                else:
                    changed = self.client.append(
                        payload["noteId"],
                        payload["html"],
                        payload["marker"],
                    )
                    result["sent" if changed else "deduplicated"] += 1
                self.outbox.mark_sent(path)
            except (
                json.JSONDecodeError,
                OSError,
                KeyError,
                TypeError,
                UnicodeError,
                ValidationError,
                urllib.error.URLError,
                TimeoutError,
                ConnectionError,
                RuntimeError,
            ):
                result["failed"] += 1
        return result
