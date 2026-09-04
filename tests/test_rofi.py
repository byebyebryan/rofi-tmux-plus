from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from rofi_tmux_plus import cli, rofi
from rofi_tmux_plus.config import Config
from rofi_tmux_plus.errors import ContractError


def session(
    host_id: str,
    session_id: str,
    name: str,
    *,
    activity: int | None = 100,
    attached: int | None = 0,
) -> dict[str, object]:
    return {
        "hostId": host_id,
        "serverGeneration": f"tmux-v1:{host_id}:generation",
        "sessionId": session_id,
        "createdAt": 10,
        "name": name,
        "activityAt": activity,
        "lastAttachedAt": activity,
        "attachedClients": attached,
        "pending": False,
        "windowCount": 2,
        "sessionPath": f"/home/test/code/{host_id}",
        "currentWindow": "shell",
        "currentPath": f"/home/test/code/{host_id}",
    }


def host(
    host_id: str,
    display: str,
    *,
    local: bool,
    sessions: list[dict[str, object]] | None = None,
    status: str = "ok",
    stale: bool = False,
    unavailable: bool = False,
) -> dict[str, object]:
    row: dict[str, object] = {
        "hostId": host_id,
        "display": display,
        "local": local,
        "status": status,
        "observedAt": 100,
        "nativeHostname": f"{host_id}.native",
        "serverGeneration": f"tmux-v1:{host_id}:generation" if status == "ok" else None,
        "route": f"{host_id}.route" if not local else None,
        "sessions": sessions or [],
    }
    if stale:
        row["stale"] = True
    if unavailable:
        row["unavailable"] = True
    if status != "ok":
        row["error"] = {"code": "offline", "message": "route unavailable"}
    return row


def payload(
    *,
    hosts: list[dict[str, object]] | None = None,
    catalog: list[dict[str, object]] | None = None,
    revision: str | None = "sha256:fixture",
    marker: dict[str, object] | None = None,
    needed: bool = False,
    requested: bool = False,
) -> dict[str, object]:
    rows = hosts or [host("alpha", "Alpha", local=True)]
    if catalog is None:
        catalog = [
            {"hostId": str(row["hostId"]), "display": row["display"], "local": row["local"]}
            for row in rows
        ]
    return {
        "schemaVersion": 1,
        "generatedAt": 100,
        "meshRevision": revision,
        "hosts": rows,
        "hostCatalog": catalog,
        "remoteRefreshNeeded": needed,
        "remoteRefreshRequested": requested,
        "remoteRefresh": marker,
    }


class FakeModel:
    def __init__(self, *values: dict[str, object]) -> None:
        self.values = list(values)
        self.calls: list[bool] = []
        self.refresh_calls = 0
        self.host_refreshes: list[tuple[str, str | None]] = []
        self.current_host_refreshes: list[str] = []
        self.host_refresh_error: Exception | None = None

    def load(self, *, start_refresh: bool) -> SimpleNamespace:
        self.calls.append(start_refresh)
        value = self.values[0] if len(self.values) == 1 else self.values.pop(0)
        return SimpleNamespace(payload=value)

    def refresh_now(self) -> SimpleNamespace:
        self.refresh_calls += 1
        value = self.values[0] if len(self.values) == 1 else self.values.pop(0)
        return SimpleNamespace(payload=value)

    def refresh_host(self, host_id: str, revision: str | None) -> SimpleNamespace:
        self.host_refreshes.append((host_id, revision))
        if self.host_refresh_error is not None:
            raise self.host_refresh_error
        value = self.values[0] if len(self.values) == 1 else self.values.pop(0)
        return SimpleNamespace(payload=value)

    def refresh_host_current(self, host_id: str) -> SimpleNamespace:
        self.current_host_refreshes.append(host_id)
        if self.host_refresh_error is not None:
            raise self.host_refresh_error
        value = self.values[0] if len(self.values) == 1 else self.values.pop(0)
        return SimpleNamespace(payload=value)


class FakeLifecycle:
    def __init__(self, error: ContractError | None = None) -> None:
        self.error = error
        self.opens: list[tuple[object, ...]] = []
        self.creates: list[tuple[object, ...]] = []
        self.renames: list[tuple[object, ...]] = []
        self.kills: list[tuple[object, ...]] = []

    def open(self, *args: object) -> dict[str, object]:
        self.opens.append(args)
        if self.error is not None:
            raise self.error
        return {"schemaVersion": 1, "ok": True}

    def create(self, *args: object) -> dict[str, object]:
        self.creates.append(args)
        if self.error is not None:
            raise self.error
        return {"schemaVersion": 1, "ok": True}

    def rename(self, *args: object) -> dict[str, object]:
        self.renames.append(args)
        if self.error is not None:
            raise self.error
        return {"schemaVersion": 1, "ok": True}

    def kill(self, *args: object) -> dict[str, object]:
        self.kills.append(args)
        if self.error is not None:
            raise self.error
        return {"schemaVersion": 1, "ok": True}


