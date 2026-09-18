#!/usr/bin/env python3
"""Tests for fileops.py (stdlib only)."""

import errno
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fileops as fops


class FileopsTests(unittest.TestCase):
    def setUp(self):
        if os.geteuid() == 0:
            self.skipTest("chmod semantics differ when running as root")
        self.src_dir = tempfile.mkdtemp()
        self.dst_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.src_dir, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.dst_dir, ignore_errors=True)

    def make_src(self, name="test.bin", content=b"hello world"):
        src = Path(self.src_dir) / name
        src.write_bytes(content)
        return src, content

    # T1 - same-dir rename returns MOVED
    def test_same_dir_rename(self):
        src, content = self.make_src()
        dst = Path(self.src_dir) / "moved.bin"
        result_path, outcome = fops.relocate(src, dst)
        self.assertEqual(outcome, fops.MOVED)
        self.assertEqual(result_path, dst)
        self.assertFalse(src.exists())
        self.assertTrue(dst.exists())
        self.assertEqual(dst.read_bytes(), content)

    # T2 - EXDEV writable -> MOVED, mtime preserved
    def test_exdev_writable_move(self):
        src, content = self.make_src()
        mtime_before = src.stat().st_mtime
        dst = Path(self.dst_dir) / "moved.bin"

        def _raise_exdev(*args, **kwargs):
            raise OSError(errno.EXDEV, "cross-device link")

        with patch.object(fops.os, "rename", side_effect=_raise_exdev):
            result_path, outcome = fops.relocate(src, dst)

        self.assertEqual(outcome, fops.MOVED)
        self.assertFalse(src.exists())
        self.assertTrue(dst.exists())
        self.assertEqual(dst.read_bytes(), content)
        # copy2 preserves mtime; os.replace preserves timestamps
        self.assertAlmostEqual(dst.stat().st_mtime, mtime_before, delta=0.01)

    # T3 - EXDEV + read-only media -> COPIED_SOURCE_KEPT, src intact, dst identical
    def test_exdev_readonly_media_copies_and_keeps_source(self):
        src_dir = Path(self.src_dir)
        src_dir.chmod(0o755)
        src, content = self.make_src()

        # Make the source directory unwritable/unexecutable so unlink fails.
        src_dir.chmod(0o555)
        self.addCleanup(src_dir.chmod, 0o755)

        dst = Path(self.dst_dir) / "copied.bin"

        def _raise_exdev(*args, **kwargs):
            raise OSError(errno.EXDEV, "cross-device link")

        with patch.object(fops.os, "rename", side_effect=_raise_exdev):
            result_path, outcome = fops.relocate(src, dst)

        self.assertEqual(outcome, fops.COPIED_SOURCE_KEPT)
        self.assertTrue(src.exists())
        self.assertEqual(src.read_bytes(), content)
        self.assertTrue(dst.exists())
        self.assertEqual(dst.read_bytes(), content)
        self.assertFalse(
            any(p.suffix == ".sspart" for p in Path(self.dst_dir).iterdir())
        )

    # T4 - copy raises ENOSPC -> raises, no .sspart left, src intact, no final dst
    def test_copy_enospc_cleans_up(self):
        src, content = self.make_src()
        dst = Path(self.dst_dir) / "nope.bin"

        def _raise_exdev(*args, **kwargs):
            raise OSError(errno.EXDEV, "cross-device link")

        def _raise_enospc(*args, **kwargs):
            raise OSError(errno.ENOSPC, "no space left")

        with patch.object(fops.os, "rename", side_effect=_raise_exdev):
            with patch.object(fops.shutil, "copyfile", side_effect=_raise_enospc):
                with self.assertRaises(OSError) as ctx:
                    fops.relocate(src, dst)

        self.assertEqual(ctx.exception.errno, errno.ENOSPC)
        self.assertTrue(src.exists())
        self.assertFalse(dst.exists())
        self.assertFalse(
            any(p.suffix == ".sspart" for p in Path(self.dst_dir).iterdir())
        )

    # T5 - short copy -> verify raises
    def test_short_copy_raises(self):
        src, content = self.make_src()
        dst = Path(self.dst_dir) / "short.bin"

        def _raise_exdev(*args, **kwargs):
            raise OSError(errno.EXDEV, "cross-device link")

        def _zero_copy(src_path, dst_path, *args, **kwargs):
            Path(dst_path).write_bytes(b"")

        with patch.object(fops.os, "rename", side_effect=_raise_exdev):
            with patch.object(fops.shutil, "copyfile", side_effect=_zero_copy):
                with self.assertRaises(OSError) as ctx:
                    fops.relocate(src, dst)

        self.assertEqual(ctx.exception.errno, errno.EIO)
        self.assertIn("short copy", str(ctx.exception))
        self.assertTrue(src.exists())
        self.assertFalse(dst.exists())

    # T6 - dst collision -> unique_target, pre-existing file untouched
    def test_unique_target_avoids_collision(self):
        src1, content1 = self.make_src("test.bin", b"first")
        src2, content2 = self.make_src("test2.bin", b"second")
        dst_base = Path(self.dst_dir) / "test.bin"
        shutil.copy2(str(src1), str(dst_base))

        result_path1, outcome1 = fops.relocate(src1, dst_base)
        self.assertEqual(outcome1, fops.MOVED)
        self.assertNotEqual(result_path1, dst_base)
        self.assertTrue(result_path1.name.startswith("test_"))
        self.assertTrue(result_path1.exists())
        self.assertEqual(result_path1.read_bytes(), content1)

        # Original destination still holds first content
        self.assertEqual(dst_base.read_bytes(), content1)

    # T7 - unlink FileNotFoundError -> MOVED
    def test_unlink_notfound_returns_moved(self):
        src, content = self.make_src()
        dst = Path(self.dst_dir) / "gone.bin"

        def _raise_exdev(*args, **kwargs):
            raise OSError(errno.EXDEV, "cross-device link")

        original_unlink = os.unlink

        def _raise_notfound(path, *args, **kwargs):
            if str(path) == str(src):
                raise FileNotFoundError(path)
            return original_unlink(path)

        with patch.object(fops.os, "rename", side_effect=_raise_exdev):
            with patch.object(fops.os, "unlink", side_effect=_raise_notfound):
                result_path, outcome = fops.relocate(src, dst)

        self.assertEqual(outcome, fops.MOVED)
        self.assertTrue(dst.exists())
        self.assertEqual(dst.read_bytes(), content)


if __name__ == "__main__":
    unittest.main()