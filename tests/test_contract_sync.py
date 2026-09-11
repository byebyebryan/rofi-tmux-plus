from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
SSH_ROOT = ROOT.parent / "rofi-ssh-plus"
SYNC = ROOT / "scripts" / "check-contract-sync"
HOST_BUNDLE = "contracts/host-mesh-v1"


@unittest.skipUnless(SSH_ROOT.is_dir(), "sibling rofi-ssh-plus checkout is unavailable")
class ContractSyncTests(unittest.TestCase):
    def copies(self) -> tuple[tempfile.TemporaryDirectory[str], Path, Path]:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        source = root / "ssh"
        target = root / "tmux"
        shutil.copytree(SSH_ROOT, source, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copytree(ROOT, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        return temporary, source, target

    @staticmethod
    def git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            check=check,
            capture_output=True,
            text=True,
        )

    def commit(self, root: Path, *paths: str) -> None:
        self.git(root, "add", *paths)
        environment = os.environ.copy()
        environment.update(
            {
                "GIT_AUTHOR_NAME": "contract-sync-test",
                "GIT_AUTHOR_EMAIL": "contract-sync-test@example.invalid",
                "GIT_COMMITTER_NAME": "contract-sync-test",
                "GIT_COMMITTER_EMAIL": "contract-sync-test@example.invalid",
            }
        )
        subprocess.run(
            ["git", "-C", str(root), "commit", "-m", "test: advance producer"],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )

    @staticmethod
    def rewrite_manifest(bundle: Path) -> None:
        files = sorted(
            (
                path.relative_to(bundle).as_posix()
                for path in bundle.rglob("*")
                if path.is_file() and path.name not in {"SHA256SUMS", "SOURCE.json"}
            ),
            key=lambda name: name.encode(),
        )
        manifest = "".join(
            f"{hashlib.sha256((bundle / name).read_bytes()).hexdigest()}  {name}\n"
            for name in files
        )
        (bundle / "SHA256SUMS").write_text(manifest, encoding="utf-8")

    def run_sync(self, source: Path, target: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(target / "scripts/check-contract-sync"), str(source)],
            check=False,
            capture_output=True,
            text=True,
        )

    def test_later_non_contract_producer_commit_is_accepted(self) -> None:
        temporary, source, target = self.copies()
        with temporary:
            with (source / "README.md").open("a", encoding="utf-8") as stream:
                stream.write("\nfuture implementation note\n")
            self.commit(source, "README.md")
            result = self.run_sync(source, target)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("remains unchanged", result.stdout)

    def test_non_ancestor_producer_commit_is_rejected(self) -> None:
        temporary, source, target = self.copies()
        with temporary:
            tree = self.git(source, "rev-parse", "HEAD^{tree}").stdout.strip()
            environment = os.environ.copy()
            environment.update(
                {
                    "GIT_AUTHOR_NAME": "contract-sync-test",
                    "GIT_AUTHOR_EMAIL": "contract-sync-test@example.invalid",
                    "GIT_COMMITTER_NAME": "contract-sync-test",
                    "GIT_COMMITTER_EMAIL": "contract-sync-test@example.invalid",
                }
            )
            commit = subprocess.run(
                ["git", "-C", str(source), "commit-tree", tree, "-m", "test: diverge producer"],
                check=True,
                capture_output=True,
                text=True,
                env=environment,
            ).stdout.strip()
            self.git(source, "update-ref", "refs/heads/main", commit)
            result = self.run_sync(source, target)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not descend", result.stderr)

    def test_historical_contract_manifest_change_is_rejected_after_restore(self) -> None:
        temporary, source, target = self.copies()
        with temporary:
            bundle = source / HOST_BUNDLE
            contract = bundle / "contract.md"
            canonical_contract = contract.read_bytes()
            canonical_manifest = (bundle / "SHA256SUMS").read_bytes()
            with contract.open("a", encoding="utf-8") as stream:
                stream.write("\ncontract change\n")
            self.rewrite_manifest(bundle)
            self.commit(source, f"{HOST_BUNDLE}/contract.md", f"{HOST_BUNDLE}/SHA256SUMS")

            ancestor = self.git(source, "rev-parse", "HEAD").stdout.strip()
            provenance_path = target / HOST_BUNDLE / "SOURCE.json"
            provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
            released_digest = provenance["bundleDigest"]
            provenance["sourceCommit"] = ancestor
            provenance_path.write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")

            contract.write_bytes(canonical_contract)
            (bundle / "SHA256SUMS").write_bytes(canonical_manifest)
            self.commit(source, f"{HOST_BUNDLE}/contract.md", f"{HOST_BUNDLE}/SHA256SUMS")

            self.assertEqual((bundle / "SHA256SUMS").read_bytes(), canonical_manifest)
            self.assertEqual(
                json.loads(provenance_path.read_text(encoding="utf-8"))["bundleDigest"],
                released_digest,
            )
            result = self.run_sync(source, target)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checksum manifest changed after released provenance", result.stderr)

    def test_dirty_current_contract_bundle_is_rejected(self) -> None:
        temporary, source, target = self.copies()
        with temporary:
            contract = source / HOST_BUNDLE / "contract.md"
            with contract.open("a", encoding="utf-8") as stream:
                stream.write("\nuncommitted contract change\n")
            result = self.run_sync(source, target)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checksum coverage mismatch", result.stderr)


if __name__ == "__main__":
    unittest.main()