def rendered_records(value: str) -> tuple[list[str], list[str]]:
    delimiter = f"\0delim\x1f{rofi.ROFI_DELIMITER_VALUE}\n"
    if delimiter in value:
        header, records = value.split(delimiter, 1)
        headers = [*header.splitlines(), delimiter.rstrip("\n")]
    else:
        records = value
        headers = [
            part for part in records.split(rofi.ROFI_RECORD_SEPARATOR) if part.startswith("\0")
        ]
    rows = [
        row
        for row in records.removesuffix(rofi.ROFI_RECORD_SEPARATOR).split(
            rofi.ROFI_RECORD_SEPARATOR
        )
        if row and not row.startswith("\0")
    ]
    return headers, rows


def row_options(row: str) -> dict[str, str]:
    _, separator, encoded = row.partition("\0")
    if not separator:
        raise AssertionError("missing row options")
    fields = encoded.split("\x1f")
    return dict(zip(fields[::2], fields[1::2], strict=True))


class RofiRenderTests(unittest.TestCase):
    def test_two_physical_pango_lines_escape_text_and_keep_typed_identity(self) -> None:
        item = session("alpha", "$0", "<work>&")
        item["currentWindow"] = "win<one>"
        value = rofi.render_snapshot(
            payload(hosts=[host("alpha", "Alpha", local=True, sessions=[item])]),
            now=200,
            titles=(),
        )
        _, rows = rendered_records(value)
        self.assertEqual(1, len(rows))
        options = row_options(rows[0])
        self.assertEqual(1, options["display"].count("\n"))
        self.assertIn("&lt;work&gt;&amp;", options["display"])
        self.assertIn("win&lt;one&gt;", options["display"])
        identity = json.loads(options["info"])
        self.assertTrue(
            {"hostId", "serverGeneration", "sessionId", "createdAt"} <= set(identity),
        )
        self.assertEqual("$0", identity["sessionId"])
        self.assertIn("/home/test/code/alpha", options["meta"])
        self.assertIn("detached", options["meta"])
        self.assertEqual(rofi.TERMINAL_ICON, options["icon"])

    def test_recent_live_rows_precede_stale_rows_then_activity_and_identity(self) -> None:
        live_old = session("alpha", "$0", "old", activity=10)
        live_new = session("alpha", "$1", "new", activity=20)
        stale_new = session("beta", "$2", "stale", activity=200, attached=None)
        result = rofi.render_snapshot(
            payload(
                hosts=[
                    host("alpha", "Alpha", local=True, sessions=[live_old, live_new]),
                    host(
                        "beta",
                        "Beta",
                        local=False,
                        sessions=[stale_new],
                        status="error",
                        stale=True,
                        unavailable=True,
                    ),
                ]
            ),
            now=300,
            titles=(),
        )
        _, rows = rendered_records(result)
        names = [row.split("\0", 1)[0] for row in rows]
        self.assertEqual(["new", "old", "stale"], names)
        self.assertIn("unavailable", row_options(rows[-1])["display"])

    def test_statuses_are_open_here_attached_detached_and_unavailable(self) -> None:
        local_open = session("alpha", "$0", "open")
        local_attached = session("alpha", "$1", "attached", attached=1)
        remote_detached = session("beta", "$2", "detached", attached=0)
        remote_stale = session("beta", "$3", "stale", attached=None)
        value = payload(
            hosts=[
                host("alpha", "Alpha", local=True, sessions=[local_open, local_attached]),
                host("beta", "Beta", local=False, sessions=[remote_detached, remote_stale]),
            ]
        )
        result = rofi.render_snapshot(
            value,
            now=200,
            titles=("open:0 @ alpha",),
        )
        _, rows = rendered_records(result)
        statuses = {
            json.loads(row_options(row)["info"])["name"]: json.loads(row_options(row)["info"])[
                "status"
            ]
            for row in rows
        }
        self.assertEqual(
            {
                "open": "open here",
                "attached": "attached",
                "detached": "detached",
                "stale": "detached",
            },
            statuses,
        )
        self.assertFalse(
            rofi._is_open_here(
                local_open,
                host("alpha", "Alpha", local=True),
                ("open:0 @ another-host",),
            )
        )

        stale_host = host(
            "beta",
            "Beta",
            local=False,
            sessions=[remote_stale],
            status="error",
            stale=True,
            unavailable=True,
        )
        statuses = {
            json.loads(row_options(row)["info"])["name"]: json.loads(row_options(row)["info"])[
                "status"
            ]
            for row in rendered_records(
                rofi.render_snapshot(
                    payload(
                        hosts=[host("alpha", "Alpha", local=True), stale_host],
                    ),
                    now=200,
                    titles=(),
                )
            )[1]
        }
        self.assertEqual("unavailable", statuses["stale"])

    def test_hosts_root_uses_complete_cold_catalog_in_order_and_host_layer_omits_host(self) -> None:
        alpha = host("alpha", "Alpha", local=True, sessions=[])
        catalog = [
            {"hostId": "alpha", "display": "Alpha", "local": True},
            {"hostId": "beta", "display": "Beta", "local": False},
            {"hostId": "gamma", "display": "Gamma", "local": False},
        ]
        value = payload(hosts=[alpha], catalog=catalog)
        root = rofi.render_snapshot(value, navigation=rofi.NavigationState("hosts"), now=200)
        _, rows = rendered_records(root)
        self.assertEqual([row.split("\0", 1)[0] for row in rows], ["Alpha", "Beta", "Gamma"])
        infos = [json.loads(row_options(row)["info"]) for row in rows]
        self.assertEqual(["alpha", "beta", "gamma"], [item["hostId"] for item in infos])
        nested = rofi.render_snapshot(
            payload(
                hosts=[
                    host("alpha", "Alpha", local=True, sessions=[session("alpha", "$0", "one")])
                ],
                catalog=catalog,
            ),
            navigation=rofi.NavigationState("hosts", "alpha"),
            now=200,
            titles=(),
        )
        _, nested_rows = rendered_records(nested)
        self.assertNotIn("Alpha  ·", row_options(nested_rows[0])["display"])


class RofiProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.value = payload(
            hosts=[
                host("alpha", "Alpha", local=True, sessions=[session("alpha", "$0", "one")]),
                host("beta", "Beta", local=False, sessions=[session("beta", "$1", "two")]),
            ]
        )
        self.model = FakeModel(self.value)
        self.lifecycle = FakeLifecycle()

    def invoke(
        self,
        environ: dict[str, str],
        *,
        model: FakeModel | None = None,
        lifecycle: FakeLifecycle | None = None,
    ) -> str:
        output = io.StringIO()
        with patch("rofi_tmux_plus.rofi._niri_titles", return_value=()), redirect_stdout(output):
            result = rofi.run_rofi(
                environ,
                model_service=model or self.model,
                lifecycle_service=lifecycle or self.lifecycle,
                config=Config(),
            )
        self.assertEqual(0, result)
        return output.getvalue()

    def test_left_right_wrap_roots_and_enter_drills_host(self) -> None:
        right = self.invoke(
            {
                "ROFI_RETV": str(rofi.ROFI_RETV_CUSTOM_2),
                "ROFI_DATA": rofi._navigation_data(rofi.NavigationState()),
            }
        )
        self.assertIn("Tmux › Hosts", right)
        self.assertNotIn("keep-filter", right)
        right_again = self.invoke(
            {
                "ROFI_RETV": str(rofi.ROFI_RETV_CUSTOM_2),
                "ROFI_DATA": rofi._navigation_data(rofi.NavigationState("hosts")),
            }
        )
        self.assertIn("Tmux › Recent", right_again)
        nested_right = self.invoke(
            {
                "ROFI_RETV": str(rofi.ROFI_RETV_CUSTOM_2),
                "ROFI_DATA": rofi._navigation_data(rofi.NavigationState("hosts", "alpha")),
            }
        )
        self.assertIn("Tmux › Recent", nested_right)
        self.assertNotIn("keep-filter", nested_right)
        left = self.invoke(
            {
                "ROFI_RETV": str(rofi.ROFI_RETV_CUSTOM_3),
                "ROFI_DATA": rofi._navigation_data(rofi.NavigationState()),
            }
        )
        self.assertIn("Tmux › Hosts", left)
        root, rows = rendered_records(
            rofi.render_snapshot(self.value, navigation=rofi.NavigationState("hosts"), now=200)
        )
        del root
        host_info = row_options(rows[0])["info"]
        entered = self.invoke(
            {
                "ROFI_RETV": str(rofi.ROFI_RETV_SELECTED),
                "ROFI_DATA": rofi._navigation_data(rofi.NavigationState("hosts")),
                "ROFI_INFO": host_info,
            }
        )
        self.assertIn("Tmux › Hosts › Alpha", entered)
        self.assertNotIn("keep-filter", entered)

    def test_escape_backs_nested_and_exits_at_root(self) -> None:
        backed = self.invoke(
            {
                "ROFI_RETV": str(rofi.ROFI_RETV_CUSTOM_6),
                "ROFI_DATA": rofi._navigation_data(rofi.NavigationState("hosts", "beta")),
            }
        )
        self.assertIn("Tmux › Hosts", backed)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                0,
                rofi.run_rofi(
                    {
                        "ROFI_RETV": str(rofi.ROFI_RETV_CUSTOM_6),
                        "ROFI_DATA": rofi._navigation_data(rofi.NavigationState("hosts")),
                    },
                    model_service=self.model,
                    lifecycle_service=self.lifecycle,
                    config=Config(),
                ),
            )
        self.assertEqual("", output.getvalue())
        self.assertNotIn("element-navigation", backed)
        self.assertNotIn("keep-filter", backed)

    def test_open_uses_typed_full_reference_and_revision(self) -> None:
        rendered = rofi.render_snapshot(self.value, now=200, titles=())
        _, rows = rendered_records(rendered)
        info = row_options(rows[0])["info"]
        self.assertEqual("sha256:fixture", json.loads(info)["meshRevision"])
        self.invoke({"ROFI_RETV": "1", "ROFI_INFO": info}, lifecycle=self.lifecycle)
        self.assertEqual(
            [("alpha", "sha256:fixture", "tmux-v1:alpha:generation", "$0", 10, None)],
            self.lifecycle.opens,
        )

    def test_open_uses_the_revision_in_typed_selection_not_a_new_model_revision(self) -> None:
        _, rows = rendered_records(rofi.render_snapshot(self.value, now=200, titles=()))
        selection = row_options(rows[0])["info"]
        changed = {**self.value, "meshRevision": "sha256:changed"}
        model = FakeModel(changed)
        self.invoke({"ROFI_RETV": "1", "ROFI_INFO": selection}, model=model)
        self.assertEqual("sha256:fixture", self.lifecycle.opens[0][1])

    def test_malformed_selection_without_a_mesh_revision_never_reaches_lifecycle(self) -> None:
        _, rows = rendered_records(rofi.render_snapshot(self.value, now=200, titles=()))
        selection = json.loads(row_options(rows[0])["info"])
        del selection["meshRevision"]
        output = self.invoke({"ROFI_RETV": "1", "ROFI_INFO": json.dumps(selection)})
        self.assertIn("Unable to open session", output)
        self.assertEqual([], self.lifecycle.opens)

    def test_selection_rejects_unicode_format_controls_before_lifecycle(self) -> None:
        _, rows = rendered_records(rofi.render_snapshot(self.value, now=200, titles=()))
        selection = json.loads(row_options(rows[0])["info"])
        selection["serverGeneration"] += "\u2066"
        output = self.invoke({"ROFI_RETV": "1", "ROFI_INFO": json.dumps(selection)})
        self.assertIn("Unable to open session", output)
        self.assertEqual([], self.lifecycle.opens)

    def test_oversized_selection_metadata_never_reaches_json_or_lifecycle(self) -> None:
        output = self.invoke({"ROFI_RETV": "1", "ROFI_INFO": "x" * (rofi.MAX_DATA_LENGTH + 1)})
        self.assertIn("Unable to open session", output)
        self.assertEqual([], self.lifecycle.opens)

    def test_open_failure_keeps_state_and_emits_bounded_notice(self) -> None:
        lifecycle = FakeLifecycle(ContractError("stale_session", "the selected session changed"))
        rendered = rofi.render_snapshot(
            self.value, navigation=rofi.NavigationState("hosts", "alpha"), now=200, titles=()
        )
        _, rows = rendered_records(rendered)
        output = self.invoke(
            {
                "ROFI_RETV": "1",
                "ROFI_INFO": row_options(rows[0])["info"],
                "ROFI_DATA": rofi._navigation_data(rofi.NavigationState("hosts", "alpha")),
            },
            lifecycle=lifecycle,
        )
        self.assertIn("Unable to open session", output)
        self.assertIn("keep-selection", output)
        self.assertIn("keep-filter", output)
        self.assertIn("errorDeadline", output)
        self.assertIn('"hostId":"alpha"', output)
        self.assertTrue(self.model.calls[-1])

    def test_open_failure_notice_is_bounded_before_it_reenters_rofi_data(self) -> None:
        lifecycle = FakeLifecycle(ContractError("operation_failed", "x" * 10_000))
        _, rows = rendered_records(rofi.render_snapshot(self.value, now=200, titles=()))
        output = self.invoke(
            {"ROFI_RETV": "1", "ROFI_INFO": row_options(rows[0])["info"]}, lifecycle=lifecycle
        )
        message = output.split("\0message\x1f", 1)[1].split(rofi.ROFI_RECORD_SEPARATOR, 1)[0]
        self.assertLessEqual(len(message), rofi.MAX_MESSAGE_LENGTH)
        self.assertTrue(message.endswith("…"))

    def test_tab_is_not_a_view_callback_and_recent_custom_input_enters_host_chooser(self) -> None:
        self.assertNotIn("Tab", rofi.render_snapshot(self.value))
        output = self.invoke({"ROFI_RETV": "2", "ROFI_INPUT": "new-name"})
        self.assertIn("Tmux › Choose host", output)
        self.assertIn('"name":"new-name"', output)
        self.assertEqual([], self.lifecycle.opens)
        self.assertEqual([], self.lifecycle.creates)


class RofiMutationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.value = payload(
            hosts=[
                host("alpha", "Alpha", local=True, sessions=[session("alpha", "$0", "one")]),
                host("beta", "Beta", local=False, sessions=[session("beta", "$1", "two")]),
            ]
        )
        self.model = FakeModel(self.value)
        self.lifecycle = FakeLifecycle()

    def invoke(self, environ: dict[str, str], *, lifecycle: FakeLifecycle | None = None) -> str:
        output = io.StringIO()
        with patch("rofi_tmux_plus.rofi._niri_titles", return_value=()), redirect_stdout(output):
            self.assertEqual(
                0,
                rofi.run_rofi(
                    environ,
                    model_service=self.model,
                    lifecycle_service=lifecycle or self.lifecycle,
                    config=Config(),
                ),
            )
        return output.getvalue()

    @staticmethod
    def data(rendered: str) -> str:
        return rendered.split("\0data\x1f", 1)[1].split(rofi.ROFI_RECORD_SEPARATOR, 1)[0]

    @staticmethod
    def rows(rendered: str) -> list[str]:
        return rendered_records(rendered)[1]

    def session_info(self, *, host_id: str = "alpha") -> str:
        rendered = rofi.render_snapshot(self.value, now=200, titles=())
        for row in self.rows(rendered):
            if json.loads(row_options(row)["info"])["hostId"] == host_id:
                return row_options(row)["info"]
        raise AssertionError("session row missing")

    def test_recent_create_chooser_uses_typed_host_and_exact_create_arguments(self) -> None:
        chooser = self.invoke({"ROFI_RETV": "2", "ROFI_INPUT": "fresh"})
        self.assertIn("Tmux › Choose host", chooser)
        beta = next(
            row_options(row)["info"]
            for row in self.rows(chooser)
            if json.loads(row_options(row)["info"])["hostId"] == "beta"
        )
        self.assertEqual("host", json.loads(beta)["type"])
        result = self.invoke({"ROFI_RETV": "1", "ROFI_DATA": self.data(chooser), "ROFI_INFO": beta})
        self.assertEqual("", result)
        self.assertEqual(
            [("beta", "sha256:fixture", "fresh", None, (), (), False, None, True)],
            self.lifecycle.creates,
        )

    def test_ensure_opens_only_a_current_exact_name_and_host_root_cannot_create(self) -> None:
        chooser = self.invoke({"ROFI_RETV": "2", "ROFI_INPUT": "one"})
        alpha = row_options(self.rows(chooser)[0])["info"]
        self.invoke({"ROFI_RETV": "1", "ROFI_DATA": self.data(chooser), "ROFI_INFO": alpha})
        self.assertEqual(
            [("alpha", "sha256:fixture", "tmux-v1:alpha:generation", "$0", 10, None)],
            self.lifecycle.opens,
        )
        root = self.invoke(
            {
                "ROFI_RETV": "2",
                "ROFI_INPUT": "blocked",
                "ROFI_DATA": rofi._navigation_data(rofi.NavigationState("hosts")),
            }
        )
        self.assertIn("enter a host", root)
        self.assertEqual([], self.lifecycle.creates)

    def test_host_layer_custom_create_and_hostile_input_never_mutates(self) -> None:
        nested = rofi._navigation_data(rofi.NavigationState("hosts", "beta"))
        self.assertEqual(
            "", self.invoke({"ROFI_RETV": "2", "ROFI_INPUT": "nested", "ROFI_DATA": nested})
        )
        self.assertEqual(
            [("beta", "sha256:fixture", "nested", None, (), (), False, None, True)],
            self.lifecycle.creates,
        )
        failed = self.invoke({"ROFI_RETV": "2", "ROFI_INPUT": "\u2066", "ROFI_DATA": nested})
        self.assertIn("Unable to create or open", failed)
        self.assertEqual(1, len(self.lifecycle.creates))
        empty = self.invoke({"ROFI_RETV": "2", "ROFI_INPUT": "", "ROFI_DATA": nested})
        self.assertIn("session name is empty", empty)
        self.assertEqual(1, len(self.lifecycle.creates))
        oversized = self.invoke(
            {
                "ROFI_RETV": "2",
                "ROFI_INPUT": "x" * (rofi.MAX_TYPED_NAME_LENGTH + 1),
                "ROFI_DATA": nested,
            }
        )
        self.assertIn("session name is too large", oversized)
        self.assertEqual(1, len(self.lifecycle.creates))

    def test_rename_is_explicit_and_reconciles_only_affected_host(self) -> None:
        editing = self.invoke({"ROFI_RETV": "13", "ROFI_INFO": self.session_info()})
        self.assertIn("Tmux › Rename session", editing)
        self.assertIn("Enter a new name", editing)
        # Enter is deliberately inert in rename mode.
        self.invoke(
            {"ROFI_RETV": "1", "ROFI_DATA": self.data(editing), "ROFI_INFO": self.session_info()}
        )
        self.assertEqual([], self.lifecycle.renames)
        result = self.invoke(
            {"ROFI_RETV": "2", "ROFI_DATA": self.data(editing), "ROFI_INPUT": "renamed"}
        )
        self.assertIn("Session renamed.", result)
        self.assertEqual(
            [
                (
                    "alpha",
                    "sha256:fixture",
                    "tmux-v1:alpha:generation",
                    "$0",
                    10,
                    "one",
                    "renamed",
                )
            ],
            self.lifecycle.renames,
        )
        self.assertEqual([("alpha", "sha256:fixture")], self.model.host_refreshes)
        self.assertNotIn('"action"', self.data(result))

    def test_rename_oversized_input_never_reaches_lifecycle(self) -> None:
        editing = self.invoke({"ROFI_RETV": "13", "ROFI_INFO": self.session_info()})
        output = self.invoke(
            {
                "ROFI_RETV": "2",
                "ROFI_DATA": self.data(editing),
                "ROFI_INPUT": "x" * (rofi.MAX_TYPED_NAME_LENGTH + 1),
            }
        )
        self.assertIn("session name is too large", output)
        self.assertEqual([], self.lifecycle.renames)

    def test_confirmation_defaults_cancel_and_kill_uses_exact_reference(self) -> None:
        confirmation = self.invoke(
            {"ROFI_RETV": "3", "ROFI_INFO": self.session_info(host_id="beta")}
        )
        rows = self.rows(confirmation)
        self.assertEqual("Cancel", rows[0].split("\0", 1)[0])
        self.assertIn("disconnects 0 live clients", row_options(rows[1])["display"])
        canceled = self.invoke(
            {
                "ROFI_RETV": "1",
                "ROFI_DATA": self.data(confirmation),
                "ROFI_INFO": row_options(rows[0])["info"],
            }
        )
        self.assertIn("Tmux › Recent", canceled)
        self.assertEqual([], self.lifecycle.kills)
        confirmation = self.invoke(
            {"ROFI_RETV": "3", "ROFI_INFO": self.session_info(host_id="beta")}
        )
        result = self.invoke(
            {
                "ROFI_RETV": "1",
                "ROFI_DATA": self.data(confirmation),
                "ROFI_INFO": row_options(self.rows(confirmation)[1])["info"],
            }
        )
        self.assertIn("Session killed.", result)
        self.assertEqual(
            [("beta", "sha256:fixture", "tmux-v1:beta:generation", "$1", 10, "two")],
            self.lifecycle.kills,
        )
        self.assertEqual([("beta", "sha256:fixture")], self.model.host_refreshes)

    def test_action_navigation_is_inert_escape_restores_origin_and_stale_does_not_retry(
        self,
    ) -> None:
        editing = self.invoke({"ROFI_RETV": "13", "ROFI_INFO": self.session_info()})
        inert = self.invoke({"ROFI_RETV": "11", "ROFI_DATA": self.data(editing)})
        self.assertIn("Tmux › Rename session", inert)
        backed = self.invoke({"ROFI_RETV": "15", "ROFI_DATA": self.data(editing)})
        self.assertIn("Tmux › Recent", backed)
        self.assertNotIn('"action"', self.data(backed))
        stale_lifecycle = FakeLifecycle(ContractError("stale_session", "session changed"))
        failed = self.invoke(
            {"ROFI_RETV": "2", "ROFI_DATA": self.data(editing), "ROFI_INPUT": "again"},
            lifecycle=stale_lifecycle,
        )
        self.assertIn("Unable to rename", failed)
        self.assertEqual(1, len(stale_lifecycle.renames))
        self.assertEqual(["alpha"], self.model.current_host_refreshes)

    def test_confirmation_refuses_changed_typed_selection(self) -> None:
        confirmation = self.invoke({"ROFI_RETV": "3", "ROFI_INFO": self.session_info()})
        changed = json.loads(row_options(self.rows(confirmation)[1])["info"])
        changed["selection"]["sessionId"] = "$999"
        output = self.invoke(
            {
                "ROFI_RETV": "1",
                "ROFI_DATA": self.data(confirmation),
                "ROFI_INFO": json.dumps(changed),
            }
        )
        self.assertIn("Unable to kill", output)
        self.assertEqual([], self.lifecycle.kills)

    def test_destructive_action_requires_current_rendered_mesh_revision(self) -> None:
        changed = {**self.value, "meshRevision": "sha256:changed"}
        self.model.values = [changed]
        editing = self.invoke({"ROFI_RETV": "13", "ROFI_INFO": self.session_info()})
        self.assertIn("selected host mesh changed", editing)
        self.assertNotIn('"action"', editing)
        self.assertEqual([], self.lifecycle.renames)
        self.assertEqual([], self.lifecycle.kills)

    def test_success_is_not_reclassified_when_affected_refresh_fails(self) -> None:
        editing = self.invoke({"ROFI_RETV": "13", "ROFI_INFO": self.session_info()})
        self.model.host_refresh_error = ContractError(
            "operation_failed", "remote inventory unavailable"
        )
        output = self.invoke(
            {"ROFI_RETV": "2", "ROFI_DATA": self.data(editing), "ROFI_INPUT": "renamed"}
        )
        self.assertIn("Session renamed.", output)
        self.assertIn("Refresh warning", output)
        self.assertEqual(1, len(self.lifecycle.renames))
        self.assertIn("Tmux › Recent", output)
        self.assertIn("one", output)
        data = self.data(output)
        self.assertNotIn('"action"', data)
        self.assertEqual("recent", json.loads(data)["navigation"]["view"])

    def test_invalid_pending_action_blocks_all_mutation_callbacks_and_escape_cancels(self) -> None:
        invalid = json.dumps(
            {
                "version": 1,
                "navigation": {"view": "recent"},
                "action": {"kind": "rename"},
            }
        )
        for retv in ("1", "2", "13", "3"):
            with self.subTest(retv=retv):
                output = self.invoke(
                    {
                        "ROFI_RETV": retv,
                        "ROFI_DATA": invalid,
                        "ROFI_INFO": self.session_info(),
                        "ROFI_INPUT": "new-name",
                    }
                )
                self.assertIn("Pending action state is invalid", output)
                self.assertIn('"blockedAction":true', self.data(output))
        self.assertEqual([], self.lifecycle.opens)
        self.assertEqual([], self.lifecycle.creates)
        self.assertEqual([], self.lifecycle.renames)
        self.assertEqual([], self.lifecycle.kills)
        self.assertEqual("", self.invoke({"ROFI_RETV": "15", "ROFI_DATA": invalid}))

    def test_pending_action_state_budget_handles_unicode_and_selection_boundary(self) -> None:
        safe = rofi._new_action("choose-host", rofi.NavigationState(), name="é" * 1800)
        state = rofi._error_state(
            rofi.ContinuationState(action=safe), "é" * rofi.MAX_MESSAGE_LENGTH, now=100, key="test"
        )
        encoded = rofi._state_data(state)
        self.assertLessEqual(len(encoded), rofi.MAX_DATA_LENGTH)
        self.assertFalse(rofi.parse_continuation_state(encoded).blocked_action)
        with self.assertRaises(ContractError):
            rofi._new_action("choose-host", rofi.NavigationState(), name="é" * 2048)

        selection = json.loads(self.session_info())
        selection["serverGeneration"] = "é" * 2048
        oversized_action = json.dumps(
            {
                "version": 1,
                "navigation": {"view": "recent"},
                "action": {
                    "kind": "rename",
                    "origin": {"view": "recent"},
                    "selection": selection,
                },
            },
            ensure_ascii=True,
        )
        self.assertLessEqual(len(oversized_action), rofi.MAX_DATA_LENGTH)
        self.assertTrue(rofi.parse_continuation_state(oversized_action).blocked_action)
        output = self.invoke(
            {"ROFI_RETV": "2", "ROFI_DATA": oversized_action, "ROFI_INPUT": "new-name"}
        )
        self.assertIn("Pending action state is invalid", output)
        self.assertEqual([], self.lifecycle.renames)


class RofiRefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old = payload(
            hosts=[host("alpha", "Alpha", local=True, sessions=[session("alpha", "$0", "old")])],
            marker={
                "schemaVersion": 1,
                "state": "running",
                "meshRevision": "sha256:fixture",
                "updatedAt": 100,
            },
            needed=True,
        )
        self.fresh = payload(
            hosts=[host("alpha", "Alpha", local=True, sessions=[session("alpha", "$0", "new")])],
            marker={
                "schemaVersion": 1,
                "state": "complete",
                "meshRevision": "sha256:fixture",
                "updatedAt": 200,
            },
        )

    def test_initial_stale_model_polls_and_completion_preserves_selection_filter_then_clears(
        self,
    ) -> None:
        model = FakeModel(self.old)
        output = io.StringIO()
        with (
            patch("rofi_tmux_plus.rofi._niri_titles", return_value=()),
            patch("rofi_tmux_plus.rofi.time.time", return_value=100),
            redirect_stdout(output),
        ):
            rofi.run_rofi(
                {"ROFI_RETV": "0"},
                model_service=model,
                lifecycle_service=FakeLifecycle(),
                config=Config(),
            )
        initial = output.getvalue()
        self.assertIn("Refreshing in background", initial)
        self.assertIn('"refreshDeadline":', initial)
        data = initial.split("\0data\x1f", 1)[1].split("\n", 1)[0]
        model.values = [self.fresh]
        output = io.StringIO()
        with (
            patch("rofi_tmux_plus.rofi._niri_titles", return_value=()),
            patch("rofi_tmux_plus.rofi.time.time", return_value=101),
            redirect_stdout(output),
        ):
            rofi.run_rofi(
                {"ROFI_RETV": str(rofi.ROFI_RETV_CUSTOM_19), "ROFI_DATA": data},
                model_service=model,
                lifecycle_service=FakeLifecycle(),
                config=Config(),
            )
        completed = output.getvalue()
        self.assertIn("keep-selection", completed)
        self.assertIn("keep-filter", completed)
        self.assertNotIn("Refreshing in background", completed)
        self.assertIn("delay: 0", completed)
        self.assertEqual([True, False], model.calls)

    def test_failure_stall_and_stale_stop_polling_and_show_self_clearing_notice_without_retry(
        self,
    ) -> None:
        for marker_state in ("failed", "stalled", "stale"):
            with self.subTest(marker_state=marker_state):
                marker = {
                    "schemaVersion": 1,
                    "state": marker_state,
                    "meshRevision": "sha256:fixture",
                    "updatedAt": 200,
                    "message": "worker stopped",
                }
                failed = payload(
                    hosts=[
                        host("alpha", "Alpha", local=True, sessions=[session("alpha", "$0", "old")])
                    ],
                    marker=marker,
                    needed=True,
                )
                model = FakeModel(failed)
                output = io.StringIO()
                with (
                    patch("rofi_tmux_plus.rofi._niri_titles", return_value=()),
                    patch("rofi_tmux_plus.rofi.time.time", return_value=100),
                    redirect_stdout(output),
                ):
                    rofi.run_rofi(
                        {
                            "ROFI_RETV": str(rofi.ROFI_RETV_CUSTOM_19),
                            "ROFI_DATA": rofi._refresh_data(150),
                        },
                        model_service=model,
                        lifecycle_service=FakeLifecycle(),
                        config=Config(),
                    )
                rendered = output.getvalue()
                self.assertIn("worker stopped", rendered)
                self.assertNotIn('"refreshDeadline":', rendered)
                self.assertIn('"errorDeadline":', rendered)
                self.assertEqual([False], model.calls)

    def test_alt_r_is_bounded_foreground_refresh_without_background_restart(self) -> None:
        model = FakeModel(self.fresh)
        output = io.StringIO()
        with patch("rofi_tmux_plus.rofi._niri_titles", return_value=()), redirect_stdout(output):
            rofi.run_rofi(
                {"ROFI_RETV": str(rofi.ROFI_RETV_CUSTOM_1)},
                model_service=model,
                lifecycle_service=FakeLifecycle(),
                config=Config(),
            )
        self.assertEqual(1, model.refresh_calls)
        self.assertNotIn("Refreshing in background", output.getvalue())


class EntryPointTests(unittest.TestCase):
    def test_cli_auto_detects_rofi_without_changing_json_dispatch(self) -> None:
        with (
            patch.dict(os.environ, {"ROFI_RETV": "0"}),
            patch("rofi_tmux_plus.rofi.run_rofi", return_value=7) as run,
        ):
            self.assertEqual(7, cli.main([]))
        run.assert_called_once()

    def test_cli_arguments_remain_json_commands_when_rofi_environment_is_inherited(self) -> None:
        inventory = type(
            "Inventory",
            (),
            {
                "inventory": lambda self, **_kwargs: {
                    "schemaVersion": 1,
                    "generatedAt": 1,
                    "meshRevision": None,
                    "hosts": [],
                }
            },
        )()
        output = io.StringIO()
        with (
            patch.dict(os.environ, {"ROFI_RETV": "0"}),
            patch("rofi_tmux_plus.cli._inventory_service", return_value=inventory),
            patch("rofi_tmux_plus.rofi.run_rofi") as run,
            redirect_stdout(output),
        ):
            self.assertEqual(0, cli.main(["inventory", "--json"]))
        run.assert_not_called()
        self.assertEqual([], json.loads(output.getvalue())["hosts"])
