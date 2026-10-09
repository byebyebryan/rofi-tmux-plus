"""Read-only prepared picker facts, local expiry and explicitly owned tickets."""

from __future__ import annotations

import copy
import json
import os
import time
import uuid
from collections.abc import Mapping

from .errors import ContractError, clean_message
from .observer_client import observer_api
from .picker_model import PickerModel
from .picker_notify import WATCH_REQUIRED_ENV, delivery_current, read_notification
from .picker_runtime import read_private, runtime_root, write_private

RUNTIME_ENV = "ROFI_TMUX_PLUS_RUNTIME"
CONTEXT_ENV = "ROFI_TMUX_PLUS_CONTEXT_ID"
TICKET_FILE = "refresh-ticket.json"
TICKET_LIMIT = 16384
TERMINAL = frozenset({"complete", "failed", "stale_scope", "deadline"})


def boottime_ms() -> int:
    return time.clock_gettime_ns(time.CLOCK_BOOTTIME) // 1000000


def clock_domain() -> dict[str, str]:
    # Cached presentation leases belong to this boot and time namespace.
    with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as stream:
        boot = stream.read(128).strip()
    namespace = os.readlink("/proc/self/ns/time").replace("[", "").replace("]", "")
    return {"bootId": boot, "timeNamespace": namespace}


def local_scope(environ=None):
    environment = os.environ if environ is None else environ
    context = observer_api().prepared.desktop_context_id(environment)
    selected = environment.get(CONTEXT_ENV)
    if selected is not None and selected != context:
        raise ValueError("picker desktop context changed")
    return clock_domain(), context


def apply_expiry(payload: Mapping, *, now=None, scope=None) -> dict:
    """Revoke cached positives on every render, including cache-only navigation."""
    if not isinstance(payload.get("prepared"), Mapping):
        return dict(payload)
    value = copy.deepcopy(dict(payload))
    lease = value["prepared"]
    now = boottime_ms() if now is None else now
    try:
        clock, context = local_scope() if scope is None else scope
        current_scope = lease["clock"] == clock and lease["contextId"] == context
    except (OSError, ValueError, ContractError, KeyError):
        current_scope = False
    current_scope = current_scope and type(lease.get("checkedAt")) is int
    current_scope = current_scope and lease["checkedAt"] <= now
    watch_current = True
    if lease.get("watchRuntime") is not None:
        try:
            watch_current = delivery_current(read_notification(lease["watchRuntime"]), now)
        except (OSError, ValueError, KeyError):
            watch_current = False
    for host in value.get("hosts", []):
        expiry = host.get("ownerExpiry")
        current = current_scope and type(expiry) is int and expiry > now
        if current and not watch_current:
            host.update(status="unavailable", unavailable=True)
            host["error"] = {"code": "watch_unavailable", "message": "picker updates unavailable"}
        if not current and (host.get("status") == "ok" or not current_scope):
            host.update(status="stale", stale=True, unavailable=True, ownerFactsExpired=True)
            host["error"] = {
                "code": "owner_expired" if current_scope else "stale_scope",
                "message": "owner facts expired" if current_scope else "picker context changed",
            }
    desktop_expiry = lease.get("desktopExpiry")
    if (
        not current_scope
        or not watch_current
        or type(desktop_expiry) is not int
        or desktop_expiry <= now
    ):
        value["viewerFactsCurrent"] = False
    for host in value.get("hosts", []):
        expiry = host.get("viewerExpiry", desktop_expiry)
        if (
            not current_scope
            or not watch_current
            or type(expiry) is not int
            or expiry <= now
            or host.get("viewerFactsCurrent", value.get("viewerFactsCurrent")) is not True
        ):
            host["viewerFactsCurrent"] = False
            for session in host.get("sessions", []):
                session["localViewer"] = {"state": "unknown", "reason": "desktop_expired"}
    return value


