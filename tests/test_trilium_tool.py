from __future__ import annotations

import contextlib
import email.message
import hashlib
import http.server
import io
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
from typing import ClassVar

from trilium_tool import (
    ALLOWED_KINDS,
    Config,
    KnowledgeWriter,
    Outbox,
    TriliumClient,
    ValidationError,
    default_outbox_path,
    load_config,
    validate_html,
    validate_payload,
)
from trilium_tool.cli import main

TEST_CREDENTIAL = "unit-test-credential"
PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]


class FakeTriliumHandler(http.server.BaseHTTPRequestHandler):
    notes: ClassVar[dict[str, dict]] = {}
    contents: ClassVar[dict[str, str]] = {}
    posted_attributes: ClassVar[list[dict]] = []
    requests: ClassVar[list[dict]] = []
    put_count: ClassVar[int] = 0
    persist_put: ClassVar[bool] = True
    corrupt_created_content: ClassVar[bool] = False
    attribute_failures_remaining: ClassVar[int] = 0
    attribute_failure_on_attempt: ClassVar[int | None] = None
    attribute_attempt_count: ClassVar[int] = 0
    invalid_create_responses_remaining: ClassVar[int] = 0

    @classmethod
    def reset(cls) -> None:
        cls.notes = {
            "note_demo": {
                "noteId": "note_demo",
                "title": "Example note",
                "type": "text",
                "attributes": [],
                "parentNoteIds": ["parent_demo"],
            }
        }
        cls.contents = {"note_demo": "<h1>Example note</h1>"}
        cls.posted_attributes = []
        cls.requests = []
        cls.put_count = 0
        cls.persist_put = True
        cls.corrupt_created_content = False
        cls.attribute_failures_remaining = 0
        cls.attribute_failure_on_attempt = None
        cls.attribute_attempt_count = 0
        cls.invalid_create_responses_remaining = 0

    def _record(self, body: bytes = b"") -> None:
        type(self).requests.append(
            {
                "method": self.command,
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "content_type": self.headers.get("Content-Type"),
                "body": body,
            }
        )

    def _json(self, status: int, payload: object) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _empty(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        self._record()
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/etapi/notes":
            self._json(200, {"results": list(type(self).notes.values())})
            return
        if parsed.path.startswith("/etapi/notes/") and parsed.path.endswith("/content"):
            note_id = urllib.parse.unquote(parsed.path.split("/")[-2])
            content = type(self).contents[note_id].encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return
        if parsed.path.startswith("/etapi/notes/"):
            note_id = urllib.parse.unquote(parsed.path.rsplit("/", 1)[-1])
            if note_id not in type(self).notes:
                self._json(404, {"message": "not found"})
                return
            self._json(200, type(self).notes[note_id])
            return
        self._json(404, {"message": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self._record(body)
        payload = json.loads(body or b"{}")
        if self.path == "/etapi/create-note":
            number = 1 + sum(
                note_id.startswith("created_") for note_id in type(self).notes
            )
            note_id = payload.get("noteId") or f"created_{number}"
            if note_id in type(self).notes:
                self._json(409, {"message": "already exists"})
                return
            note = {
                "noteId": note_id,
                "title": payload["title"],
                "type": payload["type"],
                "attributes": [],
                "parentNoteIds": [payload["parentNoteId"]],
            }
            type(self).notes[note_id] = note
            content = payload["content"]
            if type(self).corrupt_created_content:
                content += "<p>server changed this</p>"
            type(self).contents[note_id] = content
            if type(self).invalid_create_responses_remaining:
                type(self).invalid_create_responses_remaining -= 1
                invalid = b"{"
                self.send_response(201)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(invalid)))
                self.end_headers()
                self.wfile.write(invalid)
                return
            self._json(201, {"note": note, "branch": {"branchId": "branch_demo"}})
            return
        if self.path == "/etapi/attributes":
            type(self).attribute_attempt_count += 1
            if (
                type(self).attribute_failure_on_attempt
                == type(self).attribute_attempt_count
            ):
                type(self).attribute_failure_on_attempt = None
                self._json(503, {"message": "temporarily unavailable"})
                return
            if type(self).attribute_failures_remaining:
                type(self).attribute_failures_remaining -= 1
                self._json(503, {"message": "temporarily unavailable"})
                return
            attribute = {
                "attributeId": f"attribute_{len(type(self).posted_attributes) + 1}",
                **payload,
            }
            type(self).posted_attributes.append(attribute)
            type(self).notes[payload["noteId"]]["attributes"].append(attribute)
            self._json(201, attribute)
            return
        self._json(404, {"message": "not found"})

    def do_PUT(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self._record(body)
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/etapi/notes/") and parsed.path.endswith("/content"):
            note_id = urllib.parse.unquote(parsed.path.split("/")[-2])
            type(self).put_count += 1
            if type(self).persist_put:
                type(self).contents[note_id] = body.decode("utf-8")
            self._empty(204)
            return
        self._json(404, {"message": "not found"})

    def log_message(self, format: str, *args: object) -> None:
        return


class FakeTriliumServer:
    def __enter__(self) -> FakeTriliumServer:
        FakeTriliumHandler.reset()
        self.server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), FakeTriliumHandler
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.url = f"http://{host}:{port}"
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


@contextlib.contextmanager
def unavailable_url():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    host, port = sock.getsockname()
    sock.close()
    yield f"http://{host}:{port}"


def sample_payload(*, kind: str = "analysis") -> dict:
    return {
        "parentNoteId": "parent_demo",
        "title": "Example analysis",
        "kind": kind,
        "html": "<h1>Result</h1><p>Durable content.</p>",
        "labels": {"status": "draft", "createdBy": "agent"},
        "relations": {"project": "project_demo"},
    }


class HtmlValidationTests(unittest.TestCase):
    def test_accepts_nonempty_passive_html(self) -> None:
        validate_html("<section><h2>Safe</h2><p>Text</p></section>")

    def test_rejects_empty_html(self) -> None:
        for content in ("", " \n\t"):
            with self.subTest(content=content), self.assertRaises(ValidationError):
                validate_html(content)

    def test_rejects_script_iframe_and_inline_event_handlers(self) -> None:
        unsafe = [
            "<script>alert(1)</script>",
            '<IFRAME src="https://example.test"></IFRAME>',
            '<p onclick="alert(1)">text</p>',
            "<img ONERROR = alert(1)>",
            '<a href="javascript:alert(1)">run</a>',
            '<object data="https://example.test/payload"></object>',
            '<meta http-equiv="refresh" content="0;url=https://example.test">',
            '<p style="background:url(data:text/html,x)">text</p>',
        ]
        for content in unsafe:
            with self.subTest(content=content), self.assertRaises(ValidationError):
                validate_html(content)


class PayloadValidationTests(unittest.TestCase):
    def test_accepts_full_schema_v1_kind_set(self) -> None:
        expected = {
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
        self.assertEqual(ALLOWED_KINDS, expected)
        for kind in expected:
            with self.subTest(kind=kind):
                validate_payload(sample_payload(kind=kind))

    def test_rejects_missing_fields_unknown_kind_and_reserved_labels(self) -> None:
        valid = sample_payload()
        invalid = [
            {**valid, "title": ""},
            {**valid, "parentNoteId": ""},
            {**valid, "html": ""},
            {**valid, "kind": "unsupported"},
            {**valid, "labels": {"idempotencyKey": "caller-value"}},
            {**valid, "labels": {"type": "caller-value"}},
            {**valid, "labels": []},
            {**valid, "relations": []},
        ]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                validate_payload(payload)


class ConfigTests(unittest.TestCase):
    def test_loads_explicit_env_file_and_environment_takes_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = pathlib.Path(directory) / "trilium.env"
            env_file.write_text(
                "\n".join(
                    [
                        "TRILIUM_URL=https://from-file.example.test",
                        "TRILIUM_ETAPI_TOKEN=file-value",
                        "TRILIUM_ACTOR=file-actor",
                        "TRILIUM_LANGUAGE=de",
                    ]
                ),
                encoding="utf-8",
            )
            config = load_config(
                {
                    "TRILIUM_ENV_FILE": str(env_file),
                    "TRILIUM_URL": "https://from-env.example.test",
                    "TRILIUM_ETAPI_TOKEN": TEST_CREDENTIAL,
                },
                platform_name="Linux",
                home=pathlib.Path(directory),
            )

        self.assertEqual(config.url, "https://from-env.example.test")
        self.assertEqual(config.token, TEST_CREDENTIAL)
        self.assertEqual(config.actor, "file-actor")
        self.assertEqual(config.language, "de")
        self.assertNotIn(TEST_CREDENTIAL, repr(config))

    def test_defaults_actor_and_language(self) -> None:
        config = load_config(
            {
                "TRILIUM_URL": "https://trilium.example.test",
                "TRILIUM_ETAPI_TOKEN": TEST_CREDENTIAL,
            },
            platform_name="Linux",
            home=pathlib.Path("/home/example"),
        )
        self.assertEqual((config.actor, config.language), ("agent", "en"))

    def test_rejects_missing_credentials_and_credentials_in_url(self) -> None:
        invalid_environments = [
            {},
            {"TRILIUM_URL": "https://trilium.example.test"},
            {
                "TRILIUM_URL": "https://user:password@trilium.example.test",
                "TRILIUM_ETAPI_TOKEN": TEST_CREDENTIAL,
            },
            {
                "TRILIUM_URL": "file:///tmp/trilium",
                "TRILIUM_ETAPI_TOKEN": TEST_CREDENTIAL,
            },
        ]
        for environment in invalid_environments:
            with (
                self.subTest(environment=environment),
                self.assertRaises(ValidationError),
            ):
                load_config(environment)

    def test_default_outbox_paths_are_cross_platform(self) -> None:
        home = pathlib.Path("/Users/example")
        self.assertEqual(
            default_outbox_path({}, platform_name="Linux", home=home),
            home / ".local" / "share" / "trilium-tool" / "outbox",
        )
        self.assertEqual(
            default_outbox_path(
                {"XDG_DATA_HOME": "/data/example"},
                platform_name="Linux",
                home=home,
            ),
            pathlib.Path("/data/example/trilium-tool/outbox"),
        )
        self.assertEqual(
            default_outbox_path({}, platform_name="Darwin", home=home),
            home / "Library" / "Application Support" / "trilium-tool" / "outbox",
        )
        self.assertEqual(
            default_outbox_path(
                {"LOCALAPPDATA": "/Users/example/AppData/Local"},
                platform_name="Windows",
                home=home,
            ),
            pathlib.Path("/Users/example/AppData/Local/trilium-tool/outbox"),
        )

    def test_explicit_outbox_overrides_platform_default(self) -> None:
        expected = pathlib.Path("/tmp/example-outbox")
        self.assertEqual(
            default_outbox_path(
                {"AGENT_KB_OUTBOX": str(expected)},
                platform_name="Windows",
                home=pathlib.Path("/Users/example"),
            ),
            expected,
        )


class TriliumClientTests(unittest.TestCase):
    def test_search_and_read_return_server_data(self) -> None:
        with FakeTriliumServer() as server:
            client = TriliumClient(server.url, TEST_CREDENTIAL)
            results = client.search("example")
            note = client.read("note_demo")

        self.assertEqual([item["noteId"] for item in results], ["note_demo"])
        self.assertEqual(note["title"], "Example note")
        self.assertEqual(note["content"], "<h1>Example note</h1>")
        self.assertTrue(FakeTriliumHandler.requests)
        self.assertTrue(
            all(
                request["authorization"] == TEST_CREDENTIAL
                for request in FakeTriliumHandler.requests
            )
        )

    def test_search_rejects_non_list_results(self) -> None:
        class InvalidSearchClient(TriliumClient):
            def _request(self, *args: object, **kwargs: object) -> object:
                return {"results": "invalid"}

        client = InvalidSearchClient(
            "https://trilium.example.test",
            TEST_CREDENTIAL,
        )
        with self.assertRaisesRegex(RuntimeError, "search response"):
            client.search("example")

    def test_cross_origin_redirect_is_blocked_before_token_can_leak(self) -> None:
        class SinkHandler(http.server.BaseHTTPRequestHandler):
            requests = 0
            authorization = None

            def do_GET(self) -> None:  # noqa: N802
                type(self).requests += 1
                type(self).authorization = self.headers.get("Authorization")
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                return

        class RedirectHandler(http.server.BaseHTTPRequestHandler):
            target = ""

            def do_GET(self) -> None:  # noqa: N802
                self.send_response(302)
                self.send_header("Location", type(self).target)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                return

        sink = http.server.ThreadingHTTPServer(("127.0.0.1", 0), SinkHandler)
        source = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        sink_thread = threading.Thread(target=sink.serve_forever, daemon=True)
        source_thread = threading.Thread(target=source.serve_forever, daemon=True)
        sink_thread.start()
        source_thread.start()
        RedirectHandler.target = f"http://127.0.0.1:{sink.server_port}/capture"
        try:
            client = TriliumClient(
                f"http://127.0.0.1:{source.server_port}", TEST_CREDENTIAL
            )
            with self.assertRaisesRegex(
                urllib.error.HTTPError,
                "cross-origin",
            ):
                client.read_content("note_demo")
        finally:
            source.shutdown()
            sink.shutdown()
            source.server_close()
            sink.server_close()
            source_thread.join(timeout=2)
            sink_thread.join(timeout=2)

        self.assertEqual(SinkHandler.requests, 0)
        self.assertIsNone(SinkHandler.authorization)

    def test_create_writes_idempotency_label_first_and_verifies_readback(self) -> None:
        with FakeTriliumServer() as server:
            client = TriliumClient(server.url, TEST_CREDENTIAL)
            note_id = client.create(sample_payload(), idempotency_key="create-key")

        self.assertRegex(note_id, r"^tk_[0-9a-f]{29}$")
        attributes = FakeTriliumHandler.posted_attributes
        triples = [(item["type"], item["name"], item["value"]) for item in attributes]
        self.assertEqual(triples[0], ("label", "idempotencyKey", "create-key"))
        self.assertEqual(triples[1], ("label", "type", "analysis"))
        self.assertIn(("label", "status", "draft"), triples)
        self.assertIn(("relation", "project", "project_demo"), triples)

    def test_create_raises_when_readback_does_not_match(self) -> None:
        with FakeTriliumServer() as server:
            FakeTriliumHandler.corrupt_created_content = True
            client = TriliumClient(server.url, TEST_CREDENTIAL)
            with self.assertRaisesRegex(RuntimeError, "read-back"):
                client.create(sample_payload(), idempotency_key="create-key")

    def test_append_adds_one_escaped_semantic_wrapper_and_verifies_readback(
        self,
    ) -> None:
        fragment = "<h2>History</h2><p>Added once.</p>"
        marker = 'release-1"<&'
        with FakeTriliumServer() as server:
            client = TriliumClient(server.url, TEST_CREDENTIAL)
            changed = client.append("note_demo", fragment, marker)

        self.assertTrue(changed)
        self.assertEqual(FakeTriliumHandler.put_count, 1)
        content_sha256 = hashlib.sha256(fragment.encode("utf-8")).hexdigest()
        self.assertEqual(
            FakeTriliumHandler.contents["note_demo"],
            "<h1>Example note</h1>\n"
            '<section data-trilium-tool-marker="release-1&quot;&lt;&amp;" '
            f'data-trilium-tool-sha256="{content_sha256}">'
            "<h2>History</h2><p>Added once.</p></section>",
        )
        put_request = next(
            request
            for request in FakeTriliumHandler.requests
            if request["method"] == "PUT"
        )
        self.assertEqual(put_request["content_type"], "text/plain; charset=utf-8")

    def test_append_duplicate_marker_is_successful_and_unchanged(self) -> None:
        marker = "stable-history-key"
        with FakeTriliumServer() as server:
            FakeTriliumHandler.contents["note_demo"] += (
                '\n<section data-trilium-tool-marker="stable-history-key">'
                "<p>Existing history.</p></section>"
            )
            client = TriliumClient(server.url, TEST_CREDENTIAL)
            before = FakeTriliumHandler.contents["note_demo"]
            changed = client.append("note_demo", "<p>Should not repeat.</p>", marker)

        self.assertFalse(changed)
        self.assertEqual(FakeTriliumHandler.put_count, 0)
        self.assertEqual(FakeTriliumHandler.contents["note_demo"], before)

    def test_append_rejects_reused_marker_with_different_content(self) -> None:
        with FakeTriliumServer() as server:
            client = TriliumClient(server.url, TEST_CREDENTIAL)
            client.append("note_demo", "<p>Version one.</p>", "stable-key")
            with self.assertRaisesRegex(ValidationError, "different content"):
                client.append("note_demo", "<p>Version two.</p>", "stable-key")

    def test_append_raises_when_put_readback_does_not_match(self) -> None:
        with FakeTriliumServer() as server:
            FakeTriliumHandler.persist_put = False
            client = TriliumClient(server.url, TEST_CREDENTIAL)
            with self.assertRaisesRegex(RuntimeError, "read-back"):
                client.append("note_demo", "<p>History.</p>", "history-key")


class OutboxTests(unittest.TestCase):
    def test_enqueue_create_is_atomic_complete_and_credential_free(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            outbox = Outbox(pathlib.Path(directory))
            path = outbox.enqueue(sample_payload(), idempotency_key="queued-create")
            files = list(pathlib.Path(directory).glob("*.json"))
            temporary_files = list(pathlib.Path(directory).glob("*.tmp"))
            text = path.read_text(encoding="utf-8")
            record = json.loads(text)
            directory_mode = path.parent.stat().st_mode & 0o777
            file_mode = path.stat().st_mode & 0o777

        self.assertEqual(files, [path])
        self.assertEqual(temporary_files, [])
        self.assertEqual(record["operation"], "create")
        self.assertEqual(record["payload"], sample_payload())
        self.assertEqual(len(record["contentSha256"]), 64)
        self.assertEqual(record["idempotencyKey"], "queued-create")
        self.assertNotIn(TEST_CREDENTIAL, text)
        self.assertNotIn("TRILIUM_ETAPI_TOKEN", text)
        if os.name != "nt":
            self.assertEqual(directory_mode, 0o700)
            self.assertEqual(file_mode, 0o600)

    def test_enqueue_append_records_marker_without_client_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            outbox = Outbox(pathlib.Path(directory))
            path = outbox.enqueue_append(
                "note_demo",
                "<p>Queued history.</p>",
                "history-key",
            )
            record = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(record["operation"], "append")
        self.assertEqual(record["idempotencyKey"], "history-key")
        self.assertEqual(
            record["payload"],
            {
                "noteId": "note_demo",
                "html": "<p>Queued history.</p>",
                "marker": "history-key",
            },
        )
        self.assertEqual(len(record["contentSha256"]), 64)

    def test_enqueue_rejects_explicit_blank_idempotency_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            outbox = Outbox(pathlib.Path(directory))
            with self.assertRaisesRegex(ValidationError, "Idempotency key"):
                outbox.enqueue(sample_payload(), idempotency_key="  ")


class KnowledgeWriterTests(unittest.TestCase):
    def test_transient_http_errors_queue_but_client_errors_raise(self) -> None:
        class FailingClient(TriliumClient):
            def __init__(self, code: int) -> None:
                self.code = code

            def create(self, payload: dict, idempotency_key: str) -> str:
                raise urllib.error.HTTPError(
                    "https://trilium.example.test",
                    self.code,
                    "failure",
                    email.message.Message(),
                    None,
                )

            def append(self, note_id: str, fragment: str, marker: str) -> bool:
                raise urllib.error.HTTPError(
                    "https://trilium.example.test",
                    self.code,
                    "failure",
                    email.message.Message(),
                    None,
                )

        with tempfile.TemporaryDirectory() as directory:
            outbox = Outbox(pathlib.Path(directory))
            transient_writer = KnowledgeWriter(FailingClient(503), outbox)
            create_result = transient_writer.create(sample_payload())
            append_result = transient_writer.append(
                "note_demo",
                "<p>Queued.</p>",
                "queued-marker",
            )
            permanent_writer = KnowledgeWriter(FailingClient(401), outbox)
            with self.assertRaises(urllib.error.HTTPError):
                permanent_writer.create(sample_payload())

        self.assertEqual(create_result["status"], "QUEUED")
        self.assertEqual(append_result["status"], "QUEUED")

    def test_replay_recovers_create_after_note_exists_without_attributes(self) -> None:
        with tempfile.TemporaryDirectory() as directory, FakeTriliumServer() as server:
            FakeTriliumHandler.attribute_failures_remaining = 1
            outbox = Outbox(pathlib.Path(directory))
            writer = KnowledgeWriter(TriliumClient(server.url, TEST_CREDENTIAL), outbox)
            initial = writer.create(sample_payload())
            replayed = writer.replay()
            matching = [
                note
                for note in FakeTriliumHandler.notes.values()
                if note["title"] == "Example analysis"
            ]
            attributes = {
                (item["type"], item["name"], item["value"])
                for item in matching[0]["attributes"]
            }

        self.assertEqual(initial["status"], "QUEUED")
        self.assertEqual(replayed, {"sent": 1, "deduplicated": 0, "failed": 0})
        self.assertEqual(len(matching), 1)
        self.assertIn(
            ("label", "idempotencyKey", initial["idempotencyKey"]),
            attributes,
        )

    def test_replay_completes_attributes_after_idempotency_label_was_written(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory, FakeTriliumServer() as server:
            FakeTriliumHandler.attribute_failure_on_attempt = 2
            outbox = Outbox(pathlib.Path(directory))
            writer = KnowledgeWriter(TriliumClient(server.url, TEST_CREDENTIAL), outbox)
            initial = writer.create(sample_payload())
            replayed = writer.replay()
            matching = [
                note
                for note in FakeTriliumHandler.notes.values()
                if note["title"] == "Example analysis"
            ]
            attributes = {
                (item["type"], item["name"], item["value"])
                for item in matching[0]["attributes"]
            }

        self.assertEqual(initial["status"], "QUEUED")
        self.assertEqual(replayed, {"sent": 0, "deduplicated": 1, "failed": 0})
        self.assertEqual(len(matching), 1)
        self.assertTrue(
            {
                ("label", "idempotencyKey", initial["idempotencyKey"]),
                ("label", "type", "analysis"),
                ("label", "status", "draft"),
                ("label", "createdBy", "agent"),
                ("relation", "project", "project_demo"),
            }.issubset(attributes)
        )

    def test_create_queues_original_key_after_ambiguous_json_response(self) -> None:
        with tempfile.TemporaryDirectory() as directory, FakeTriliumServer() as server:
            FakeTriliumHandler.invalid_create_responses_remaining = 1
            outbox = Outbox(pathlib.Path(directory))
            writer = KnowledgeWriter(TriliumClient(server.url, TEST_CREDENTIAL), outbox)
            result = writer.create(sample_payload())
            records = outbox.records()
            record = outbox.load(records[0])
            replayed = writer.replay()
            matching = [
                note
                for note in FakeTriliumHandler.notes.values()
                if note["title"] == "Example analysis"
            ]

        self.assertEqual(result["status"], "QUEUED")
        self.assertEqual(record["idempotencyKey"], result["idempotencyKey"])
        self.assertEqual(replayed, {"sent": 1, "deduplicated": 0, "failed": 0})
        self.assertEqual(len(matching), 1)

    def test_create_queues_when_trilium_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory, unavailable_url() as url:
            writer = KnowledgeWriter(
                TriliumClient(url, TEST_CREDENTIAL, timeout=0.1),
                Outbox(pathlib.Path(directory)),
            )
            result = writer.create(sample_payload())
            queued_file_exists = pathlib.Path(result["path"]).is_file()

        self.assertEqual(result["status"], "QUEUED")
        self.assertTrue(queued_file_exists)

    def test_append_queues_when_trilium_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory, unavailable_url() as url:
            writer = KnowledgeWriter(
                TriliumClient(url, TEST_CREDENTIAL, timeout=0.1),
                Outbox(pathlib.Path(directory)),
            )
            result = writer.append(
                "note_demo",
                "<p>Offline history.</p>",
                "offline-history-key",
            )
            record = json.loads(
                pathlib.Path(result["path"]).read_text(encoding="utf-8")
            )

        self.assertEqual(result["status"], "QUEUED")
        self.assertEqual(record["operation"], "append")
        self.assertEqual(record["payload"]["marker"], "offline-history-key")

    def test_append_duplicate_marker_returns_synced_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory, FakeTriliumServer() as server:
            FakeTriliumHandler.contents["note_demo"] += (
                '\n<section data-trilium-tool-marker="existing-key"><p>Existing.</p></section>'
            )
            writer = KnowledgeWriter(
                TriliumClient(server.url, TEST_CREDENTIAL),
                Outbox(pathlib.Path(directory)),
            )
            result = writer.append("note_demo", "<p>Duplicate.</p>", "existing-key")

        self.assertEqual(
            result,
            {"status": "SYNCED", "noteId": "note_demo", "unchanged": True},
        )

    def test_replay_sends_create_once_and_moves_record_to_sent(self) -> None:
        with tempfile.TemporaryDirectory() as directory, FakeTriliumServer() as server:
            outbox = Outbox(pathlib.Path(directory))
            outbox.enqueue(sample_payload(), idempotency_key="queued-create")
            writer = KnowledgeWriter(TriliumClient(server.url, TEST_CREDENTIAL), outbox)
            first = writer.replay()
            second = writer.replay()
            sent_files = list((pathlib.Path(directory) / "sent").glob("*.json"))

        self.assertEqual(first, {"sent": 1, "deduplicated": 0, "failed": 0})
        self.assertEqual(second, {"sent": 0, "deduplicated": 0, "failed": 0})
        self.assertEqual(len(sent_files), 1)
        matching = [
            note
            for note in FakeTriliumHandler.notes.values()
            if note["title"] == "Example analysis"
        ]
        self.assertEqual(len(matching), 1)

    def test_replay_treats_legacy_record_without_operation_as_create(self) -> None:
        with tempfile.TemporaryDirectory() as directory, FakeTriliumServer() as server:
            outbox = Outbox(pathlib.Path(directory))
            path = outbox.enqueue(sample_payload(), idempotency_key="legacy-create")
            record = json.loads(path.read_text(encoding="utf-8"))
            del record["operation"]
            path.write_text(json.dumps(record), encoding="utf-8")
            writer = KnowledgeWriter(TriliumClient(server.url, TEST_CREDENTIAL), outbox)
            result = writer.replay()
            sent_files = list((pathlib.Path(directory) / "sent").glob("*.json"))

        self.assertEqual(result, {"sent": 1, "deduplicated": 0, "failed": 0})
        self.assertEqual(len(sent_files), 1)
        matching = [
            note
            for note in FakeTriliumHandler.notes.values()
            if note["title"] == "Example analysis"
        ]
        self.assertEqual(len(matching), 1)

    def test_replay_deduplicates_create_by_exact_idempotency_label(self) -> None:
        with tempfile.TemporaryDirectory() as directory, FakeTriliumServer() as server:
            outbox = Outbox(pathlib.Path(directory))
            outbox.enqueue(sample_payload(), idempotency_key="already-created")
            FakeTriliumHandler.notes["existing_demo"] = {
                "noteId": "existing_demo",
                "title": "Example analysis",
                "type": "text",
                "attributes": [
                    {
                        "attributeId": "existing_attribute",
                        "noteId": "existing_demo",
                        "type": "label",
                        "name": "idempotencyKey",
                        "value": "already-created",
                    }
                ],
                "parentNoteIds": ["parent_demo"],
            }
            FakeTriliumHandler.contents["existing_demo"] = sample_payload()["html"]
            writer = KnowledgeWriter(TriliumClient(server.url, TEST_CREDENTIAL), outbox)
            result = writer.replay()

        self.assertEqual(result, {"sent": 0, "deduplicated": 1, "failed": 0})
        matching = [
            note
            for note in FakeTriliumHandler.notes.values()
            if note["title"] == "Example analysis"
        ]
        self.assertEqual([note["noteId"] for note in matching], ["existing_demo"])

    def test_replay_append_is_idempotent_across_duplicate_queue_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory, FakeTriliumServer() as server:
            outbox = Outbox(pathlib.Path(directory))
            for _ in range(2):
                outbox.enqueue_append(
                    "note_demo",
                    "<p>Queued once.</p>",
                    "same-append-key",
                )
            writer = KnowledgeWriter(TriliumClient(server.url, TEST_CREDENTIAL), outbox)
            result = writer.replay()
            content = FakeTriliumHandler.contents["note_demo"]
            sent_files = list((pathlib.Path(directory) / "sent").glob("*.json"))

        self.assertEqual(result, {"sent": 1, "deduplicated": 1, "failed": 0})
        self.assertEqual(content.count('data-trilium-tool-marker="same-append-key"'), 1)
        self.assertEqual(len(sent_files), 2)

    def test_replay_rejects_unsupported_operation_and_leaves_record_pending(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory, FakeTriliumServer() as server:
            path = pathlib.Path(directory) / "unsupported.json"
            path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "operation": "delete",
                        "idempotencyKey": "unsupported-key",
                        "createdAt": "2026-01-01T00:00:00+00:00",
                        "contentSha256": "0" * 64,
                        "payload": {},
                    }
                ),
                encoding="utf-8",
            )
            writer = KnowledgeWriter(
                TriliumClient(server.url, TEST_CREDENTIAL),
                Outbox(pathlib.Path(directory)),
            )
            result = writer.replay()
            pending_file_exists = path.is_file()

        self.assertEqual(result, {"sent": 0, "deduplicated": 0, "failed": 1})
        self.assertTrue(pending_file_exists)

    def test_replay_counts_invalid_utf8_record_as_failed(self) -> None:
        with tempfile.TemporaryDirectory() as directory, FakeTriliumServer() as server:
            path = pathlib.Path(directory) / "invalid.json"
            path.write_bytes(b"\xff")
            writer = KnowledgeWriter(
                TriliumClient(server.url, TEST_CREDENTIAL),
                Outbox(pathlib.Path(directory)),
            )
            result = writer.replay()

        self.assertEqual(result, {"sent": 0, "deduplicated": 0, "failed": 1})


class CliTests(unittest.TestCase):
    def test_help_lists_required_commands_and_full_kind_set(self) -> None:
        stdout = io.StringIO()
        with (
            self.assertRaises(SystemExit) as raised,
            contextlib.redirect_stdout(stdout),
        ):
            main(["--help"])
        self.assertEqual(raised.exception.code, 0)
        for command in ("search", "read", "create", "append", "replay"):
            self.assertIn(command, stdout.getvalue())

        create_stdout = io.StringIO()
        with (
            self.assertRaises(SystemExit) as create_exit,
            contextlib.redirect_stdout(create_stdout),
        ):
            main(["create", "--help"])
        self.assertEqual(create_exit.exception.code, 0)
        self.assertIn("moc", create_stdout.getvalue())

    def test_create_reports_queued_with_actor_and_language_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory, unavailable_url() as url:
            root = pathlib.Path(directory)
            html_file = root / "note.html"
            html_file.write_text("<h1>Result</h1><p>Offline.</p>", encoding="utf-8")
            outbox = root / "outbox"
            env = {
                **os.environ,
                "PYTHONPATH": str(PROJECT_ROOT / "src"),
                "TRILIUM_URL": url,
                "TRILIUM_ETAPI_TOKEN": TEST_CREDENTIAL,
                "AGENT_KB_OUTBOX": str(outbox),
            }
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "trilium_tool",
                    "create",
                    "--parent",
                    "PARENT_NOTE_ID",
                    "--title",
                    "Offline example",
                    "--kind",
                    "moc",
                    "--html-file",
                    str(html_file),
                ],
                cwd=PROJECT_ROOT,
                env=env,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            records = list(outbox.glob("*.json"))
            record = json.loads(records[0].read_text(encoding="utf-8"))

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(completed.stdout.startswith("KB: QUEUED path="))
        self.assertNotIn(TEST_CREDENTIAL, completed.stdout + completed.stderr)
        self.assertEqual(len(records), 1)
        self.assertEqual(record["payload"]["labels"]["createdBy"], "agent")
        self.assertEqual(record["payload"]["labels"]["language"], "en")

    def test_duplicate_append_cli_stays_in_synced_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory, FakeTriliumServer() as server:
            FakeTriliumHandler.contents["note_demo"] += (
                '\n<section data-trilium-tool-marker="cli-key"><p>Existing.</p></section>'
            )
            root = pathlib.Path(directory)
            html_file = root / "fragment.html"
            html_file.write_text("<p>Duplicate.</p>", encoding="utf-8")
            env = {
                **os.environ,
                "PYTHONPATH": str(PROJECT_ROOT / "src"),
                "TRILIUM_URL": server.url,
                "TRILIUM_ETAPI_TOKEN": TEST_CREDENTIAL,
                "AGENT_KB_OUTBOX": str(root / "outbox"),
            }
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "trilium_tool",
                    "append",
                    "note_demo",
                    "--html-file",
                    str(html_file),
                    "--marker",
                    "cli-key",
                ],
                cwd=PROJECT_ROOT,
                env=env,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            completed.stdout.strip(), "KB: SYNCED noteId=note_demo unchanged=true"
        )
        self.assertNotIn(TEST_CREDENTIAL, completed.stdout + completed.stderr)
        self.assertEqual(FakeTriliumHandler.put_count, 0)


if __name__ == "__main__":
    unittest.main()
