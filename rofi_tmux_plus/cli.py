"""Versioned JSON command-line boundary for local Tmux Session v1."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence

from .config import load_config
from .errors import ContractError, clean_message
from .inventory_service import InventoryService
from .lifecycle_service import LifecycleService
from .picker_model import PickerModelService, RemoteRefresh
from .remote_cache import RemoteCache
from .tmux import validate_user_option

_JSON_COMMANDS = {
    "inventory",
    "open",
    "create",
    "rename",
    "kill",
    "_picker-model",
    "_refresh",
    "_refresh-status",
}
_ROFI_CALLBACK_ENV = {"ROFI_DATA", "ROFI_INFO", "ROFI_INPUT"}


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
    def error(self, message: str) -> None:
        raise ContractError("invalid_input", message)


def _common(parser: argparse.ArgumentParser, *, reference: bool = False) -> None:
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--host")
    parser.add_argument("--mesh-revision")
    if reference:
        parser.add_argument("--server-generation", required=True)
        parser.add_argument("--session-id", required=True)
        parser.add_argument("--created-at", required=True, type=int)


def build_parser() -> JsonArgumentParser:
    parser = JsonArgumentParser(prog="rofi-tmux-plus", add_help=True)
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
    parser = build_parser()
    try:
        result = dispatch(parser.parse_args(argv))
    except ContractError as error:
        print(json.dumps(error.envelope(), separators=(",", ":"), ensure_ascii=False))
        return 2
    except BrokenPipeError:
        return 1
    except Exception as error:  # noqa: BLE001 - defensive JSON process boundary
        failure = ContractError("operation_failed", clean_message(error))
        print(json.dumps(failure.envelope(), separators=(",", ":"), ensure_ascii=False))
        return 1
    if result is not None:
        print(json.dumps(result, separators=(",", ":"), ensure_ascii=False))
    return 0