def project_frame(frame: dict, *, now: int, scope) -> dict:
    """Translate validated Fleet v1 into private presentation facts, never v1 IPC."""
    view = frame["snapshot"]
    if view is None:
        raise ContractError("operation_failed", "prepared reader returned no snapshot")
    mesh = view["mesh"]
    desktop = view["desktop"]
    payload = {
        "schemaVersion": 1,
        "generatedAt": time.time_ns() // 1000000,
        "meshRevision": mesh["revision"],
        "hostCatalog": [],
        "hosts": [],
        "viewerFactsCurrent": desktop["state"] == "ready",
        "prepared": {
            "readerId": frame["readerId"],
            "contextId": frame["contextId"],
            "clock": frame["clock"],
            "checkedAt": now,
            "viewRevision": view["viewRevision"],
            "desktopExpiry": desktop["expiresAt"] if desktop["state"] == "ready" else None,
        },
    }
    for host in view["hosts"]:
        owner = host["owner"]
        receipt = owner["receipt"]
        current = (
            mesh["state"] in {"ready", "local_only"}
            and receipt is not None
            and receipt["state"] == "ready"
            and type(owner["localExpiry"]) is int
            and owner["localExpiry"] > now
        )
        catalog = {key: host[key] for key in ("hostId", "display", "local")}
        payload["hostCatalog"].append(catalog)
        sample, source = owner["sample"], owner["source"]
        row = {
            **catalog,
            "status": "ok" if current else "unavailable",
            "unavailable": not current,
            "observedAt": sample["observedAt"] if sample is not None else None,
            "nativeHostname": source["nativeHostname"] if source is not None else None,
            "serverGeneration": owner["serverGeneration"],
            "ownerExpiry": owner["localExpiry"] if current else None,
            "viewerFactsCurrent": desktop["state"] == "ready",
            "viewerExpiry": desktop["expiresAt"] if desktop["state"] == "ready" else None,
            "sessions": copy.deepcopy(host["sessions"]),
        }
        bindings = host.get("localBindings")
        if bindings is not None:
            try:
                observer_api().bindings_contract.validate_fleet_bindings(
                    bindings,
                    host,
                    context_id=view["contextId"],
                    clock_value=view["clock"],
                    now=now,
                )
            except (ImportError, ValueError, TypeError, KeyError) as error:
                raise ContractError(
                    "operation_failed", "Prepared local bindings are invalid"
                ) from error
            binding_current = current and bindings["receipt"]["state"] == "ready"
            row["viewerFactsCurrent"] = binding_current
            row["viewerExpiry"] = bindings["receipt"]["expiresAt"] if binding_current else None
            by_ref = {
                tuple(
                    item["sessionRef"][key]
                    for key in ("hostId", "serverGeneration", "sessionId", "createdAt")
                ): item["association"]
                for item in bindings["rows"]
            }
            for session in row["sessions"]:
                association = by_ref[
                    tuple(
                        session[key]
                        for key in ("hostId", "serverGeneration", "sessionId", "createdAt")
                    )
                ]
                presence = {
                    "state": association["state"],
                    "reason": None
                    if association["state"] in {"open", "none"}
                    else association["reason"],
                }
                if association["state"] == "open":
                    presence.update(
                        confidence="matched",
                        evidence="retained_native_association",
                        resolvedAt=association["resolvedAt"],
                    )
                session["localViewer"] = presence
        if not current:
            row["error"] = copy.deepcopy(
                owner["error"]
                or view["error"]
                or {"code": "owner_unavailable", "message": "owner facts are not current"}
            )
        payload["hosts"].append(row)
    return apply_expiry(payload, now=now, scope=scope)


def _uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _ticket_record(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"clock", "contextId", "hostId", "ticket"}
        or not isinstance(value["clock"], dict)
        or not isinstance(value["contextId"], str)
        or not isinstance(value["hostId"], str)
    ):
        raise ValueError("invalid retained refresh scope")
    ticket = value["ticket"]
    if (
        not isinstance(ticket, dict)
        or not _uuid(ticket.get("id"))
        or not _uuid(ticket.get("publisherId"))
        or ticket.get("state") not in TERMINAL | {"accepted", "coalesced", "running"}
        or type(ticket.get("requestedAt")) is not int
        or type(ticket.get("deadlineAt")) is not int
        or not 0 <= ticket["requestedAt"] < ticket["deadlineAt"]
        or not isinstance(ticket.get("sources"), list)
        or not 1 <= len(ticket["sources"]) <= 32
    ):
        raise ValueError("invalid retained refresh ticket")
    for source in ticket["sources"]:
        if (
            not isinstance(source, dict)
            or source.get("error") is not None
            and not isinstance(source["error"], dict)
        ):
            raise ValueError("invalid retained refresh result")
    return value


def ticket_marker(record) -> dict | None:
    if record is None:
        return None
    ticket = record["ticket"]
    state = ticket["state"]
    marker = {
        "state": "running"
        if state not in TERMINAL
        else {"stale_scope": "stale", "deadline": "stalled"}.get(state, state),
        "updatedAt": ticket["requestedAt"],
        "ticketId": ticket["id"],
        "publisherId": ticket["publisherId"],
    }
    if state in {"failed", "stale_scope", "deadline"}:
        messages = [row["error"].get("message") for row in ticket["sources"] if row.get("error")]
        marker["message"] = clean_message(
            "; ".join(str(message) for message in messages if message)
            or "requested refresh " + state
        )
    return marker


