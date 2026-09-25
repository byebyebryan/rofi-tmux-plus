from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from rofi_tmux_plus import cli, rofi
from rofi_tmux_plus.config import Config
from rofi_tmux_plus.errors import ContractError
from rofi_tmux_plus.presentation_cache import SNAPSHOT_KEY_LENGTH, PresentationSnapshotCache


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
        options_by_name = {
            json.loads(row_options(row)["info"])["name"]: row_options(row) for row in rows
        }
        statuses = {
            name: json.loads(options["info"])["status"] for name, options in options_by_name.items()
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
        self.assertEqual("true", options_by_name["open"]["active"])
        self.assertNotIn("urgent", options_by_name["open"])
        for name in ("attached", "detached", "stale"):
            with self.subTest(name=name):
                self.assertNotIn("active", options_by_name[name])
                self.assertNotIn("urgent", options_by_name[name])
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
        _, stale_rows = rendered_records(
            rofi.render_snapshot(
                payload(
                    hosts=[host("alpha", "Alpha", local=True), stale_host],
                ),
                now=200,
                titles=(),
            )
        )
        stale_options = row_options(stale_rows[0])
        self.assertEqual("unavailable", json.loads(stale_options["info"])["status"])
        self.assertEqual("true", stale_options["urgent"])
        self.assertNotIn("active", stale_options)
        self.assertNotIn("nonselectable", stale_options)
        _, recovered_rows = rendered_records(
            rofi.render_snapshot(
                payload(
                    hosts=[
                        host("alpha", "Alpha", local=True),
                        host("beta", "Beta", local=False, sessions=[remote_stale]),
                    ]
                ),
                now=200,
                titles=(),
            )
        )
        recovered_options = row_options(recovered_rows[0])
        self.assertEqual("detached", json.loads(recovered_options["info"])["status"])
        self.assertNotIn("active", recovered_options)
        self.assertNotIn("urgent", recovered_options)

    def test_flat_scopes_use_complete_catalog_order_and_leaf_rows(self) -> None:
        alpha = host("alpha", "Alpha", local=True, sessions=[session("alpha", "$0", "one")])
        beta = host("beta", "Beta", local=False, sessions=[session("beta", "$1", "two")])
        catalog = [
            {"hostId": "alpha", "display": "Alpha", "local": True},
            {"hostId": "beta", "display": "Beta", "local": False},
            {"hostId": "gamma", "display": "Gamma", "local": False},
        ]
        value = payload(hosts=[alpha, beta], catalog=catalog)
        self.assertEqual(
            [
                (rofi.VIEW_ALL, None),
                (rofi.VIEW_LOCAL, "alpha"),
                (rofi.VIEW_HOST, "beta"),
                (rofi.VIEW_HOST, "gamma"),
            ],
            [(item.view, item.host_id) for item in rofi._scope_ring(value)],
        )
        all_rows = rendered_records(
            rofi.render_snapshot(value, navigation=rofi.NavigationState(), now=200, titles=())
        )[1]
        self.assertEqual([row.split("\0", 1)[0] for row in all_rows], ["one", "two"])
        self.assertIn("Alpha", row_options(all_rows[0])["display"])
        local_rows = rendered_records(
            rofi.render_snapshot(
                value,
                navigation=rofi.NavigationState(rofi.VIEW_LOCAL, "alpha"),
                now=200,
                titles=(),
            )
        )[1]
        self.assertEqual([row.split("\0", 1)[0] for row in local_rows], ["one"])
        self.assertNotIn("Alpha  ·", row_options(local_rows[0])["display"])
        cold_rows = rendered_records(
            rofi.render_snapshot(
                value,
                navigation=rofi.NavigationState(rofi.VIEW_HOST, "gamma"),
                now=200,
                titles=(),
            )
        )[1]
        self.assertEqual(
            [row.split("\0", 1)[0] for row in cold_rows], ["No tmux sessions available on Gamma"]
        )
        cold_options = row_options(cold_rows[0])
        self.assertEqual("true", cold_options["nonselectable"])
        self.assertNotIn("urgent", cold_options)
        self.assertNotIn("active", cold_options)
        empty_local = payload(
            hosts=[host("alpha", "Alpha", local=True)],
            catalog=[{"hostId": "alpha", "display": "Alpha", "local": True}],
            revision=None,
        )
        _, empty_local_rows = rendered_records(
            rofi.render_snapshot(empty_local, now=200, titles=())
        )
        empty_local_options = row_options(empty_local_rows[0])
        self.assertEqual("true", empty_local_options["nonselectable"])
        self.assertNotIn("urgent", empty_local_options)
        self.assertNotIn("active", empty_local_options)
        unavailable = host(
            "gamma",
            "Gamma",
            local=False,
            status="error",
            stale=True,
            unavailable=True,
        )
        unavailable_value = payload(
            hosts=[host("alpha", "Alpha", local=True), unavailable],
            catalog=[
                {"hostId": "alpha", "display": "Alpha", "local": True},
                {"hostId": "gamma", "display": "Gamma", "local": False},
            ],
        )
        _, unavailable_rows = rendered_records(
            rofi.render_snapshot(
                unavailable_value,
                navigation=rofi.NavigationState(rofi.VIEW_HOST, "gamma"),
                now=200,
                titles=(),
            )
        )
        unavailable_options = row_options(unavailable_rows[0])
        self.assertEqual("true", unavailable_options["nonselectable"])
        self.assertEqual("true", unavailable_options["urgent"])
        self.assertNotIn("active", unavailable_options)
        local_only = payload(
            hosts=[alpha],
            catalog=[{"hostId": "alpha", "display": "Alpha", "local": True}],
            revision=None,
        )
        self.assertEqual(
            [(rofi.VIEW_LOCAL, "alpha")],
            [(item.view, item.host_id) for item in rofi._scope_ring(local_only)],
        )
        local_only_rows = rendered_records(rofi.render_snapshot(local_only, now=200, titles=()))[1]
        self.assertIn("Tmux › Local", rofi.render_snapshot(local_only, now=200, titles=()))
        self.assertEqual([row.split("\0", 1)[0] for row in local_only_rows], ["one"])


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
        self.cache_directory = TemporaryDirectory()
        self.addCleanup(self.cache_directory.cleanup)
        self.presentation_cache = PresentationSnapshotCache(Path(self.cache_directory.name))
        self.snapshot_key = self.presentation_cache.store(self.value)

    def navigation_data(self, navigation: rofi.NavigationState) -> str:
        return rofi._state_data(
            rofi.ContinuationState(navigation=navigation, snapshot_key=self.snapshot_key)
        )

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
                presentation_cache=self.presentation_cache,
            )
        self.assertEqual(0, result)
        return output.getvalue()

    def test_left_right_wrap_flat_scopes_preserves_filter_and_resets_selection(self) -> None:
        right = self.invoke(
            {
                "ROFI_RETV": str(rofi.ROFI_RETV_CUSTOM_2),
                "ROFI_DATA": self.navigation_data(rofi.NavigationState()),
            }
        )
        self.assertIn("Tmux › Local", right)
        self.assertIn("keep-filter", right)
        self.assertNotIn("keep-selection", right)
        self.assertEqual(
            {"view": rofi.VIEW_LOCAL, "hostId": "alpha"},
            json.loads(right.split("\0data\x1f", 1)[1].split(rofi.ROFI_RECORD_SEPARATOR, 1)[0])[
                "navigation"
            ],
        )
        right_again = self.invoke(
            {
                "ROFI_RETV": str(rofi.ROFI_RETV_CUSTOM_2),
                "ROFI_DATA": self.navigation_data(rofi.NavigationState(rofi.VIEW_LOCAL, "alpha")),
            }
        )
        self.assertIn("Tmux › Beta", right_again)
        left = self.invoke(
            {
                "ROFI_RETV": str(rofi.ROFI_RETV_CUSTOM_3),
                "ROFI_DATA": self.navigation_data(rofi.NavigationState(rofi.VIEW_HOST, "beta")),
            }
        )
        self.assertIn("Tmux › Local", left)
        wrapped = self.invoke(
            {
                "ROFI_RETV": str(rofi.ROFI_RETV_CUSTOM_3),
                "ROFI_DATA": self.navigation_data(rofi.NavigationState(rofi.VIEW_LOCAL, "alpha")),
            }
        )
        self.assertIn("Tmux › All", wrapped)

    def test_legacy_escape_callback_is_an_immediate_noop(self) -> None:
        output = io.StringIO()
        with (
            patch("rofi_tmux_plus.rofi.load_config", side_effect=AssertionError("must not load")),
            redirect_stdout(output),
        ):
            self.assertEqual(
                0,
                rofi.run_rofi(
                    {
                        "ROFI_RETV": str(rofi.ROFI_RETV_CUSTOM_6),
                        "ROFI_DATA": "malformed and oversized state" * 1000,
                    },
                ),
            )
        self.assertEqual("", output.getvalue())
        self.assertEqual([], self.model.calls)

    def test_arrow_callback_is_cache_only_and_uses_exact_snapshot(self) -> None:
        broken = FakeModel(payload(hosts=[host("alpha", "Wrong", local=True)]))

        def broken_load(*, start_refresh: bool) -> SimpleNamespace:
            del start_refresh
            raise RuntimeError("model unavailable")

        broken.load = broken_load
        output = self.invoke(
            {
                "ROFI_RETV": str(rofi.ROFI_RETV_CUSTOM_2),
                "ROFI_DATA": self.navigation_data(rofi.NavigationState()),
            },
            model=broken,
        )
        self.assertIn("Tmux › Local", output)
        self.assertIn("one", output)
        self.assertNotIn("model unavailable", output)
        self.assertIn("keep-filter", output)
        self.assertNotIn("keep-selection", output)
        self.assertEqual([], broken.calls)
        self.assertEqual([], self.lifecycle.opens)
        self.assertEqual([], self.lifecycle.creates)

    def test_arrow_cache_miss_is_bounded_before_any_setup_or_model_read(self) -> None:
        missing = rofi._state_data(rofi.ContinuationState(snapshot_key="f" * SNAPSHOT_KEY_LENGTH))
        output = io.StringIO()
        with (
            patch(
                "rofi_tmux_plus.rofi.load_config",
                side_effect=AssertionError("arrow callback must not load config"),
            ),
            redirect_stdout(output),
        ):
            result = rofi.run_rofi(
                {
                    "ROFI_RETV": str(rofi.ROFI_RETV_CUSTOM_2),
                    "ROFI_DATA": missing,
                },
                presentation_cache=self.presentation_cache,
            )
        self.assertEqual(0, result)
        self.assertIn(rofi.CACHED_SNAPSHOT_UNAVAILABLE_MESSAGE, output.getvalue())
        self.assertIn("keep-filter", output.getvalue())
        self.assertNotIn("arrow callback must not load config", output.getvalue())

    def test_browsing_rejects_forged_host_row_as_non_leaf(self) -> None:
        forged = json.dumps({"type": "host", "hostId": "beta", "display": "Beta"})
        output = self.invoke({"ROFI_RETV": "1", "ROFI_INFO": forged})
        self.assertIn("selected row is not a session", output)
        self.assertEqual([], self.lifecycle.opens)

    def test_open_uses_typed_full_reference_and_revision(self) -> None:
        rendered = rofi.render_snapshot(self.value, now=200, titles=())
        _, rows = rendered_records(rendered)
        info = row_options(rows[0])["info"]
        self.assertEqual("sha256:fixture", json.loads(info)["meshRevision"])
        self.invoke({"ROFI_RETV": "1", "ROFI_INFO": info}, lifecycle=self.lifecycle)
        self.assertEqual([], self.model.calls)
        self.assertEqual(
            [("alpha", "sha256:fixture", "tmux-v1:alpha:generation", "$0", 10, None)],
            self.lifecycle.opens,
        )

    def test_local_only_open_uses_typed_reference_without_model_load(self) -> None:
        value = payload(
            hosts=[host("alpha", "Alpha", local=True, sessions=[session("alpha", "$0", "one")])],
            revision=None,
        )
        model = FakeModel(value)
        _, rows = rendered_records(rofi.render_snapshot(value, now=200, titles=()))
        info = row_options(rows[0])["info"]
        self.invoke(
            {"ROFI_RETV": "1", "ROFI_INFO": info},
            model=model,
            lifecycle=self.lifecycle,
        )
        self.assertEqual([], model.calls)
        self.assertEqual(
            [("alpha", None, "tmux-v1:alpha:generation", "$0", 10, None)],
            self.lifecycle.opens,
        )

    def test_remote_open_uses_typed_reference_without_model_load(self) -> None:
        value = payload(
            hosts=[host("beta", "Beta", local=False, sessions=[session("beta", "$1", "two")])],
            revision="sha256:remote",
        )
        model = FakeModel(value)
        _, rows = rendered_records(rofi.render_snapshot(value, now=200, titles=()))
        info = row_options(rows[0])["info"]
        self.invoke(
            {"ROFI_RETV": "1", "ROFI_INFO": info},
            model=model,
            lifecycle=self.lifecycle,
        )
        self.assertEqual([], model.calls)
        self.assertEqual(
            [("beta", "sha256:remote", "tmux-v1:beta:generation", "$1", 10, None)],
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
        self.assertIn(
            "[Open]</span> · Kill  │  Tab: Cycle actions\u2028\u2028Unable to open session", output
        )
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
            self.value,
            navigation=rofi.NavigationState(rofi.VIEW_LOCAL, "alpha"),
            now=200,
            titles=(),
        )
        _, rows = rendered_records(rendered)
        output = self.invoke(
            {
                "ROFI_RETV": "1",
                "ROFI_INFO": row_options(rows[0])["info"],
                "ROFI_DATA": rofi._navigation_data(rofi.NavigationState(rofi.VIEW_LOCAL, "alpha")),
            },
            lifecycle=lifecycle,
        )
        self.assertIn(
            "[Open]</span> · Kill  │  Tab: Cycle actions\u2028\u2028Unable to open session", output
        )
        self.assertIn("keep-selection", output)
        self.assertIn("keep-filter", output)
        self.assertIn("errorDeadline", output)
        self.assertIn('"hostId":"alpha"', output)
        self.assertEqual([True], self.model.calls)

    def test_open_failure_notice_is_bounded_before_it_reenters_rofi_data(self) -> None:
        lifecycle = FakeLifecycle(ContractError("operation_failed", "x" * 10_000))
        _, rows = rendered_records(rofi.render_snapshot(self.value, now=200, titles=()))
        output = self.invoke(
            {"ROFI_RETV": "1", "ROFI_INFO": row_options(rows[0])["info"]}, lifecycle=lifecycle
        )
        message = output.split("\0message\x1f", 1)[1].split(rofi.ROFI_RECORD_SEPARATOR, 1)[0]
        self.assertLessEqual(len(message), rofi.MAX_MESSAGE_LENGTH)
        self.assertTrue(message.endswith("…"))
        self.assertEqual([False], self.model.calls)

    def test_action_hint_advertises_tab_and_retired_custom_input_is_inert(self) -> None:
        rendered = rofi.render_snapshot(self.value)
        self.assertIn(
            'Enter: <span foreground="#42a5f5" weight="bold">[Open]</span> · Kill', rendered
        )
        self.assertIn("  │  Tab: Cycle actions", rendered)
        self.assertNotIn("Shift+Tab:", rendered)
        self.assertIn(
            "Tab: Cycle actions\u2028\u2028&lt;offline&gt; &amp; busy",
            rofi._action_message(rofi.ContinuationState(), "<offline> & busy"),
        )
        output = self.invoke({"ROFI_RETV": "2", "ROFI_INPUT": "new-name"})
        self.assertEqual("", output)
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
        self.cache_directory = TemporaryDirectory()
        self.addCleanup(self.cache_directory.cleanup)
        self.presentation_cache = PresentationSnapshotCache(Path(self.cache_directory.name))

    def invoke(
        self,
        environ: dict[str, str],
        *,
        model: FakeModel | None = None,
        lifecycle: FakeLifecycle | None = None,
    ) -> str:
        output = io.StringIO()
        with patch("rofi_tmux_plus.rofi._niri_titles", return_value=()), redirect_stdout(output):
            self.assertEqual(
                0,
                rofi.run_rofi(
                    environ,
                    model_service=model or self.model,
                    lifecycle_service=lifecycle or self.lifecycle,
                    config=Config(),
                    presentation_cache=self.presentation_cache,
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

    def kill_mode(self, *, host_id: str = "alpha") -> str:
        return self.invoke(
            {
                "ROFI_RETV": str(rofi.ROFI_RETV_CUSTOM_7),
                "ROFI_DATA": rofi._state_data(
                    rofi.ContinuationState(snapshot_key=self.presentation_cache.store(self.value))
                ),
                "ROFI_INFO": self.session_info(host_id=host_id),
            }
        )

    def test_retired_browse_callbacks_are_inert_even_with_existing_state(self) -> None:
        state = rofi._state_data(
            rofi.ContinuationState(snapshot_key=self.presentation_cache.store(self.value))
        )
        for retv in (
            rofi.ROFI_RETV_CUSTOM_INPUT,
            rofi.ROFI_RETV_DELETE_ENTRY,
            rofi.ROFI_RETV_CUSTOM_4,
        ):
            with self.subTest(retv=retv):
                self.assertEqual(
                    "",
                    self.invoke(
                        {
                            "ROFI_RETV": str(retv),
                            "ROFI_DATA": state,
                            "ROFI_INFO": self.session_info(),
                            "ROFI_INPUT": "must-not-mutate",
                        }
                    ),
                )
        self.assertEqual([], self.model.calls)
        self.assertEqual([], self.lifecycle.opens)
        self.assertEqual([], self.lifecycle.creates)
        self.assertEqual([], self.lifecycle.renames)
        self.assertEqual([], self.lifecycle.kills)

    def test_tab_and_shift_tab_cycle_actions_without_model_reads_and_preserve_row_identity(
        self,
    ) -> None:
        state = rofi._state_data(
            rofi.ContinuationState(snapshot_key=self.presentation_cache.store(self.value))
        )
        selected = self.session_info(host_id="beta")
        kill = self.invoke(
            {
                "ROFI_RETV": str(rofi.ROFI_RETV_CUSTOM_7),
                "ROFI_DATA": state,
                "ROFI_INFO": selected,
            }
        )
        self.assertIn("\x00prompt\x1fTmux › All", kill)
        self.assertNotIn("Tmux › All › Kill", kill)
        self.assertIn('Open · <span foreground="#ef5350" weight="bold">[Kill]</span>', kill)
        self.assertIn("keep-selection", kill)
        self.assertIn("keep-filter", kill)
        self.assertIn("\0new-selection\x1f1", kill)
        self.assertEqual("kill", json.loads(self.data(kill))["action"])
        for row in self.rows(kill):
            options = row_options(row)
            self.assertEqual("true", options["active"])
            self.assertEqual("true", options["urgent"])
        self.assertEqual([], self.model.calls)
        self.assertEqual([], self.lifecycle.kills)

        opened = self.invoke(
            {
                "ROFI_RETV": str(rofi.ROFI_RETV_CUSTOM_8),
                "ROFI_DATA": self.data(kill),
            }
        )
        self.assertIn("Tmux › All", opened)
        self.assertEqual("open", json.loads(self.data(opened))["action"])
        self.assertEqual([], self.model.calls)

    def test_confirmation_defaults_cancel_and_kill_uses_exact_reference(self) -> None:
        kill_mode = self.kill_mode(host_id="beta")
        confirmation = self.invoke(
            {
                "ROFI_RETV": "1",
                "ROFI_DATA": self.data(kill_mode),
                "ROFI_INFO": self.session_info(host_id="beta"),
            }
        )
        rows = self.rows(confirmation)
        self.assertEqual("Cancel", rows[0].split("\0", 1)[0])
        cancel_options = row_options(rows[0])
        kill_options = row_options(rows[1])
        self.assertIn("disconnects 0 live clients", kill_options["display"])
        self.assertNotIn("active", cancel_options)
        self.assertNotIn("urgent", cancel_options)
        self.assertEqual("true", kill_options["active"])
        self.assertEqual("true", kill_options["urgent"])
        canceled = self.invoke(
            {
                "ROFI_RETV": "1",
                "ROFI_DATA": self.data(confirmation),
                "ROFI_INFO": row_options(rows[0])["info"],
            }
        )
        self.assertIn("Tmux › All", canceled)
        self.assertEqual("open", json.loads(self.data(canceled))["action"])
        self.assertEqual([], self.lifecycle.kills)
        confirmation = self.invoke(
            {
                "ROFI_RETV": "1",
                "ROFI_DATA": self.data(self.kill_mode(host_id="beta")),
                "ROFI_INFO": self.session_info(host_id="beta"),
            }
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

    def test_confirmation_tab_is_inert_and_failed_kill_cannot_repeat(self) -> None:
        kill_mode = self.kill_mode()
        confirmation = self.invoke(
            {
                "ROFI_RETV": "1",
                "ROFI_DATA": self.data(kill_mode),
                "ROFI_INFO": self.session_info(),
            }
        )
        self.model.calls.clear()
        inert = self.invoke(
            {"ROFI_RETV": str(rofi.ROFI_RETV_CUSTOM_7), "ROFI_DATA": self.data(confirmation)}
        )
        self.assertIn("Tmux › Confirm kill", inert)
        self.assertEqual([], self.model.calls)
        self.assertEqual("", self.invoke({"ROFI_RETV": "15", "ROFI_DATA": self.data(confirmation)}))

        failed_lifecycle = FakeLifecycle(ContractError("stale_session", "session changed"))
        failed = self.invoke(
            {
                "ROFI_RETV": "1",
                "ROFI_DATA": self.data(confirmation),
                "ROFI_INFO": row_options(self.rows(confirmation)[1])["info"],
            },
            lifecycle=failed_lifecycle,
        )
        self.assertIn("Unable to kill session", failed)
        self.assertEqual(1, len(failed_lifecycle.kills))
        guarded = self.invoke(
            {
                "ROFI_RETV": "1",
                "ROFI_DATA": self.data(failed),
                "ROFI_INFO": row_options(self.rows(failed)[1])["info"],
            },
            lifecycle=failed_lifecycle,
        )
        self.assertIn("kill already attempted", guarded)
        self.assertEqual(1, len(failed_lifecycle.kills))

    def test_confirmation_refuses_changed_typed_selection(self) -> None:
        kill_mode = self.kill_mode()
        confirmation = self.invoke(
            {
                "ROFI_RETV": "1",
                "ROFI_DATA": self.data(kill_mode),
                "ROFI_INFO": self.session_info(),
            }
        )
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
        kill_mode = self.kill_mode()
        editing = self.invoke(
            {
                "ROFI_RETV": "1",
                "ROFI_DATA": self.data(kill_mode),
                "ROFI_INFO": self.session_info(),
            }
        )
        self.assertIn("selected host mesh changed", editing)
        self.assertEqual("open", json.loads(self.data(editing))["action"])
        self.assertEqual([], self.lifecycle.renames)
        self.assertEqual([], self.lifecycle.kills)

    def test_kill_mode_rejects_an_unavailable_selected_host_before_confirmation(self) -> None:
        unavailable = payload(
            hosts=[
                host(
                    "alpha",
                    "Alpha",
                    local=True,
                    sessions=[session("alpha", "$0", "one")],
                    status="error",
                    stale=True,
                    unavailable=True,
                ),
                host("beta", "Beta", local=False, sessions=[session("beta", "$1", "two")]),
            ]
        )
        self.model.values = [unavailable]
        output = self.invoke(
            {
                "ROFI_RETV": "1",
                "ROFI_DATA": self.data(self.kill_mode()),
                "ROFI_INFO": self.session_info(),
            }
        )
        self.assertIn("selected host is unavailable", output)
        self.assertIn("Tmux › All", output)
        data = self.data(output)
        self.assertEqual("open", json.loads(data)["action"])
        self.assertNotIn('"pendingAction"', data)
        rows = self.rows(output)
        selected_index = next(
            index
            for index, row in enumerate(rows)
            if json.loads(row_options(row)["info"])["hostId"] == "alpha"
        )
        self.assertIn(f"\0new-selection\x1f{selected_index}", output)
        self.assertEqual([], self.lifecycle.kills)

    def test_success_is_not_reclassified_when_affected_refresh_fails(self) -> None:
        kill_mode = self.kill_mode()
        confirmation = self.invoke(
            {
                "ROFI_RETV": "1",
                "ROFI_DATA": self.data(kill_mode),
                "ROFI_INFO": self.session_info(),
            }
        )
        self.model.host_refresh_error = ContractError(
            "operation_failed", "remote inventory unavailable"
        )
        output = self.invoke(
            {
                "ROFI_RETV": "1",
                "ROFI_DATA": self.data(confirmation),
                "ROFI_INFO": row_options(self.rows(confirmation)[1])["info"],
            }
        )
        self.assertIn("Session killed.", output)
        self.assertIn("Refresh warning", output)
        self.assertEqual(1, len(self.lifecycle.kills))
        self.assertIn("Tmux › All", output)
        self.assertIn("one", output)
        data = self.data(output)
        self.assertEqual("open", json.loads(data)["action"])
        self.assertNotIn('"pendingAction"', data)
        self.assertEqual(rofi.VIEW_ALL, json.loads(data)["navigation"]["view"])

    def test_unknown_action_blocks_enter_visibly_and_retired_callbacks_remain_inert(self) -> None:
        invalid = json.dumps(
            {
                "version": 1,
                "navigation": {"view": rofi.VIEW_ALL},
                "action": "rename",
            }
        )
        for retv in ("1", str(rofi.ROFI_RETV_CUSTOM_7)):
            with self.subTest(retv=retv):
                output = self.invoke(
                    {
                        "ROFI_RETV": retv,
                        "ROFI_DATA": invalid,
                        "ROFI_INFO": self.session_info(),
                        "ROFI_INPUT": "new-name",
                    }
                )
                self.assertIn("Action state is invalid", output)
                self.assertIn('"blockedAction":true', self.data(output))
        self.assertEqual([], self.lifecycle.opens)
        self.assertEqual([], self.lifecycle.creates)
        self.assertEqual([], self.lifecycle.renames)
        self.assertEqual([], self.lifecycle.kills)
        self.assertEqual("", self.invoke({"ROFI_RETV": "2", "ROFI_DATA": invalid}))
        self.assertEqual("", self.invoke({"ROFI_RETV": "15", "ROFI_DATA": invalid}))

    def test_pending_action_state_budget_handles_unicode_and_selection_boundary(self) -> None:
        selection = json.loads(self.session_info())
        selection["name"] = "é" * 1800
        safe = rofi._new_action("confirm-kill", rofi.NavigationState(), selection=selection)
        state = rofi._error_state(
            rofi.ContinuationState(action=rofi.ACTION_KILL, pending_action=safe),
            "é" * rofi.MAX_MESSAGE_LENGTH,
            now=100,
            key="test",
        )
        encoded = rofi._state_data(state)
        self.assertLessEqual(len(encoded), rofi.MAX_DATA_LENGTH)
        self.assertFalse(rofi.parse_continuation_state(encoded).blocked_action)
        with self.assertRaises(ContractError):
            rofi._new_action(
                "confirm-kill",
                rofi.NavigationState(),
                selection={**selection, "name": "é" * 2048},
            )

        selection = json.loads(self.session_info())
        selection["serverGeneration"] = "é" * 2048
        oversized_action = json.dumps(
            {
                "version": 1,
                "navigation": {"view": rofi.VIEW_ALL},
                "action": "kill",
                "pendingAction": {
                    "kind": "rename",
                    "origin": {"view": rofi.VIEW_ALL},
                    "selection": selection,
                },
            },
            ensure_ascii=True,
        )
        self.assertLessEqual(len(oversized_action), rofi.MAX_DATA_LENGTH)
        self.assertTrue(rofi.parse_continuation_state(oversized_action).blocked_action)
        output = self.invoke(
            {"ROFI_RETV": "1", "ROFI_DATA": oversized_action, "ROFI_INPUT": "new-name"}
        )
        self.assertIn("Action state is invalid", output)
        self.assertEqual([], self.lifecycle.renames)


class RofiRefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cache_directory = TemporaryDirectory()
        self.addCleanup(self.cache_directory.cleanup)
        self.presentation_cache = PresentationSnapshotCache(Path(self.cache_directory.name))
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

    def test_initial_render_carries_cached_snapshot_for_arrow_callbacks(self) -> None:
        model = FakeModel(self.fresh)
        output = io.StringIO()
        with (
            patch("rofi_tmux_plus.rofi._niri_titles", return_value=()),
            redirect_stdout(output),
        ):
            rofi.run_rofi(
                {"ROFI_RETV": "0"},
                model_service=model,
                lifecycle_service=FakeLifecycle(),
                config=Config(),
                presentation_cache=self.presentation_cache,
            )
        rendered = output.getvalue()
        self.assertIn("\0data\x1f", rendered)
        data = rendered.split("\0data\x1f", 1)[1].split("\n", 1)[0]
        state = json.loads(data)
        self.assertTrue(rofi.valid_snapshot_key(state["snapshotKey"]))
        self.assertEqual(self.fresh, self.presentation_cache.load(state["snapshotKey"]))

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
                presentation_cache=self.presentation_cache,
            )
        initial = output.getvalue()
        self.assertIn("Refreshing in background", initial)
        self.assertIn('"refreshDeadline":', initial)
        _, initial_rows = rendered_records(initial)
        initial_options = row_options(initial_rows[0])
        self.assertNotIn("active", initial_options)
        self.assertNotIn("urgent", initial_options)
        data = initial.split("\0data\x1f", 1)[1].split("\n", 1)[0]
        initial_state = json.loads(data)
        self.assertTrue(rofi.valid_snapshot_key(initial_state["snapshotKey"]))
        self.assertEqual(self.old, self.presentation_cache.load(initial_state["snapshotKey"]))
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
                presentation_cache=self.presentation_cache,
            )
        completed = output.getvalue()
        self.assertIn("keep-selection", completed)
        self.assertIn("keep-filter", completed)
        self.assertNotIn("Refreshing in background", completed)
        self.assertIn("delay: 0", completed)
        self.assertEqual([True, False], model.calls)

    def test_refresh_preserves_kill_mode_and_highlighted_identity_through_reorder(self) -> None:
        before = payload(
            hosts=[
                host("alpha", "Alpha", local=True, sessions=[session("alpha", "$0", "alpha")]),
                host("beta", "Beta", local=False, sessions=[session("beta", "$1", "beta")]),
            ]
        )
        after = payload(
            hosts=[
                host(
                    "alpha",
                    "Alpha",
                    local=True,
                    sessions=[session("alpha", "$0", "alpha", activity=20)],
                ),
                host(
                    "beta",
                    "Beta",
                    local=False,
                    sessions=[session("beta", "$1", "beta", activity=30)],
                ),
            ]
        )
        _, before_rows = rendered_records(rofi.render_snapshot(before, now=200, titles=()))
        selected = json.loads(row_options(before_rows[1])["info"])
        state = rofi._state_data(
            rofi.ContinuationState(
                action=rofi.ACTION_KILL,
                highlighted=rofi._highlighted_selection(selected),
            )
        )
        output = io.StringIO()
        with patch("rofi_tmux_plus.rofi._niri_titles", return_value=()), redirect_stdout(output):
            rofi.run_rofi(
                {"ROFI_RETV": str(rofi.ROFI_RETV_CUSTOM_19), "ROFI_DATA": state},
                model_service=FakeModel(after),
                lifecycle_service=FakeLifecycle(),
                config=Config(),
                presentation_cache=self.presentation_cache,
            )
        rendered = output.getvalue()
        self.assertIn("Tmux › All", rendered)
        self.assertIn("[Kill]</span>", rendered)
        self.assertIn("\0new-selection\x1f0", rendered)
        rows = rendered_records(rendered)[1]
        self.assertEqual("beta", json.loads(row_options(rows[0])["info"])["name"])
        self.assertEqual(
            "kill",
            json.loads(rendered.split("\0data\x1f", 1)[1].split(rofi.ROFI_RECORD_SEPARATOR, 1)[0])[
                "action"
            ],
        )

    def test_scope_change_clears_prior_highlight_before_later_timeout(self) -> None:
        snapshot = payload(
            hosts=[
                host("alpha", "Alpha", local=True, sessions=[session("alpha", "$0", "alpha")]),
                host("beta", "Beta", local=False, sessions=[session("beta", "$1", "beta")]),
            ]
        )
        _, rows = rendered_records(rofi.render_snapshot(snapshot, now=200, titles=()))
        selected = json.loads(row_options(rows[0])["info"])
        state = rofi._state_data(
            rofi.ContinuationState(
                snapshot_key=self.presentation_cache.store(snapshot),
                highlighted=rofi._highlighted_selection(selected),
            )
        )
        output = io.StringIO()
        with patch("rofi_tmux_plus.rofi._niri_titles", return_value=()), redirect_stdout(output):
            rofi.run_rofi(
                {"ROFI_RETV": str(rofi.ROFI_RETV_CUSTOM_2), "ROFI_DATA": state},
                presentation_cache=self.presentation_cache,
            )
        scoped = output.getvalue()
        scoped_data = scoped.split("\0data\x1f", 1)[1].split(rofi.ROFI_RECORD_SEPARATOR, 1)[0]
        scoped_state = json.loads(scoped_data)
        self.assertEqual({"view": rofi.VIEW_LOCAL, "hostId": "alpha"}, scoped_state["navigation"])
        self.assertNotIn("highlighted", scoped_state)

        output = io.StringIO()
        with patch("rofi_tmux_plus.rofi._niri_titles", return_value=()), redirect_stdout(output):
            rofi.run_rofi(
                {"ROFI_RETV": str(rofi.ROFI_RETV_CUSTOM_19), "ROFI_DATA": scoped_data},
                model_service=FakeModel(snapshot),
                lifecycle_service=FakeLifecycle(),
                config=Config(),
                presentation_cache=self.presentation_cache,
            )
        refreshed = output.getvalue()
        self.assertIn("keep-selection", refreshed)
        self.assertNotIn("\0new-selection\x1f", refreshed)

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
                        presentation_cache=self.presentation_cache,
                    )
                rendered = output.getvalue()
                self.assertIn("worker stopped", rendered)
                self.assertNotIn('"refreshDeadline":', rendered)
                self.assertIn('"errorDeadline":', rendered)
                self.assertEqual([False], model.calls)

    def test_alt_r_is_bounded_foreground_refresh_without_background_restart(self) -> None:
        model = FakeModel(self.fresh)
        _, rows = rendered_records(rofi.render_snapshot(self.fresh, now=200, titles=()))
        selected = json.loads(row_options(rows[0])["info"])
        state = rofi._state_data(
            rofi.ContinuationState(
                action=rofi.ACTION_KILL,
                highlighted=rofi._highlighted_selection(selected),
            )
        )
        output = io.StringIO()
        with patch("rofi_tmux_plus.rofi._niri_titles", return_value=()), redirect_stdout(output):
            rofi.run_rofi(
                {"ROFI_RETV": str(rofi.ROFI_RETV_CUSTOM_1), "ROFI_DATA": state},
                model_service=model,
                lifecycle_service=FakeLifecycle(),
                config=Config(),
                presentation_cache=self.presentation_cache,
            )
        self.assertEqual(1, model.refresh_calls)
        self.assertNotIn("Refreshing in background", output.getvalue())
        self.assertIn("Tmux › Local", output.getvalue())
        self.assertIn("[Kill]</span>", output.getvalue())
        self.assertIn("\0new-selection\x1f0", output.getvalue())


