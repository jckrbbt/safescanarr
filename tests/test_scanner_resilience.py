#!/usr/bin/env python3
"""Resilience tests for scanner.py / fileops.py integration."""

import errno
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as config_module
from database import Database
import fileops
import scanner


class ScannerResilienceTests(unittest.TestCase):
    def setUp(self):
        if os.geteuid() == 0:
            self.skipTest("chmod semantics differ when running as root")
        self.base = tempfile.mkdtemp()
        self.watch = Path(self.base) / "watch"
        self.quarantine = Path(self.base) / "quarantine"
        self.vcs = Path(self.base) / "vcs"
        self.watch.mkdir(parents=True, exist_ok=True)
        self.quarantine.mkdir(parents=True, exist_ok=True)
        self.vcs.mkdir(parents=True, exist_ok=True)
        self.addCleanup(
            lambda: shutil.rmtree(self.base, ignore_errors=True)
        )

        # Reset scanner's warn-once flag for every test
        scanner._SOURCE_RETAINED_WARNED = False

    def install_config(self):
        os.environ["BASE_DIR"] = self.base
        cfg = config_module.raw()
        cfg["watch_folders"] = [str(self.watch)]
        cfg["quarantine_dir"] = str(self.quarantine)
        cfg["quarantine_auto_reject_days"] = 0
        cfg["delete_on_reject"] = False
        cfg["zone_quarantine"] = 0.5
        cfg["zone_auto_reject"] = 0.9
        config_module.save(cfg)

    def make_video(self, folder: Path, name: str = "video.mp4", content: Optional[bytes] = None):
        if content is None:
            content = b"MOVIEFILE " + os.urandom(256) + name.encode()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_bytes(content)
        return path

    def make_flagged_result(self, max_conf: float = 0.6, labels=None):
        if labels is None:
            labels = [{"label": "Unsafe", "confidence": max_conf}]
        return {"flagged": True, "labels": labels, "max_conf": max_conf}

    def patch_scanner(self):
        scanner.generate_vcs = lambda *args, **kwargs: True
        scanner.analyse_video_file = lambda *args, **kwargs: self.make_flagged_result()
        scanner._send_webhook = lambda *args, **kwargs: None

    # T8 - cross-device + read-only media: scan completes, flagged file retained
    def test_exdev_readonly_scan_completes_and_marks_source_retained(self):
        self.install_config()
        self.patch_scanner()

        file1 = self.make_video(self.watch / "a", "file1.mp4")
        file2 = self.make_video(self.watch / "b", "file2.mp4")
        file3 = self.make_video(self.watch / "c", "file3.mp4")

        # Middle video lives in a read-only directory; unlink will fail with EACCES.
        file2.parent.chmod(0o555)
        self.addCleanup(file2.parent.chmod, 0o755)

        original_rename = os.rename

        def _rename(src, dst):
            if str(src) == str(file2):
                raise OSError(errno.EXDEV, "cross-device link")
            return original_rename(src, dst)

        db = Database(os.path.join(self.base, "safescanarr.db"))
        with patch.object(fileops.os, "rename", side_effect=_rename):
            scanner.run_scan(db)

        rows = {r["path"]: dict(r) for r in db.list_files()}
        self.assertEqual(len(rows), 3)
        for p in (str(file1), str(file2), str(file3)):
            self.assertIn(p, rows)
            self.assertEqual(rows[p]["review_state"], "quarantined")
            self.assertEqual(rows[p]["flagged"], 1)
            self.assertIsNotNone(rows[p]["quarantine_path"])

        self.assertEqual(rows[str(file2)]["source_retained"], 1)
        self.assertTrue(
            Path(rows[str(file2)]["quarantine_path"]).exists()
        )

        # Each video ends up with exactly one quarantine copy
        q_files = [p for p in self.quarantine.iterdir() if p.is_file()]
        self.assertEqual(len(q_files), 3)

    # T9 - unexpected failure during analysis: scan_pid cleared, error isolated
    def test_analysis_runtime_error_is_isolated(self):
        self.install_config()

        def _gen(*args, **kwargs):
            return True

        scanner.generate_vcs = _gen
        scanner.analyse_video_file = lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("analysis boom")
        )
        scanner._send_webhook = lambda *args, **kwargs: None

        video = self.make_video(self.watch, "boom.mp4")
        db = Database(os.path.join(self.base, "safescanarr.db"))

        with self.assertLogs(scanner.log, level="ERROR") as ctx:
            scanner.run_scan(db)
        self.assertIsNone(db.get_scan_pid())

        rows = {r["path"]: dict(r) for r in db.list_files()}
        self.assertIn(str(video), rows)
        self.assertEqual(rows[str(video)]["status"], "error")
        self.assertEqual(rows[str(video)]["review_state"], "pending")
        self.assertTrue(any("Unexpected error" in m for m in ctx.output))

    # T10 - two undeletable files: operator warning once, per-file lines twice
    def test_undeletable_source_warns_once_and_logs_per_file(self):
        self.install_config()
        self.patch_scanner()

        file1 = self.make_video(self.watch / "ro1", "file1.mp4")
        file2 = self.make_video(self.watch / "ro2", "file2.mp4")
        for folder in (file1.parent, file2.parent):
            folder.chmod(0o555)
            self.addCleanup(folder.chmod, 0o755)

        original_rename = os.rename

        def _rename(src, dst):
            if str(src) in (str(file1), str(file2)):
                raise OSError(errno.EXDEV, "cross-device link")
            return original_rename(src, dst)

        db = Database(os.path.join(self.base, "safescanarr.db"))
        with patch.object(fileops.os, "rename", side_effect=_rename):
            with self.assertLogs(scanner.log, level="WARNING") as ctx:
                scanner.run_scan(db)

        operator_warnings = [
            m for m in ctx.output
            if "Quarantined content remains in the library" in m
        ]
        self.assertEqual(len(operator_warnings), 1)
        per_file_lines = [
            m for m in ctx.output
            if "(COPIED, SOURCE NOT REMOVED)" in m or "(quarantined, source retained)" in m
        ]
        self.assertEqual(len(per_file_lines), 2)

    # T11 - second scan over retained file does not duplicate the quarantine copy
    def test_second_scan_does_not_duplicate_retained_copy(self):
        self.install_config()
        self.patch_scanner()

        video = self.make_video(self.watch / "retained", "file.mp4")
        video.parent.chmod(0o555)
        self.addCleanup(video.parent.chmod, 0o755)

        original_rename = os.rename

        def _rename(src, dst):
            if str(src) == str(video):
                raise OSError(errno.EXDEV, "cross-device link")
            return original_rename(src, dst)

        db = Database(os.path.join(self.base, "safescanarr.db"))
        with patch.object(fileops.os, "rename", side_effect=_rename):
            scanner.run_scan(db)

        rows = {r["path"]: dict(r) for r in db.list_files()}
        first_q = Path(rows[str(video)]["quarantine_path"])
        first_mtime = first_q.stat().st_mtime

        # Run again with no source changes
        scanner.run_scan(db)

        rows = {r["path"]: dict(r) for r in db.list_files()}
        self.assertEqual(Path(rows[str(video)]["quarantine_path"]), first_q)
        self.assertEqual(first_q.stat().st_mtime, first_mtime)
        q_files = [p for p in self.quarantine.iterdir() if p.is_file()]
        self.assertEqual(len(q_files), 1)

    # T12 - source_retained round-trips and is not clobbered by set_review_state
    def test_source_retained_roundtrips(self):
        db = Database(os.path.join(self.base, "test.db"))
        db.upsert_file(
            "/tmp/test.mp4", "test.mp4", 100, 1234.0,
            review_state="quarantined", source_retained=True,
        )
        row = db.get_file("/tmp/test.mp4")
        self.assertEqual(row["source_retained"], 1)
        db.set_review_state("/tmp/test.mp4", "approved")
        row = db.get_file("/tmp/test.mp4")
        self.assertEqual(row["source_retained"], 1)


if __name__ == "__main__":
    unittest.main()
