"""Checkpoint writes survive the Windows file locks seen during scans."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import marketplace_atomic


class AtomicJsonTests(unittest.TestCase):
    def test_retries_temporary_target_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "conditioned.json"
            target.write_text('{"old": true}', encoding="utf-8")
            real_replace = Path.replace
            calls = []

            def briefly_locked(source, destination):
                calls.append(source)
                if len(calls) < 3:
                    self.assertEqual(json.loads(target.read_text(encoding="utf-8")),
                                     {"old": True})
                    raise PermissionError("target temporarily open")
                return real_replace(source, destination)

            with (patch.object(Path, "replace", briefly_locked),
                  patch.object(marketplace_atomic.time, "sleep")):
                marketplace_atomic.write_json(target, {"complete": True})

            self.assertEqual(json.loads(target.read_text(encoding="utf-8")),
                             {"complete": True})
            self.assertEqual(len(calls), 3)
            self.assertEqual(list(Path(folder).glob("*.tmp")), [])

    def test_does_not_replace_old_checkpoint_after_retries_exhausted(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "conditioned.json"
            target.write_text('{"old": true}', encoding="utf-8")
            with (patch.object(Path, "replace", side_effect=PermissionError("locked")),
                  patch.object(marketplace_atomic.time, "sleep")):
                with self.assertRaises(PermissionError):
                    marketplace_atomic.write_json(target, {"complete": True})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")),
                             {"old": True})
            self.assertEqual(list(Path(folder).glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
