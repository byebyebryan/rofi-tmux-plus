"""Versioned JSON command-line boundary for local Tmux Session v1."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Sequence

from .config import load_config
from .errors import ContractError, clean_message
from .inventory_service import InventoryService
from .lifecycle_service import LifecycleService
from .picker_model import PickerModelService, RemoteRefresh
from .remote_cache import RemoteCache
from .tmux import validate_required_options, validate_user_option
from .wire import WireError, validate_string_bounds, write_document

_PUBLIC_JSON_COMMANDS = {"inventory", "open", "create", "rename", "kill"}
_JSON_COMMANDS = _PUBLIC_JSON_COMMANDS | {
    "_picker-model",
    "_refresh",
    "_refresh-status",
}
_ROFI_CALLBACK_ENV = {"ROFI_DATA", "ROFI_INFO", "ROFI_INPUT"}
_ERROR_CODE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$", re.ASCII)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", re.ASCII)
_REVISION = re.compile(r"^sha256:[0-9a-f]{64}$", re.ASCII)


def _is_rofi_invocation(argv: Sequence[str]) -> bool:
    """Recognize initial and callback script-mode invocations.

    Rofi calls a script without arguments initially, but passes the selected
    row as argv[0] for callbacks.  Explicit JSON commands remain available to
    processes that merely inherited Rofi's environment.
    """

    if "ROFI_RETV" not in os.environ:
        return False
    if not argv:
        return True
    # Agent Plus invokes the public JSON contract from inside its own Rofi
    # callback, so those child processes inherit ROFI_* variables.  Contract
    # commands always carry arguments; a Rofi callback supplies the selected
    # or custom row as one argv item, even when that text names a command.
    if argv[0] in _JSON_COMMANDS and len(argv) > 1:
        return False
    if _ROFI_CALLBACK_ENV.intersection(os.environ):
        return True
    return os.environ.get("ROFI_RETV", "0") != "0" and argv[0] not in _JSON_COMMANDS


class JsonArgumentParser(argparse.ArgumentParser):
    machine = False

    def error(self, message: str) -> None:
        raise ContractError("invalid_input", message)

    def _print_message(self, message: str, file: object | None = None) -> None:
        if self.machine:
            return
        super()._print_message(message, file)  # type: ignore[arg-type]

    def exit(self, status: int = 0, message: str | None = None) -> None:
        # argparse otherwise writes help/usage text to stderr, which would
        # violate the machine boundary for malformed or incomplete requests.
        if not self.machine:
            super().exit(status, message)
        raise ContractError("invalid_input", message or "invalid command line")


def _common(parser: argparse.ArgumentParser, *, reference: bool = False) -> None:
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--host")
    parser.add_argument("--mesh-revision")
    if reference:
        parser.add_argument("--server-generation", required=True)
        parser.add_argument("--session-id", required=True)
        parser.add_argument("--created-at", required=True, type=int)


def build_parser(*, machine: bool = False) -> JsonArgumentParser:
    parser = JsonArgumentParser(prog="rofi-tmux-plus", add_help=True)
    parser.machine = machine
    commands = parser.add_subparsers(dest="command", required=True)

    inventory = commands.add_parser("inventory", add_help=True)
    inventory.add_argument("--json", action="store_true")
    inventory.add_argument("--host", action="append", default=[])
    inventory.add_argument("--mesh-revision")
    inventory.add_argument("--panes", action="store_true")
    inventory.add_argument("--session-option", action="append", default=[])

    open_parser = commands.add_parser("open", add_help=True)
    _common(open_parser, reference=True)
    open_parser.add_argument("--expected-name")
    open_parser.add_argument("--require-option", action="append", default=[])

    create = commands.add_parser("create", add_help=True)
    _common(create)
    create.add_argument("--name", required=True)
    create.add_argument("--cwd")
    create.add_argument("--set-option", action="append", default=[])
    create.add_argument("--defer-until-attached", action="store_true")
    create.add_argument("--attach-timeout", type=int)
    create.add_argument("--open", action="store_true")
    create.add_argument("command_argv", nargs=argparse.REMAINDER)

    rename = commands.add_parser("rename", add_help=True)
    _common(rename, reference=True)
    rename.add_argument("--expected-name", required=True)
    rename.add_argument("--name", required=True)

    kill = commands.add_parser("kill", add_help=True)
    _common(kill, reference=True)
    kill.add_argument("--expected-name", required=True)

    # Private process boundary for the future Rofi frontend. These commands
    # deliberately do not alter the published live-inventory contract.
    model = commands.add_parser("_picker-model", add_help=True)
    model.add_argument("--json", action="store_true")
    model.add_argument("--no-refresh", action="store_true")
    refresh = commands.add_parser("_refresh", add_help=True)
    refresh.add_argument("--mesh-revision", required=True)
    refresh_status = commands.add_parser("_refresh-status", add_help=True)
    refresh_status.add_argument("--json", action="store_true")
    refresh_status.add_argument("--mesh-revision", required=True)
    for command_parser in commands.choices.values():
        command_parser.machine = machine
    return parser


def _require_json(args: argparse.Namespace) -> None:
    if not args.json:
        raise ContractError("invalid_input", "--json is required by Tmux Session Contract v1")


def _options(raw_options: Sequence[str]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for item in raw_options:
        if "=" not in item:
            raise ContractError("invalid_input", "--set-option must use @NAME=VALUE")
        name, value = item.split("=", 1)
        validate_user_option(name)
        result.append((name, value))
    return result


def _required_options(raw_options: Sequence[str]) -> tuple[tuple[str, str], ...]:
    parsed: list[tuple[str, str]] = []
    for item in raw_options:
        if "=" not in item:
            raise ContractError("invalid_input", "--require-option must use @NAME=VALUE")
        name, value = item.split("=", 1)
        parsed.append((name, value))
    return validate_required_options(parsed)


def _lifecycle() -> LifecycleService:
    return LifecycleService(load_config())


def _inventory_service() -> InventoryService:
    return InventoryService(load_config())


def _picker_model() -> PickerModelService:
    return PickerModelService(load_config())


def _refresh() -> RemoteRefresh:
    return RemoteRefresh(load_config(), RemoteCache())


def dispatch(args: argparse.Namespace) -> dict[str, object] | None:
    if args.command == "_refresh":
        _refresh().run(args.mesh_revision)
        return None
    _require_json(args)
    if args.command == "_picker-model":
        return _picker_model().load(start_refresh=not args.no_refresh).payload
    if args.command == "_refresh-status":
        return {
            "schemaVersion": 1,
            "meshRevision": args.mesh_revision,
            "refresh": _refresh().status(args.mesh_revision),
        }
    if args.command == "inventory":
        options = [validate_user_option(option) for option in args.session_option]
        return _inventory_service().inventory(
            requested_hosts=args.host,
            mesh_revision=args.mesh_revision,
            panes=args.panes,
            option_names=options,
        )
    lifecycle = _lifecycle()
    if args.host is None:
        raise ContractError("invalid_input", "--host is required")
    if args.command == "open":
        return lifecycle.open(
            args.host,
            args.mesh_revision,
            args.server_generation,
            args.session_id,
            args.created_at,
            args.expected_name,
            _required_options(args.require_option),
        )
    if args.command == "create":
        command = list(args.command_argv)
        if command and command[0] == "--":
            command = command[1:]
        return lifecycle.create(
            args.host,
            args.mesh_revision,
            args.name,
            args.cwd,
            _options(args.set_option),
            command,
            args.defer_until_attached,
            args.attach_timeout,
            args.open,
        )
    if args.command == "rename":
        return lifecycle.rename(
            args.host,
            args.mesh_revision,
            args.server_generation,
            args.session_id,
            args.created_at,
            args.expected_name,
            args.name,
        )
    if args.command == "kill":
        return lifecycle.kill(
            args.host,
            args.mesh_revision,
            args.server_generation,
            args.session_id,
            args.created_at,
            args.expected_name,
        )
    raise ContractError("invalid_input", "unknown command")


def main(argv: Sequence[str] | None = None) -> int:
    # Keep the versioned JSON CLI usable when a caller merely inherited
    # Rofi's environment while honoring Rofi's selected-row callback argv.
    if argv is None:
        argv = sys.argv[1:]
    if _is_rofi_invocation(argv):
        from .rofi import run_rofi

        return run_rofi()
    machine = bool(argv and argv[0] in _PUBLIC_JSON_COMMANDS and "--json" in argv)
    parser = build_parser(machine=machine)
    try:
        args = parser.parse_args(argv)
        result = dispatch(args)
    except ContractError as error:
        _emit_error(error, _output_limit(argv))
        return 2
    except BrokenPipeError:
        return 1
    except Exception as error:  # noqa: BLE001 - defensive JSON process boundary
        failure = ContractError("operation_failed", clean_message(error))
        _emit_error(failure, _output_limit(argv))
        return 1
    if result is not None:
        if args.command in _PUBLIC_JSON_COMMANDS:
            try:
                _validate_public_result(args.command, result)
                write_document(sys.stdout, result, limit=_output_limit(argv))
            except (BrokenPipeError, WireError) as error:
                if isinstance(error, BrokenPipeError):
                    return 1
                _emit_error(
                    ContractError("operation_failed", clean_message(error)),
                    _output_limit(argv),
                )
                return 1
        else:
            print(json.dumps(result, separators=(",", ":"), ensure_ascii=False))
    return 0


def _output_limit(argv: Sequence[str]) -> int:
    command = argv[0] if argv else ""
    return 1 * 1024 * 1024 if command == "inventory" else 256 * 1024


def _validate_public_result(command: str, result: object) -> None:
    if (
        not isinstance(result, dict)
        or type(result.get("schemaVersion")) is not int
        or result.get("schemaVersion") != 1
    ):
        raise WireError("producer returned an invalid schema version")
    if command == "inventory":
        if "ok" in result:
            raise WireError("inventory response must not carry an ok field")
        _require_fields(result, "generatedAt", "meshRevision", "hosts")
        if not _integer_field(result.get("generatedAt"), 0, 2**63 - 1):
            raise WireError("inventory timestamp is invalid")
        _revision_field(result.get("meshRevision"))
        hosts = result.get("hosts")
        if not isinstance(hosts, list) or not hosts or len(hosts) > 128:
            raise WireError("inventory host count exceeded its limit")
        host_ids: set[str] = set()
        for host in hosts:
            if not isinstance(host, dict):
                raise WireError("inventory host row is invalid")
            _validate_host_row(host)
            host_id = host["hostId"].casefold()
            if host_id in host_ids:
                raise WireError("inventory host identities are ambiguous")
            host_ids.add(host_id)
            sessions = host.get("sessions")
            if not isinstance(sessions, list) or len(sessions) > 256:
                raise WireError("inventory session count exceeded its limit")
            pane_count = 0
            for session in sessions:
                if not isinstance(session, dict):
                    raise WireError("inventory session row is invalid")
                _validate_session(session, limit=16_384)
                if session["hostId"] != host["hostId"]:
                    raise WireError("inventory session host identity is invalid")
                panes = session.get("panes")
                if panes is not None and (not isinstance(panes, list) or len(panes) > 512):
                    raise WireError("inventory pane count exceeded its limit")
                if panes is not None:
                    for pane in panes:
                        _validate_pane(pane, limit=16_384)
                    pane_count += len(panes)
            if pane_count > 512:
                raise WireError("inventory pane count exceeded its host limit")
        validate_string_bounds(result, limit=16_384)
        return
    _require_fields(result, "ok", "meshRevision")
    if result.get("ok") is not True:
        raise WireError("lifecycle success response must have ok=true")
    _revision_field(result.get("meshRevision"))
    if command == "kill":
        _require_fields(result, "reference", "observedClients")
        reference = result.get("reference")
        if not isinstance(reference, dict):
            raise WireError("kill reference is invalid")
        _validate_reference(reference, limit=4_096)
        if not _integer_field(result.get("observedClients"), 0, 2**31 - 1):
            raise WireError("observed client count is invalid")
    else:
        _require_fields(result, "session")
        session = result.get("session")
        if not isinstance(session, dict):
            raise WireError("lifecycle session is invalid")
        _validate_session(session, limit=4_096)
        has_focus = "focused" in result
        has_launch = "terminalLaunched" in result
        if command == "open" and not has_focus:
            raise WireError("open response must include focused")
        if command == "open" and not has_launch:
            raise WireError("open response must include terminalLaunched")
        if has_focus != has_launch:
            raise WireError("lifecycle focus and launch fields must be paired")
        if has_focus:
            if type(result["focused"]) is not bool or type(result["terminalLaunched"]) is not bool:
                raise WireError("lifecycle focus fields are invalid")
            if result["focused"] == result["terminalLaunched"]:
                raise WireError("lifecycle focus fields must be exclusive")
    validate_string_bounds(result, limit=4_096)


def _require_fields(value: dict[str, object], *names: str) -> None:
    if any(name not in value for name in names):
        raise WireError("producer response is missing a required field")


def _integer_field(value: object, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _revision_field(value: object) -> None:
    if value is not None and (not isinstance(value, str) or _REVISION.fullmatch(value) is None):
        raise WireError("mesh revision is invalid")


def _string_field(value: object, *, nullable: bool, limit: int) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str) or not value or len(value) > limit:
        raise WireError("response string field is invalid")


def _validate_reference(reference: dict[str, object], *, limit: int) -> None:
    _require_fields(reference, "hostId", "serverGeneration", "sessionId", "createdAt")
    _string_field(reference.get("hostId"), nullable=False, limit=limit)
    if _IDENTIFIER.fullmatch(reference["hostId"]) is None:  # type: ignore[arg-type]
        raise WireError("reference host ID is invalid")
    _generation_field(reference.get("serverGeneration"), limit=limit)
    _string_field(reference.get("sessionId"), nullable=False, limit=4_096)
    if not re.fullmatch(r"\$[0-9]+", reference["sessionId"]):  # type: ignore[arg-type]
        raise WireError("reference session ID is invalid")
    if not _integer_field(reference.get("createdAt"), 0, 2**63 - 1):
        raise WireError("reference timestamp is invalid")


def _validate_session(session: dict[str, object], *, limit: int) -> None:
    _require_fields(
        session,
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
    )
    _validate_reference(session, limit=limit)
    _string_field(session.get("name"), nullable=True, limit=limit)
    for key in ("activityAt", "lastAttachedAt"):
        value = session.get(key)
        if value is not None and not _integer_field(value, 0, 2**63 - 1):
            raise WireError(f"session field {key} is invalid")
    for key in ("windowCount", "attachedClients"):
        value = session.get(key)
        if value is not None and not _integer_field(value, 0, 2**31 - 1):
            raise WireError(f"session field {key} is invalid")
    if type(session.get("pending")) is not bool:
        raise WireError("session pending field is invalid")
    for key in ("sessionPath", "currentWindow", "currentPath"):
        _string_field(session.get(key), nullable=True, limit=limit)
    panes = session.get("panes")
    if panes is not None:
        if not isinstance(panes, list) or len(panes) > 512:
            raise WireError("session panes are invalid")
        for pane in panes:
            _validate_pane(pane, limit=limit)
    options = session.get("options")
    if options is not None:
        _validate_options(options, limit=limit)


def _validate_pane(value: object, *, limit: int) -> None:
    if not isinstance(value, dict):
        raise WireError("session pane is invalid")
    _require_fields(value, "paneId", "pid", "currentPath", "currentCommand")
    _string_field(value.get("paneId"), nullable=False, limit=4_096)
    if not isinstance(value["paneId"], str) or re.fullmatch(r"%[0-9]+", value["paneId"]) is None:
        raise WireError("session pane ID is invalid")
    pid = value.get("pid")
    if pid is not None and not _integer_field(pid, 0, 2**31 - 1):
        raise WireError("session pane PID is invalid")
    for key in ("currentPath", "currentCommand"):
        _string_field(value.get(key), nullable=True, limit=limit)


def _validate_options(value: object, *, limit: int) -> None:
    if not isinstance(value, dict):
        raise WireError("session options are invalid")
    for name, option_value in value.items():
        if not isinstance(name, str) or re.fullmatch(r"@[A-Za-z0-9_.-]+", name, re.ASCII) is None:
            raise WireError("session option name is invalid")
        _string_field(option_value, nullable=True, limit=limit)


def _generation_field(value: object, *, limit: int) -> None:
    _string_field(value, nullable=False, limit=limit)
    assert isinstance(value, str)
    if any(ord(char) <= 0x1F or 0x7F <= ord(char) <= 0x9F for char in value):
        raise WireError("server generation is invalid")


def _validate_host_row(host: dict[str, object]) -> None:
    _require_fields(
        host,
        "hostId",
        "display",
        "local",
        "status",
        "observedAt",
        "nativeHostname",
        "serverGeneration",
        "route",
        "sessions",
    )
    _string_field(host.get("hostId"), nullable=False, limit=16_384)
    if _IDENTIFIER.fullmatch(host["hostId"]) is None:  # type: ignore[arg-type]
        raise WireError("host ID is invalid")
    _string_field(host.get("display"), nullable=False, limit=16_384)
    if type(host.get("local")) is not bool:
        raise WireError("host local field is invalid")
    status = host.get("status")
    if not isinstance(status, str) or status not in {"ok", "unreachable", "tmux_missing", "error"}:
        raise WireError("host status is invalid")
    if not _integer_field(host.get("observedAt"), 0, 2**63 - 1):
        raise WireError("host observation timestamp is invalid")
    _string_field(host.get("nativeHostname"), nullable=True, limit=16_384)
    generation = host.get("serverGeneration")
    if generation is not None:
        _generation_field(generation, limit=16_384)
    _string_field(host.get("route"), nullable=True, limit=16_384)
    sessions = host.get("sessions")
    if not isinstance(sessions, list) or len(sessions) > 256:
        raise WireError("inventory sessions are invalid")
    error = host.get("error")
    if error is not None:
        if not isinstance(error, dict):
            raise WireError("inventory host error is invalid")
        _require_fields(error, "code", "message")
        code = error.get("code")
        if not isinstance(code, str) or len(code) > 64 or _ERROR_CODE.fullmatch(code) is None:
            raise WireError("inventory host error code is invalid")
        _string_field(error.get("message"), nullable=False, limit=4_096)
        if "hostId" in error:
            host_error_id = error.get("hostId")
            _string_field(host_error_id, nullable=False, limit=4_096)
            if _IDENTIFIER.fullmatch(host_error_id) is None:  # type: ignore[arg-type]
                raise WireError("inventory host error ID is invalid")
    if status != "ok" and error is None:
        raise WireError("failed inventory host is missing its error")
    if status == "ok" and error is not None:
        raise WireError("successful inventory host must not carry an error")
    if status != "ok" and sessions:
        raise WireError("failed inventory host must not carry sessions")


def _emit_error(error: ContractError, limit: int) -> None:
    code = error.code
    if (
        not isinstance(code, str)
        or not code
        or len(code) > 64
        or _ERROR_CODE.fullmatch(code) is None
    ):
        code = "operation_failed"
    message = clean_message(error.message, limit=4_096)
    host_id = (
        error.host_id
        if isinstance(error.host_id, str)
        and len(error.host_id) <= 4_096
        and _IDENTIFIER.fullmatch(error.host_id) is not None
        else None
    )
    envelope: dict[str, object] = {
        "schemaVersion": 1,
        "ok": False,
        "error": {"code": code, "message": message},
    }
    if host_id is not None:
        cast_error = envelope["error"]
        if isinstance(cast_error, dict):
            cast_error["hostId"] = host_id
    try:
        write_document(sys.stdout, envelope, limit=limit)
    except (BrokenPipeError, WireError, TypeError, UnicodeError):
        # A bounded fixed fallback keeps producer errors typed even if an
        # unexpected exception carried an unencodable diagnostic.
        fallback = {
            "schemaVersion": 1,
            "ok": False,
            "error": {"code": "operation_failed", "message": "operation failed"},
        }
        try:
            write_document(sys.stdout, fallback, limit=limit)
        except (BrokenPipeError, WireError, TypeError, UnicodeError):
            return
