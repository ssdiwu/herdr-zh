"""在临时应用目录验证安装与恢复，不碰真实会话。"""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import manage


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve()
        self.payload = {'mods/demo/main.js': b'example', 'layouts/demo.json': b'{}'}
        self.fake = patch.object(manage, 'payload', return_value=self.payload)
        self.fake.start()

    def tearDown(self):
        self.fake.stop()
        self.tmp.cleanup()

    def install(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return manage.apply_plan(self.home, manage.install_plan(self.home))

    def test_install_repeat_and_rollback(self):
        original = b'{"global":{"cmd-x":"terminal.copy"},"unrelated":true}'
        (self.home / 'keys.json').write_bytes(original)
        receipt = self.install()
        self.assertEqual((self.home / 'mods/demo/main.js').read_bytes(), b'example')
        self.assertTrue(json.loads((self.home / 'keys.json').read_bytes())['unrelated'])
        self.assertEqual(manage.install_plan(self.home), {})
        manage.rollback(self.home, receipt)
        self.assertFalse((self.home / 'mods/demo/main.js').exists())
        self.assertEqual((self.home / 'keys.json').read_bytes(), original)

    def test_conflicting_shortcut_does_not_write(self):
        (self.home / 'keys.json').write_text('{"global":{"cmd-p":"my-action"}}')
        with self.assertRaisesRegex(RuntimeError, '拒绝覆盖'):
            manage.install_plan(self.home)
        self.assertFalse((self.home / 'mods').exists())

    def test_unmanaged_module_not_overwritten(self):
        f = self.home / 'mods/demo/main.js'
        f.parent.mkdir(parents=True)
        f.write_text('user changes')
        with self.assertRaisesRegex(RuntimeError, '拒绝覆盖'):
            manage.install_plan(self.home)
        self.assertEqual(f.read_text(), 'user changes')

    def test_rollback_refuses_later_changes_before_any_write(self):
        receipt = self.install()
        path = self.home / 'keys.json'
        path.write_text('{"later":true}')
        with self.assertRaisesRegex(RuntimeError, '安装后已有其他修改'):
            manage.rollback(self.home, receipt)
        self.assertEqual((self.home / 'mods/demo/main.js').read_bytes(), b'example')
        self.assertEqual(path.read_text(), '{"later":true}')

    def test_current_install_fingerprint_protects_local_edit(self):
        self.install()
        f = self.home / 'mods/demo/main.js'
        f.write_text('after install')
        with self.assertRaisesRegex(RuntimeError, '拒绝覆盖'):
            manage.install_plan(self.home)

    def test_frame_mismatch_blocks_whole_plan(self):
        for rel in manage.rules():
            f = self.home / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text('new incompatible upstream')
        with self.assertRaisesRegex(RuntimeError, '失配规则'):
            manage.install_plan(self.home, frames=True)
        self.assertFalse((self.home / 'mods').exists())

    def test_translation_handles_dynamic_value_without_changing_it(self):
        rules = [{'from': '`Default (${def})`', 'to': '`默认（${def}）`'}]
        original = 'const label = `Default (${def})`;'
        translated = manage.translate(original, rules, 'test')
        self.assertEqual(translated, 'const label = `默认（${def}）`;')
        self.assertEqual(manage.translate(translated, rules, 'test'), translated)

    def test_traversal_and_symlink_are_rejected(self):
        with self.assertRaises(ValueError):
            manage.target(self.home, '../outside')
        (self.home / 'mods').symlink_to(self.home / 'other', target_is_directory=True)
        with self.assertRaises(ValueError):
            manage.target(self.home, 'mods/demo/main.js')

    def test_atomic_write_detects_concurrent_change(self):
        f = self.home / 'settings.json'
        f.write_bytes(b'later')
        with self.assertRaisesRegex(RuntimeError, '检查后改变'):
            manage.atomic(f, b'new', b'earlier')
        self.assertEqual(f.read_bytes(), b'later')

    def test_state_symlink_is_rejected_before_payload_writes(self):
        folder = self.home / '.herdr-zh'
        folder.mkdir()
        outside = self.home / 'outside.json'
        outside.write_text('{}')
        (folder / 'installed.json').symlink_to(outside)
        with self.assertRaises(ValueError):
            manage.install_plan(self.home)
        with self.assertRaises(ValueError):
            manage.apply_plan(self.home, {'mods/demo/main.js': (None, b'new')})
        self.assertFalse((self.home / 'mods').exists())
        self.assertEqual(outside.read_text(), '{}')

    def test_backup_symlink_is_rejected_before_payload_writes(self):
        (self.home / '.herdr-zh').mkdir()
        outside = self.home / 'outside'
        outside.mkdir()
        (self.home / '.herdr-zh/backups').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.install()
        self.assertFalse((self.home / 'mods').exists())
        self.assertEqual(list(outside.iterdir()), [])

    def test_receipt_survives_missing_last_progress_update(self):
        receipt = self.install()
        record = json.loads(receipt.read_text())
        record['applied'] = []  # 模拟写入成功、进度更新前进程退出
        receipt.write_text(json.dumps(record))
        manage.rollback(self.home, receipt)
        self.assertFalse((self.home / 'mods/demo/main.js').exists())
        self.assertFalse((self.home / 'keys.json').exists())

    def test_operations_cannot_overlap(self):
        with manage.operation_lock(self.home):
            with self.assertRaises(BlockingIOError):
                with manage.operation_lock(self.home):
                    self.fail('第二个操作获得了锁')

    def test_version_mismatch_is_rejected(self):
        from types import SimpleNamespace
        with patch.object(manage.subprocess, 'run', return_value=SimpleNamespace(stdout='herdr-app 0.2.0')):
            with self.assertRaisesRegex(RuntimeError, '仅核验过'):
                manage.require_version()

    def test_watch_retries_rewrite_between_patch_and_next_poll(self):
        rel = 'frame/settings/main.js'
        f = self.home / rel
        f.parent.mkdir(parents=True)
        f.write_text('English')
        edits = {rel: [{'from': 'English', 'to': '中文'}]}
        actual_atomic = manage.atomic
        calls = []
        def competing_write(path, data, expected):
            actual_atomic(path, data, expected)
            calls.append(path)
            if len(calls) == 1:
                path.write_text('English')  # 应用恰好在补丁写入后重写文件
        sleeps = []
        def bounded_poll(_seconds):
            sleeps.append(True)
            if len(sleeps) == 2:
                raise KeyboardInterrupt()
        with patch.object(manage, 'rules', return_value=edits), patch.object(manage, 'require_version'), patch.object(manage, 'atomic', side_effect=competing_write), patch.object(manage.time, 'sleep', side_effect=bounded_poll):
            with self.assertRaises(KeyboardInterrupt):
                manage.watch(self.home)
        self.assertEqual(f.read_text(), '中文')
        self.assertEqual(len(calls), 2)

    def test_corrupted_backup_rejected(self):
        (self.home / 'keys.json').write_text('{}')
        receipt = self.install()
        (receipt.parent / 'before/keys.json').write_text('corrupt')
        with self.assertRaisesRegex(RuntimeError, '备份内容校验失败'):
            manage.rollback(self.home, receipt)
        self.assertTrue((self.home / 'mods/demo/main.js').exists())


if __name__ == '__main__':
    unittest.main()