class EntryPointTests(unittest.TestCase):
    def test_cli_auto_detects_rofi_without_changing_json_dispatch(self) -> None:
        with (
            patch.dict(os.environ, {"ROFI_RETV": "0"}),
            patch("rofi_tmux_plus.rofi.run_rofi", return_value=7) as run,
        ):
            self.assertEqual(7, cli.main([]))
        run.assert_called_once()

    def test_cli_routes_selected_row_callback_argv_to_rofi(self) -> None:
        with (
            patch.dict(
                os.environ,
                {"ROFI_RETV": "11", "ROFI_INFO": '{"type":"session"}'},
                clear=True,
            ),
            patch("rofi_tmux_plus.rofi.run_rofi", return_value=7) as run,
        ):
            self.assertEqual(7, cli.main(["visible row\nsecondary row"]))
        run.assert_called_once()

    def test_cli_routes_custom_input_matching_json_command_to_rofi(self) -> None:
        with (
            patch.dict(
                os.environ,
                {"ROFI_RETV": "2", "ROFI_INPUT": "inventory"},
                clear=True,
            ),
            patch("rofi_tmux_plus.rofi.run_rofi", return_value=7) as run,
        ):
            self.assertEqual(7, cli.main(["inventory"]))
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
                    "hosts": [host("local", "Local", local=True)],
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
        self.assertEqual("local", json.loads(output.getvalue())["hosts"][0]["hostId"])

    def test_contract_command_wins_over_inherited_rofi_callback_environment(self) -> None:
        inventory = type(
            "Inventory",
            (),
            {
                "inventory": lambda self, **_kwargs: {
                    "schemaVersion": 1,
                    "generatedAt": 1,
                    "meshRevision": None,
                    "hosts": [host("local", "Local", local=True)],
                }
            },
        )()
        output = io.StringIO()
        with (
            patch.dict(
                os.environ,
                {
                    "ROFI_RETV": "1",
                    "ROFI_INFO": '{"type":"session"}',
                    "ROFI_DATA": '{"schemaVersion":1}',
                },
                clear=True,
            ),
            patch("rofi_tmux_plus.cli._inventory_service", return_value=inventory),
            patch("rofi_tmux_plus.rofi.run_rofi") as run,
            redirect_stdout(output),
        ):
            self.assertEqual(0, cli.main(["inventory", "--json"]))
        run.assert_not_called()
        self.assertEqual("local", json.loads(output.getvalue())["hosts"][0]["hostId"])