class PreparedModelService:
    """Cached access creates no collectors, refresh jobs, transport or services."""

    prepared = True

    def __init__(self, config=None, *, environ=None, api=None, now=boottime_ms, scope=local_scope):
        self.environment = dict(os.environ if environ is None else environ)
        self.api = observer_api().prepared if api is None else api
        self.now = now
        self.scope = scope
        self.context = self.api.desktop_context_id(self.environment)
        if self.environment.get(CONTEXT_ENV, self.context) != self.context:
            raise ContractError("operation_failed", "picker desktop context changed")
        self.runtime = self.environment.get(RUNTIME_ENV)

    def _record(self):
        if self.runtime is None:
            return None
        raw = read_private(self.runtime, TICKET_FILE, limit=TICKET_LIMIT)
        return None if raw is None else _ticket_record(json.loads(raw))

    def _retain(self, value):
        if self.runtime is None:
            raise ContractError("operation_failed", "refresh requires a launcher-owned picker")
        _ticket_record(value)
        write_private(
            self.runtime,
            TICKET_FILE,
            json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode(),
            limit=TICKET_LIMIT,
        )

    def _read(self, **arguments):
        try:
            return self.api.read_cached(self.context, **arguments)
        except Exception as error:
            raise ContractError(
                "operation_failed", "Prepared Tmux Observer unavailable: " + clean_message(error)
            ) from error

    def _finish_locally(self, record, state, message):
        record = copy.deepcopy(record)
        ticket = record["ticket"]
        ticket["state"] = state
        for row in ticket["sources"]:
            if row.get("state") not in TERMINAL:
                row.update(state=state, error={"code": state, "message": message})
        self._retain(record)
        return record

    def _model(self, frame, record=None):
        payload = project_frame(frame, now=self.now(), scope=self.scope(self.environment))
        if self.environment.get(WATCH_REQUIRED_ENV) == "1":
            if self.runtime is None:
                raise ContractError("operation_failed", "picker watch has no owned runtime")
            payload["prepared"]["watchRuntime"] = self.runtime
            payload = apply_expiry(payload, now=self.now(), scope=self.scope(self.environment))
        marker = ticket_marker(record)
        if marker is not None:
            payload["remoteRefresh"] = marker
        return PickerModel(payload, False)

    def load(self, *, start_refresh=False, deadline=None):
        # Legacy callers may still pass start_refresh=True. It never admits work.
        record = self._record()
        if record is not None and record["ticket"]["state"] not in TERMINAL:
            clock, context = self.scope(self.environment)
            if record["clock"] != clock or record["contextId"] != context:
                record = self._finish_locally(record, "stale_scope", "picker context changed")
            elif self.now() >= record["ticket"]["deadlineAt"]:
                record = self._finish_locally(
                    record, "deadline", "requested refresh deadline elapsed"
                )
            else:
                ticket = record["ticket"]
                try:
                    frame = self._read(
                        operation="refresh_status",
                        expected_host=record["hostId"],
                        publisher_id=ticket["publisherId"],
                        ticket_id=ticket["id"],
                    )
                    outcome = frame.get("ticket")
                    if (
                        outcome is None
                        or outcome["id"] != ticket["id"]
                        or outcome["publisherId"] != ticket["publisherId"]
                    ):
                        raise ContractError(
                            "operation_failed", "reader omitted the matching ticket"
                        )
                    record = {**record, "ticket": outcome}
                    self._retain(record)
                    return self._model(frame, record)
                except ContractError as error:
                    record = self._finish_locally(record, "failed", error.message)
        frame = self._read()
        return self._model(frame, record)

    def _refresh(self, host_id=None, revision=None, *, check_revision=False):
        if self.runtime is None:
            raise ContractError("operation_failed", "refresh requires a launcher-owned picker")
        with runtime_root(self.runtime):
            pass
        frame = self._read()
        view = frame["snapshot"]
        if check_revision and view["mesh"]["revision"] != revision:
            raise ContractError("stale_mesh", "the selected host mesh changed")
        hosts = view["hosts"]
        if host_id is not None:
            hosts = [host for host in hosts if host["hostId"] == host_id]
            if not hosts:
                raise ContractError("unknown_host", "the selected host is no longer configured")
        sources = [{"hostId": host["hostId"], "source": "owner"} for host in hosts]
        if host_id is None and view["desktop"]["state"] != "unsupported":
            sources.append({"hostId": view["mesh"]["localHostId"], "source": "desktop"})
        result = self._read(
            operation="refresh",
            expected_host=view["mesh"]["localHostId"],
            publisher_id=frame["readerId"],
            sources=sources,
        )
        clock, context = self.scope(self.environment)
        record = {
            "clock": clock,
            "contextId": context,
            "hostId": view["mesh"]["localHostId"],
            "ticket": result["ticket"],
        }
        self._retain(record)
        return self._model(result, record)

    def refresh_now(self):
        return self._refresh()

    def refresh_host(self, host_id, revision):
        return self._refresh(host_id, revision, check_revision=True)

    def refresh_host_current(self, host_id):
        return self._refresh(host_id)
