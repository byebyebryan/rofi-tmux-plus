from __future__ import annotations

import hashlib
import json
import re
import subprocess
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from rofi_tmux_plus import cli
from rofi_tmux_plus.bounded_process import BoundedCompleted
from rofi_tmux_plus.errors import ContractError
from rofi_tmux_plus.host import local_host
from rofi_tmux_plus.mesh_adapter import HostMeshAdapter, _parse_snapshot
from rofi_tmux_plus.wire import WireError, decode_document

BUNDLE = Path(__file__).parents[1] / "contracts" / "tmux-session-v1"
SCHEMAS = BUNDLE / "schemas"
FIXTURES = BUNDLE / "fixtures"
HOST_BUNDLE = Path(__file__).parents[1] / "contracts" / "host-mesh-v1"
SCHEMA_URI = "https://json-schema.org/draft/2020-12/schema"


class SchemaValidationError(AssertionError):
    pass


class LocalSchemaValidator:
    """Test-only validator for the local Draft 2020-12 subset."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.documents = {
            path.resolve(): json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(root.glob("*.schema.json"))
        }

    def validate(self, instance: object, schema_name: str) -> None:
        path = (self.root / schema_name).resolve()
        self._validate(instance, self.documents[path], path, "$", None)

    def _resolve(self, base: Path, reference: str) -> tuple[Path, object]:
        if reference.startswith("#"):
            path = base
            fragment = reference[1:]
        else:
            document, separator, fragment = reference.partition("#")
            if not separator or document.startswith(("http://", "https://")):
                raise SchemaValidationError(f"non-local schema ref: {reference}")
            path = (base.parent / document).resolve()
        if path not in self.documents:
            raise SchemaValidationError(f"missing schema ref: {reference}")
        value: object = self.documents[path]
        if fragment:
            if not fragment.startswith("/"):
                raise SchemaValidationError(f"unsupported schema fragment: {reference}")
            for token in fragment[1:].split("/"):
                token = token.replace("~1", "/").replace("~0", "~")
                if not isinstance(value, dict) or token not in value:
                    raise SchemaValidationError(f"missing schema pointer: {reference}")
                value = value[token]
        return path, value

    @staticmethod
    def _type_matches(instance: object, expected: str) -> bool:
        if expected == "object":
            return isinstance(instance, dict)
        if expected == "array":
            return isinstance(instance, list)
        if expected == "string":
            return isinstance(instance, str)
        if expected == "boolean":
            return isinstance(instance, bool)
        if expected == "integer":
            return isinstance(instance, int) and not isinstance(instance, bool)
        if expected == "number":
            return isinstance(instance, (int, float)) and not isinstance(instance, bool)
        if expected == "null":
            return instance is None
        return False

    @staticmethod
    def _json_equal(left: object, right: object) -> bool:
        if isinstance(left, bool) or isinstance(right, bool):
            return type(left) is type(right) and left == right
        return left == right

    def _validate(
        self,
        instance: object,
        schema: object,
        base: Path,
        location: str,
        property_name: str | None,
    ) -> None:
        if not isinstance(schema, dict):
            raise SchemaValidationError(f"{location}: schema is not an object")
        if "$ref" in schema:
            ref_base, target = self._resolve(base, schema["$ref"])
            self._validate(instance, target, ref_base, location, property_name)
        for branch in schema.get("allOf", []):
            self._validate(instance, branch, base, location, property_name)
        for keyword in ("anyOf", "oneOf"):
            if keyword not in schema:
                continue
            successes = 0
            for branch in schema[keyword]:
                try:
                    self._validate(instance, branch, base, location, property_name)
                except SchemaValidationError:
                    continue
                successes += 1
            if (keyword == "anyOf" and successes < 1) or (keyword == "oneOf" and successes != 1):
                raise SchemaValidationError(f"{location}: {keyword} failed")
        if "not" in schema:
            try:
                self._validate(instance, schema["not"], base, location, property_name)
            except SchemaValidationError:
                pass
            else:
                raise SchemaValidationError(f"{location}: not failed")
        if "const" in schema and not self._json_equal(instance, schema["const"]):
            raise SchemaValidationError(f"{location}: const mismatch")
        if "enum" in schema and not any(
            self._json_equal(instance, item) for item in schema["enum"]
        ):
            raise SchemaValidationError(f"{location}: enum mismatch")
        if "type" in schema:
            expected = schema["type"]
            expected_types = [expected] if isinstance(expected, str) else expected
            if not any(self._type_matches(instance, item) for item in expected_types):
                raise SchemaValidationError(f"{location}: type mismatch")
        if isinstance(instance, str):
            if len(instance) < schema.get("minLength", 0):
                raise SchemaValidationError(f"{location}: string too short")
            if len(instance) > schema.get("maxLength", 2**63 - 1):
                raise SchemaValidationError(f"{location}: string too long")
            pattern = schema.get("pattern")
            if pattern is not None and re.search(pattern, instance) is None:
                raise SchemaValidationError(f"{location}: pattern mismatch")
        if isinstance(instance, (int, float)) and not isinstance(instance, bool):
            if "minimum" in schema and instance < schema["minimum"]:
                raise SchemaValidationError(f"{location}: below minimum")
            if "maximum" in schema and instance > schema["maximum"]:
                raise SchemaValidationError(f"{location}: above maximum")
        if isinstance(instance, list):
            if len(instance) < schema.get("minItems", 0):
                raise SchemaValidationError(f"{location}: too few items")
            if len(instance) > schema.get("maxItems", 2**63 - 1):
                raise SchemaValidationError(f"{location}: too many items")
            if "items" in schema:
                for index, child in enumerate(instance):
                    self._validate(child, schema["items"], base, f"{location}[{index}]", None)
        if isinstance(instance, dict):
            for name in schema.get("required", []):
                if name not in instance:
                    raise SchemaValidationError(f"{location}: missing {name}")
            if "propertyNames" in schema:
                for name in instance:
                    self._validate(name, schema["propertyNames"], base, f"{location}.{name}", name)
            properties = schema.get("properties", {})
            for name, child in properties.items():
                if name in instance:
                    self._validate(instance[name], child, base, f"{location}.{name}", name)
            additional = schema.get("additionalProperties")
            if isinstance(additional, dict):
                for name, child in instance.items():
                    if name not in properties:
                        self._validate(child, additional, base, f"{location}.{name}", name)
            elif additional is False:
                unknown = set(instance) - set(properties)
                if unknown:
                    raise SchemaValidationError(f"{location}: unknown {min(unknown)}")


def _assert_inventory_semantics(value: object) -> None:
    if not isinstance(value, dict) or not isinstance(value.get("hosts"), list):
        raise TypeError("inventory response is not an object with hosts")
    hosts = value["hosts"]
    if not hosts or len(hosts) > 128:
        raise AssertionError("too many hosts")
    identities: set[str] = set()
    for host in hosts:
        if not isinstance(host, dict):
            raise TypeError("host row is not an object")
        host_id = host.get("hostId")
        if not isinstance(host_id, str) or host_id.casefold() in identities:
            raise AssertionError("duplicate host identity")
        identities.add(host_id.casefold())
        sessions = host.get("sessions")
        if not isinstance(sessions, list) or len(sessions) > 256:
            raise AssertionError("invalid session count")
        pane_count = 0
        for session in sessions:
            if not isinstance(session, dict) or session.get("hostId") != host_id:
                raise AssertionError("session identity does not match host")
            panes = session.get("panes", [])
            if not isinstance(panes, list):
                raise TypeError("invalid panes")
            pane_count += len(panes)
            if len(panes) > 512:
                raise AssertionError("invalid per-session pane count")
        if pane_count > 512:
            raise AssertionError("invalid per-host pane count")
        status = host.get("status")
        if status == "ok" and "error" in host:
            raise AssertionError("successful host row carries an error")
        if status != "ok" and (sessions or not isinstance(host.get("error"), dict)):
            raise AssertionError("failed host row is not partial data")


_SESSION_FIELDS = {
    "hostId",
    "serverGeneration",
    "sessionId",
    "createdAt",
    "name",
    "activityAt",
    "lastAttachedAt",
    "attachedClients",
    "pending",
    "windowCount",
    "sessionPath",
    "currentWindow",
    "currentPath",
}


def _assert_error_semantics(value: object) -> None:
    if not isinstance(value, dict) or value.get("ok") is not False:
        raise AssertionError("error response is not a failed envelope")
    error = value.get("error")
    if not isinstance(error, dict) or not isinstance(error.get("code"), str):
        raise TypeError("error response has no typed error")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", error["code"], re.ASCII):
        raise AssertionError("error code is not an ASCII token")
    if not isinstance(error.get("message"), str) or not error["message"]:
        raise AssertionError("error response has no message")


def _assert_session_semantics(value: object) -> None:
    if not isinstance(value, dict) or not _SESSION_FIELDS.issubset(value):
        raise AssertionError("lifecycle response does not contain a complete session")
    if not isinstance(value["hostId"], str) or not isinstance(value["serverGeneration"], str):
        raise TypeError("session identity is incomplete")
    if not isinstance(value["sessionId"], str) or not re.fullmatch(r"\$[0-9]+", value["sessionId"]):
        raise AssertionError("session ID is not a tmux ID")
    if not isinstance(value["createdAt"], int) or isinstance(value["createdAt"], bool):
        raise TypeError("session creation time is not an integer")
    if not isinstance(value["pending"], bool):
        raise TypeError("session pending state is not boolean")


def _assert_lifecycle_semantics(value: object, schema_name: str) -> None:
    if not isinstance(value, dict) or value.get("ok") is not True:
        raise AssertionError("lifecycle response is not a success envelope")
    if schema_name == "kill.schema.json":
        reference = value.get("reference")
        if not isinstance(reference, dict) or not {
            "hostId",
            "serverGeneration",
            "sessionId",
            "createdAt",
        }.issubset(reference):
            raise AssertionError("kill response has no complete reference")
        if not isinstance(value.get("observedClients"), int) or isinstance(
            value["observedClients"], bool
        ):
            raise AssertionError("kill response has no client count")
        return
    _assert_session_semantics(value.get("session"))
    if schema_name == "open.schema.json":
        if (
            type(value.get("focused")) is not bool
            or type(value.get("terminalLaunched")) is not bool
        ):
            raise AssertionError("open response has no focus result")
        if value["focused"] == value["terminalLaunched"]:
            raise AssertionError("open response focus results are not exclusive")
    elif ("focused" in value) != ("terminalLaunched" in value):
        raise AssertionError("lifecycle focus results are not paired")


def _assert_base_envelope_semantics(value: object) -> None:
    if not isinstance(value, dict) or type(value.get("schemaVersion")) is not int:
        raise AssertionError("base envelope has no integer schema version")
    if type(value.get("ok")) is not bool:
        raise AssertionError("base envelope has no boolean result")


class ContractBundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.validator = LocalSchemaValidator(SCHEMAS)

    def test_schema_documents_are_draft_2020_12_and_refs_local(self) -> None:
        for path in sorted(SCHEMAS.glob("*.schema.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(document["$schema"], SCHEMA_URI)

            def walk(value: object) -> None:
                if isinstance(value, dict):
                    if "$ref" in value:
                        self.assertFalse(value["$ref"].startswith(("http://", "https://")))
                    for child in value.values():
                        walk(child)
                elif isinstance(value, list):
                    for child in value:
                        walk(child)

            walk(document)
        self.assertEqual(
            {path.name for path in SCHEMAS.glob("*.schema.json")},
            {
                "common.schema.json",
                "error.schema.json",
                "inventory.schema.json",
                "open.schema.json",
                "create.schema.json",
                "rename.schema.json",
                "kill.schema.json",
            },
        )

    def test_index_covers_every_valid_and_raw_fixture(self) -> None:
        index = json.loads((FIXTURES / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(index["contract"], "tmux-session-v1")
        indexed: set[str] = set()
        for case in index["cases"]:
            for field in ("command", "fixture", "kind", "schema", "expectedExit", "expected"):
                self.assertIn(field, case)
            path = FIXTURES / case["fixture"]
            self.assertTrue(path.is_file(), case["fixture"])
            indexed.add(case["fixture"])
            self.assertIn(case["expected"], ("accept", "reject"))
            if case["kind"] == "document":
                value = decode_document(path.read_bytes(), limit=1 << 20)
                self.validator.validate(value, case["schema"].removeprefix("schemas/"))
                if case["schema"] == "schemas/inventory.schema.json":
                    _assert_inventory_semantics(value)
            else:
                self.assertEqual(case["kind"], "raw")
                self.assertEqual(
                    set(case["appliesTo"]), {"inventory", "open", "create", "rename", "kill"}
                )
                with self.assertRaises(WireError):
                    decode_document(path.read_bytes(), limit=1 << 20)
        self.assertEqual(
            {
                path.relative_to(FIXTURES).as_posix()
                for directory in (FIXTURES / "valid", FIXTURES / "invalid")
                for path in directory.iterdir()
                if path.is_file()
            },
            {name for name in indexed if name.startswith(("valid/", "invalid/"))},
        )

    def test_supporting_fixture_responses_match_mapped_schemas_and_semantics(self) -> None:
        index = json.loads((FIXTURES / "index.json").read_text(encoding="utf-8"))
        for entry in index["supportingFixtures"]:
            if entry["kind"] not in {"semantic", "envelope-catalog"}:
                continue
            self.assertIn("responses", entry, entry["fixture"])
            fixture = json.loads((FIXTURES / entry["fixture"]).read_text(encoding="utf-8"))
            seen_paths: set[str] = set()
            for mapping in entry["responses"]:
                path = mapping["path"]
                self.assertNotIn(path, seen_paths, entry["fixture"])
                seen_paths.add(path)
                self.assertIn(mapping["expectedExit"], {"zero", "nonzero"})
                value: object = fixture
                for part in path.split("."):
                    self.assertIsInstance(value, dict, f"{entry['fixture']}: {path}")
                    assert isinstance(value, dict)
                    self.assertIn(part, value, f"{entry['fixture']}: {path}")
                    value = value[part]
                schema = mapping["schema"]
                self.validator.validate(value, schema.removeprefix("schemas/"))
                if mapping["expectedExit"] == "zero":
                    self.assertIsInstance(value, dict)
                    assert isinstance(value, dict)
                    if schema == "schemas/inventory.schema.json":
                        _assert_inventory_semantics(value)
                    elif schema in {
                        "schemas/open.schema.json",
                        "schemas/create.schema.json",
                        "schemas/rename.schema.json",
                        "schemas/kill.schema.json",
                    }:
                        _assert_lifecycle_semantics(value, schema.removeprefix("schemas/"))
                    elif schema == "schemas/common.schema.json":
                        _assert_base_envelope_semantics(value)
                else:
                    _assert_error_semantics(value)

    def test_unknown_fields_and_typed_error_codes_are_allowed(self) -> None:
        document = json.loads((FIXTURES / "valid/inventory-no-server.json").read_text())
        document["future"] = {"producer": "extension"}
        document["hosts"][0]["futureHost"] = True
        self.validator.validate(document, "inventory.schema.json")
        self.validator.validate(
            {
                "schemaVersion": 1,
                "ok": False,
                "error": {"code": "future_code_7", "message": "later"},
            },
            "error.schema.json",
        )
        lifecycle = json.loads((FIXTURES / "valid/open-success.json").read_text())
        lifecycle["future"] = {"nested": True}
        lifecycle["session"]["futureSession"] = ["extension"]
        self.validator.validate(lifecycle, "open.schema.json")
        inventory = json.loads((FIXTURES / "valid/inventory-multiple-sessions.json").read_text())
        inventory["hosts"][0]["sessions"][0]["options"] = {"@future_option": "accepted"}
        self.validator.validate(inventory, "inventory.schema.json")

    def test_known_bool_and_integer_fields_reject_cross_types(self) -> None:
        document = json.loads((FIXTURES / "valid/inventory-no-server.json").read_text())
        document["generatedAt"] = True
        with self.assertRaises(SchemaValidationError):
            self.validator.validate(document, "inventory.schema.json")
        document["generatedAt"] = 1
        document["hosts"][0]["local"] = 1
        with self.assertRaises(SchemaValidationError):
            self.validator.validate(document, "inventory.schema.json")
        report = json.loads((FIXTURES / "valid/open-success.json").read_text())
        report["focused"] = 1
        with self.assertRaises(SchemaValidationError):
            self.validator.validate(report, "open.schema.json")

    def test_bundle_checksums_are_complete_sorted_and_define_digest(self) -> None:
        checksum = BUNDLE / "SHA256SUMS"
        raw = checksum.read_bytes()
        entries: list[tuple[str, str]] = []
        pattern = re.compile(rb"^([0-9a-f]{64})  ([^\x00\r\n]+)\n$")
        for line in raw.splitlines(keepends=True):
            match = pattern.fullmatch(line)
            self.assertIsNotNone(match, line)
            assert match is not None
            entries.append((match.group(2).decode(), match.group(1).decode()))
        names = [name for name, _digest in entries]
        self.assertEqual(names, sorted(names, key=lambda name: name.encode()))
        self.assertEqual(len(names), len(set(names)))
        actual = sorted(
            (
                path.relative_to(BUNDLE).as_posix()
                for path in BUNDLE.rglob("*")
                if path.is_file() and path.name != "SHA256SUMS"
            ),
            key=lambda name: name.encode(),
        )
        self.assertEqual(names, actual)
        for name, digest in entries:
            self.assertEqual(hashlib.sha256((BUNDLE / name).read_bytes()).hexdigest(), digest)
        self.assertRegex(hashlib.sha256(raw).hexdigest(), r"^[0-9a-f]{64}$")

    def test_host_mesh_released_provenance_is_exact_and_history_is_not_vendored(self) -> None:
        source = json.loads((HOST_BUNDLE / "SOURCE.json").read_text(encoding="utf-8"))
        self.assertEqual(source["contract"], "host-mesh-v1")
        self.assertEqual(
            source["upstreamRepository"], "https://github.com/byebyebryan/rofi-ssh-plus"
        )
        self.assertEqual(source["sourceState"], "released")
        self.assertRegex(source["sourceCommit"], r"^[0-9a-f]{40}$")
        digest = "sha256:" + hashlib.sha256((HOST_BUNDLE / "SHA256SUMS").read_bytes()).hexdigest()
        self.assertEqual(source["bundleDigest"], digest)
        self.assertFalse(
            any(path.name == "history-migration.json" for path in HOST_BUNDLE.rglob("*"))
        )

    def test_vendored_host_bundle_is_complete_and_valid_for_invoked_commands(self) -> None:
        checksum = HOST_BUNDLE / "SHA256SUMS"
        entries = {
            line.split(b"  ", 1)[1].decode().rstrip("\n"): line.split(b"  ", 1)[0].decode()
            for line in checksum.read_bytes().splitlines(keepends=True)
        }
        self.assertEqual(
            set(entries),
            {
                path.relative_to(HOST_BUNDLE).as_posix()
                for path in HOST_BUNDLE.rglob("*")
                if path.is_file() and path.name not in {"SHA256SUMS", "SOURCE.json"}
            },
        )
        for name, digest in entries.items():
            self.assertEqual(hashlib.sha256((HOST_BUNDLE / name).read_bytes()).hexdigest(), digest)
        list_raw = (HOST_BUNDLE / "fixtures/valid/list-local-only.json").read_bytes()
        decode_document(list_raw, limit=512 * 1024)
        adapter = HostMeshAdapter(
            which=lambda _name: "/fake",
            runner=lambda *_args, **_kwargs: subprocess.CompletedProcess(
                ["fake"], 0, list_raw, b""
            ),
        )
        self.assertEqual(adapter.load().local_host_id, "alpha")
        report_paths = sorted((HOST_BUNDLE / "fixtures/valid").glob("report-*.json"))
        self.assertEqual(
            {path.name for path in report_paths},
            {"report-accepted.json", "report-stale-observation.json"},
        )
        for path in report_paths:
            raw = path.read_bytes()
            decode_document(raw, limit=512 * 1024)
            adapter = HostMeshAdapter(
                which=lambda _name: "/fake",
                runner=lambda *_args, raw=raw, **_kwargs: subprocess.CompletedProcess(
                    ["fake"], 0, raw, b""
                ),
            )
            self.assertIsInstance(
                adapter.report_route(
                    host_id="beta",
                    route="beta-vpn.test",
                    status="reachable",
                    mesh_revision="sha256:" + "a" * 64,
                    observed_at=1,
                ),
                bool,
            )

    def test_vendored_host_index_cases_are_exercised_at_adapter_boundary(self) -> None:
        validator = LocalSchemaValidator(HOST_BUNDLE / "schemas")
        index = json.loads((HOST_BUNDLE / "fixtures/index.json").read_text(encoding="utf-8"))
        for case in index["cases"]:
            if case["kind"] != "document":
                continue
            path = HOST_BUNDLE / "fixtures" / case["fixture"]
            payload = decode_document(path.read_bytes(), limit=512 * 1024)
            validator.validate(payload, case["schema"].removeprefix("schemas/"))
            raw = path.read_bytes()
            returncode = 0 if case["expectedExit"] == "zero" else 1
            response = subprocess.CompletedProcess(["fake"], returncode, raw, b"")
            adapter = HostMeshAdapter(
                which=lambda _name: "/fake",
                runner=lambda *_args, response=response, **_kwargs: response,
            )
            if case["schema"] == "schemas/list.schema.json":
                if returncode == 0:
                    self.assertIsNotNone(adapter.load(), case["name"])
                else:
                    with self.assertRaises(ContractError) as raised:
                        adapter.load()
                    self.assertEqual(raised.exception.code, payload["error"]["code"])
            else:
                if returncode == 0:
                    accepted = adapter.report_route(
                        host_id="beta",
                        route="beta-vpn.test",
                        status="reachable",
                        mesh_revision="sha256:" + "a" * 64,
                        observed_at=1,
                    )
                    self.assertEqual(accepted, payload["accepted"], case["name"])
                else:
                    with self.assertRaises(ContractError) as raised:
                        adapter.report_route(
                            host_id="beta",
                            route="beta-vpn.test",
                            status="reachable",
                            mesh_revision="sha256:" + "a" * 64,
                            observed_at=1,
                        )
                    self.assertEqual(raised.exception.code, payload["error"]["code"])

    def test_local_identity_matrix_is_deterministic(self) -> None:
        matrix = json.loads((FIXTURES / "local-identity-matrix.json").read_text())
        for case in matrix["cases"]:
            with self.subTest(hostname=case["hostname"]):
                result = local_host(case["hostname"])
                self.assertEqual(result.host_id, case["hostId"])
                self.assertEqual(result.display, case["display"])
                self.assertEqual(set(result.aliases), set(case["aliases"]))

    def test_public_bundle_has_no_private_names(self) -> None:
        forbidden = (b"/home/bryan", b"dankmaterialshell", b".ssh/")
        for path in BUNDLE.rglob("*"):
            if not path.is_file() or path.name == "SHA256SUMS":
                continue
            lowered = path.read_bytes().lower()
            for value in forbidden:
                self.assertNotIn(value, lowered, path)


class WireAndProducerTests(unittest.TestCase):
    def _bytes_main(
        self, argv: list[str], service: object, *, lifecycle: bool = False
    ) -> tuple[int, bytes]:
        output = BytesIO()
        target = (
            "rofi_tmux_plus.cli._lifecycle"
            if lifecycle
            else "rofi_tmux_plus.cli._inventory_service"
        )
        with patch(target, return_value=service), patch("sys.stdout", output):
            status = cli.main(argv)
        return status, output.getvalue()

    @staticmethod
    def _host_row() -> dict[str, object]:
        return {
            "hostId": "local",
            "display": "Local",
            "local": True,
            "status": "ok",
            "observedAt": 1,
            "nativeHostname": "local.example",
            "serverGeneration": None,
            "route": None,
            "sessions": [],
        }

    def test_producer_emits_exact_one_lf_and_exit_consistent_success(self) -> None:
        service = type(
            "Inventory",
            (),
            {
                "inventory": lambda _self, **_kwargs: {
                    "schemaVersion": 1,
                    "generatedAt": 1,
                    "meshRevision": None,
                    "hosts": [self._host_row()],
                }
            },
        )()
        status, raw = self._bytes_main(["inventory", "--json"], service)
        self.assertEqual(status, 0)
        self.assertEqual(raw.count(b"\n"), 1)
        self.assertTrue(raw.endswith(b"\n"))
        LocalSchemaValidator(SCHEMAS).validate(
            decode_document(raw, limit=1 << 20), "inventory.schema.json"
        )

    def test_every_lifecycle_success_schema_is_emitted_and_errors_match_exit(self) -> None:
        values = {
            command: json.loads((FIXTURES / f"valid/{name}.json").read_text())
            for command, name in (
                ("open", "open-success"),
                ("create", "create-default"),
                ("rename", "rename-success"),
                ("kill", "kill-success"),
            )
        }
        lifecycle = type(
            "Lifecycle",
            (),
            {
                command: (lambda _self, *args, value=value, **_kwargs: value)
                for command, value in values.items()
            },
        )()
        for command, schema in (
            ("open", "open.schema.json"),
            ("create", "create.schema.json"),
            ("rename", "rename.schema.json"),
            ("kill", "kill.schema.json"),
        ):
            args = [command, "--json", "--host", "local"]
            if command != "create":
                args.extend(
                    ["--server-generation", "generation", "--session-id", "$0", "--created-at", "1"]
                )
            else:
                args.extend(["--name", "session"])
            if command in {"rename", "kill"}:
                args.extend(["--expected-name", "session"])
            if command == "rename":
                args.extend(["--name", "renamed"])
            status, raw = self._bytes_main(args, lifecycle, lifecycle=True)
            self.assertEqual(status, 0, command)
            LocalSchemaValidator(SCHEMAS).validate(decode_document(raw, limit=256 * 1024), schema)

        class Failing:
            def open(self, *_args: object, **_kwargs: object) -> object:
                raise cli.ContractError("stale_session", "changed")

        status, raw = self._bytes_main(
            [
                "open",
                "--json",
                "--host",
                "local",
                "--server-generation",
                "generation",
                "--session-id",
                "$0",
                "--created-at",
                "1",
            ],
            Failing(),
            lifecycle=True,
        )
        self.assertNotEqual(status, 0)
        value = decode_document(raw, limit=256 * 1024)
        self.assertFalse(value["ok"])
        self.assertEqual(value["error"]["code"], "stale_session")
        LocalSchemaValidator(SCHEMAS).validate(value, "error.schema.json")

    def test_parser_help_is_typed_and_does_not_leak_stderr(self) -> None:
        output = BytesIO()
        with patch("sys.stdout", output), patch("sys.stderr", BytesIO()):
            status = cli.main(["inventory", "--json", "--help"])
        self.assertNotEqual(status, 0)
        value = decode_document(output.getvalue(), limit=1 << 20)
        self.assertEqual(value["error"]["code"], "invalid_input")

    def test_oversized_inventory_and_aggregate_panes_become_small_typed_failure(self) -> None:
        oversized = self._host_row()
        oversized["display"] = "x" * 16_385
        service = type(
            "Inventory",
            (),
            {
                "inventory": lambda _self, **_kwargs: {
                    "schemaVersion": 1,
                    "generatedAt": 1,
                    "meshRevision": None,
                    "hosts": [oversized],
                }
            },
        )()
        status, raw = self._bytes_main(["inventory", "--json"], service)
        self.assertNotEqual(status, 0)
        self.assertLessEqual(len(raw), 1 << 20)
        self.assertEqual(decode_document(raw, limit=1 << 20)["error"]["code"], "operation_failed")

        sessions = []
        for index in range(2):
            sessions.append(
                {
                    "hostId": "local",
                    "serverGeneration": "generation",
                    "sessionId": f"${index}",
                    "createdAt": 1,
                    "name": "session",
                    "activityAt": None,
                    "lastAttachedAt": None,
                    "attachedClients": 0,
                    "pending": False,
                    "windowCount": 1,
                    "sessionPath": "/tmp",
                    "currentWindow": "shell",
                    "currentPath": "/tmp",
                    "panes": [
                        {
                            "paneId": f"%{pane}",
                            "pid": None,
                            "currentPath": "/tmp",
                            "currentCommand": "sh",
                        }
                        for pane in range(300)
                    ],
                }
            )
        row = self._host_row()
        row["serverGeneration"] = "generation"
        row["sessions"] = sessions
        service = type(
            "Inventory",
            (),
            {
                "inventory": lambda _self, **_kwargs: {
                    "schemaVersion": 1,
                    "generatedAt": 1,
                    "meshRevision": None,
                    "hosts": [row],
                }
            },
        )()
        status, raw = self._bytes_main(["inventory", "--json"], service)
        self.assertNotEqual(status, 0)
        self.assertEqual(decode_document(raw, limit=1 << 20)["error"]["code"], "operation_failed")

        lifecycle_response = json.loads((FIXTURES / "valid/rename-success.json").read_text())
        lifecycle_response["session"]["options"] = {
            f"@option_{index}": "v" * 400 for index in range(700)
        }
        lifecycle = type(
            "Lifecycle",
            (),
            {"rename": lambda _self, *args, **kwargs: lifecycle_response},
        )()
        status, raw = self._bytes_main(
            [
                "rename",
                "--json",
                "--host",
                "local",
                "--server-generation",
                "generation",
                "--session-id",
                "$0",
                "--created-at",
                "1",
                "--expected-name",
                "old",
                "--name",
                "new",
            ],
            lifecycle,
            lifecycle=True,
        )
        self.assertNotEqual(status, 0)
        self.assertLessEqual(len(raw), 256 * 1024)
        self.assertEqual(
            decode_document(raw, limit=256 * 1024)["error"]["code"], "operation_failed"
        )

    def test_consumer_distinguishes_missing_provider_and_present_broken_output(self) -> None:
        self.assertIsNone(HostMeshAdapter(which=lambda _name: None).load())
        adapter = HostMeshAdapter(
            which=lambda _name: "/fake",
            runner=lambda *_a, **_k: subprocess.CompletedProcess(["fake"], 0, b"not json", b""),
        )
        with self.assertRaises(ContractError):
            adapter.load()

    def test_consumer_rejects_all_indexed_raw_wire_cases_and_exit_mismatches(self) -> None:
        for path in sorted((HOST_BUNDLE / "fixtures/invalid").glob("*.bin")):
            response = subprocess.CompletedProcess(["fake"], 0, path.read_bytes(), b"")
            broken = HostMeshAdapter(
                which=lambda _name: "/fake", runner=lambda *_a, response=response, **_k: response
            )
            with self.subTest(path=path.name), self.assertRaises(ContractError):
                broken.load()
        good = json.loads((HOST_BUNDLE / "fixtures/valid/list-local-only.json").read_text())
        body = json.dumps(good, separators=(",", ":")).encode() + b"\n"
        for code, payload in (
            (0, {"schemaVersion": 1, "ok": False, "error": {"code": "x", "message": "bad"}}),
            (1, good),
        ):
            raw = json.dumps(payload, separators=(",", ":")).encode() + b"\n"
            response = subprocess.CompletedProcess(["fake"], code, raw, b"")
            broken = HostMeshAdapter(
                which=lambda _name: "/fake", runner=lambda *_a, response=response, **_k: response
            )
            with self.subTest(code=code), self.assertRaises(ContractError):
                broken.load()
        response = subprocess.CompletedProcess(["fake"], 0, body, b"x" * (65 * 1024))
        broken = HostMeshAdapter(which=lambda _name: "/fake", runner=lambda *_a, **_k: response)
        with self.assertRaises(ContractError):
            broken.load()

    def test_consumer_surfaces_timeout_overflow_signal_and_early_exit(self) -> None:
        valid = json.loads((HOST_BUNDLE / "fixtures/valid/list-local-only.json").read_text())
        raw = json.dumps(valid, separators=(",", ":")).encode() + b"\n"
        cases = (
            BoundedCompleted(0, raw.decode(), "", timed_out=True, stdout_bytes=raw),
            BoundedCompleted(
                0, raw.decode(), "", overflow_streams=frozenset({"stdout"}), stdout_bytes=raw
            ),
            BoundedCompleted(-15, raw.decode(), "", stdout_bytes=raw),
            BoundedCompleted(0, "", "", stdout_bytes=b""),
        )
        for completed in cases:
            adapter = HostMeshAdapter(
                which=lambda _name: "/fake", runner=lambda *_a, completed=completed, **_k: completed
            )
            with self.subTest(completed=completed), self.assertRaises(ContractError):
                adapter.load()

    def test_consumer_rejects_host_id_alias_and_route_collisions(self) -> None:
        source = json.loads((HOST_BUNDLE / "fixtures/valid/list-multi-route.json").read_text())
        for field in ("aliases", "routes"):
            document = json.loads(json.dumps(source))
            if field == "aliases":
                document["hosts"][2][field] = [document["hosts"][1]["id"]]
            else:
                document["hosts"][2][field][0]["destination"] = document["hosts"][1]["id"]
            with self.subTest(field=field), self.assertRaises(ContractError):
                _parse_snapshot(document)


if __name__ == "__main__":
    unittest.main()
