from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reference_frontend.viewer_cache import ViewerObservationCache, _observation


def _reference(
    host_id: str = "beta",
    *,
    generation: str = "tmux-v1:beta",
    session_id: str = "$7",
    created: int = 30,
) -> dict[str, object]:
    return {
        "hostId": host_id,
        "serverGeneration": generation,
        "sessionId": session_id,
        "createdAt": created,
    }


def _row(reference: dict[str, object], state: dict[str, str]) -> dict[str, object]:
    return {**reference, "localViewer": state}


def _response(
    revision: str,
    observed_at: int,
    hosts: list[dict[str, object]],
    *,
    endpoint_host: str = "alpha",
) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "meshRevision": revision,
        "generatedAt": observed_at,
        "viewerEndpoint": {"hostId": endpoint_host, "observedAt": observed_at},
        "hosts": hosts,
    }


def _host(host_id: str, rows: list[dict[str, object]]) -> dict[str, object]:
    return {"hostId": host_id, "status": "ok", "sessions": rows}


def _payload(reference: dict[str, object]) -> dict[str, object]:
    return {
        "hosts": [
            {
                "hostId": reference["hostId"],
                "status": "ok",
                "sessions": [dict(reference)],
            }
        ]
    }


class ViewerObservationCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.now = 100_000
        self.context = "desktop-one"
        self.revision = "sha256:" + "a" * 64
        self.cache = ViewerObservationCache(
            Path(self.temporary.name),
            now_millis=lambda: self.now,
            context_id=lambda: self.context,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _merge(
        self,
        response: dict[str, object],
        *,
        expected_context: str | None = None,
        endpoint_host: str = "alpha",
        expected_hosts: list[str] | None = None,
    ) -> bool:
        return self.cache.merge_response(
            response,
            mesh_revision=response["meshRevision"],
            expected_context=expected_context or self.context,
            endpoint_host_id=endpoint_host,
            expected_host_ids=expected_hosts or [host["hostId"] for host in response["hosts"]],
        )

    def test_ten_second_ttl_full_reference_join_and_endpoint_identity(self) -> None:
        reference = _reference()
        response = _response(
            self.revision,
            self.now,
            [_host("beta", [_row(reference, {"state": "open", "confidence": "confirmed"})])],
        )
        self.assertTrue(self._merge(response))

        payload = _payload(reference)
        self.assertFalse(
            self.cache.decorate(
                payload,
                mesh_revision=self.revision,
                endpoint_host_id="alpha",
            )
        )
        self.assertEqual(
            {"state": "open", "confidence": "confirmed"},
            payload["hosts"][0]["sessions"][0]["localViewer"],
        )
        self.assertEqual("alpha", payload["viewerEndpoint"]["hostId"])
        self.assertEqual(100_000, payload["viewerObservedAt"])

        wrong_endpoint = _payload(reference)
        self.assertTrue(
            self.cache.decorate(
                wrong_endpoint,
                mesh_revision=self.revision,
                endpoint_host_id="other",
            )
        )
        self.assertEqual(
            {"state": "unknown", "reason": "inventory_incomplete"},
            wrong_endpoint["hosts"][0]["sessions"][0]["localViewer"],
        )

        self.now += 10_000
        expired = _payload(reference)
        self.assertTrue(
            self.cache.decorate(
                expired,
                mesh_revision=self.revision,
                endpoint_host_id="alpha",
            )
        )
        self.assertEqual(
            {"state": "unknown", "reason": "inventory_incomplete"},
            expired["hosts"][0]["sessions"][0]["localViewer"],
        )

    def test_boolean_schema_and_foreign_owned_cache_are_unknown(self) -> None:
        reference = _reference()
        response = _response(
            self.revision,
            self.now,
            [_host("beta", [_row(reference, {"state": "open", "confidence": "confirmed"})])],
        )
        self.assertTrue(self._merge(response))
        with patch("reference_frontend.viewer_cache.os.getuid", return_value=os.getuid() + 1):
            self.assertIsNone(self.cache._read(self.revision, self.context))
        value = json.loads(self.cache._path.read_text())
        value["schemaVersion"] = True
        self.cache._path.write_text(json.dumps(value))
        self.assertIsNone(self.cache._read(self.revision, self.context))

    def test_context_mesh_and_complete_reference_changes_invalidate_positive_state(self) -> None:
        reference = _reference()
        response = _response(
            self.revision,
            self.now,
            [_host("beta", [_row(reference, {"state": "open", "confidence": "matched"})])],
        )
        self.assertTrue(self._merge(response))

        self.context = "desktop-two"
        changed_context = _payload(reference)
        self.assertTrue(
            self.cache.decorate(
                changed_context,
                mesh_revision=self.revision,
                endpoint_host_id="alpha",
            )
        )
        self.assertEqual(
            "unknown", changed_context["hosts"][0]["sessions"][0]["localViewer"]["state"]
        )

        self.context = "desktop-one"
        changed_mesh = _payload(reference)
        self.assertTrue(
            self.cache.decorate(
                changed_mesh,
                mesh_revision="sha256:" + "b" * 64,
                endpoint_host_id="alpha",
            )
        )
        changed_reference = _payload(_reference(generation="tmux-v1:restarted"))
        self.assertTrue(
            self.cache.decorate(
                changed_reference,
                mesh_revision=self.revision,
                endpoint_host_id="alpha",
            )
        )
        self.assertEqual(
            "unknown", changed_reference["hosts"][0]["sessions"][0]["localViewer"]["state"]
        )

    def test_partial_response_is_rejected_and_old_result_cannot_overwrite(self) -> None:
        beta_ref = _reference("beta")
        gamma_ref = _reference("gamma")
        full = _response(
            self.revision,
            self.now,
            [
                _host("beta", [_row(beta_ref, {"state": "open", "confidence": "confirmed"})]),
                _host("gamma", [_row(gamma_ref, {"state": "none"})]),
            ],
        )
        self.assertTrue(self._merge(full))
        self.now += 10_000
        partial = _response(
            self.revision,
            self.now,
            [_host("gamma", [_row(gamma_ref, {"state": "none"})])],
        )
        self.assertFalse(self._merge(partial, expected_hosts=["beta", "gamma"]))
        beta_payload = _payload(beta_ref)
        self.assertTrue(
            self.cache.decorate(
                beta_payload,
                mesh_revision=self.revision,
                endpoint_host_id="alpha",
            )
        )
        self.assertEqual("unknown", beta_payload["hosts"][0]["sessions"][0]["localViewer"]["state"])
        older = _response(
            self.revision,
            self.now - 10_001,
            [_host("beta", [_row(beta_ref, {"state": "open", "confidence": "confirmed"})])],
        )
        self.assertFalse(self._merge(older))

    def test_unavailable_owner_rows_remain_unknown_without_immediate_rescan(self) -> None:
        reference = _reference()
        unavailable = {
            "hostId": "beta",
            "status": "unreachable",
            "sessions": [],
        }
        response = _response(self.revision, self.now, [unavailable])
        self.assertTrue(self._merge(response))

        payload = _payload(reference)
        host = payload["hosts"][0]
        host["status"] = "unreachable"
        host["stale"] = True
        self.assertFalse(
            self.cache.decorate(
                payload,
                mesh_revision=self.revision,
                endpoint_host_id="alpha",
            )
        )
        self.assertEqual(
            {"state": "unknown", "reason": "inventory_incomplete"},
            host["sessions"][0]["localViewer"],
        )

    def test_cache_reader_rejects_symlinks_fifos_and_oversized_regular_files(self) -> None:
        cache_path = self.cache._path
        target = Path(self.temporary.name) / "target.json"
        target.write_text("{}", encoding="ascii")
        cache_path.symlink_to(target)
        self.assertIsNone(self.cache._read(self.revision, self.context))
        cache_path.unlink()

        os.mkfifo(cache_path)
        self.assertIsNone(self.cache._read(self.revision, self.context))
        cache_path.unlink()

        cache_path.write_bytes(b"x" * (4 * 1024 * 1024 + 1))
        with patch("reference_frontend.viewer_cache.os.read") as read:
            self.assertIsNone(self.cache._read(self.revision, self.context))
        read.assert_not_called()

    def test_malformed_observation_enums_fail_closed_without_type_errors(self) -> None:
        self.assertIsNone(_observation({"state": "open", "confidence": []}))
        self.assertIsNone(_observation({"state": "unknown", "reason": {"bad": "type"}}))

    def test_private_cache_permissions_are_restrictive(self) -> None:
        reference = _reference()
        response = _response(
            self.revision,
            self.now,
            [_host("beta", [_row(reference, {"state": "none"})])],
        )
        self.assertTrue(self._merge(response))
        self.assertEqual(stat.S_IMODE(self.cache.directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.cache._path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
