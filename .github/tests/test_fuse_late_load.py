"""用虚拟时钟和排队器执行生产晚加载重试，验证退避、合并及排队失败。"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_mount_identity_syntax import extract_fn

ROOT = Path(__file__).resolve().parents[2]


class FuseLateLoadTest(unittest.TestCase):
    def test_profiles_and_retry_events(self) -> None:
        rustc = shutil.which("rustc")
        self.assertIsNotNone(rustc)
        read = (ROOT / "src/hook/ops/read.rs").read_text(encoding="utf8")
        native = (ROOT / "src/hook/fuse_fix.rs").read_text(encoding="utf8")
        entries = (ROOT / "src/hook/entries.rs").read_text(encoding="utf8")
        definitions = entries[: entries.index("pub struct HookEntry")]
        state = read[read.index("static PATCH_NEXT_ATTEMPT_MS") : read.index("/// 有库刚被")]
        events = "\n".join(
            extract_fn(read, name)
            for name in ("retry_after_late_load", "apply_late_load_patches", "reset_patch_backoff", "patch_backoff")
        ).replace("std::thread::Builder", "scheduler::Builder").replace("std::thread::sleep", "scheduler::sleep")
        native_events = "pub(crate) " + extract_fn(native, "retry_after_late_load") + "\n" + extract_fn(native, "retry_if_target_enabled")
        template = (ROOT / ".github/tests/harness/fuse_late_load.rs").read_text(encoding="utf8")
        code = template.replace("// __DEFINITIONS__", definitions).replace("// __STATE__", state).replace("// __EVENTS__", events).replace("// __NATIVE_EVENTS__", native_events)
        with tempfile.TemporaryDirectory(prefix="srx-late-load-") as tmp:
            source, binary = Path(tmp) / "main.rs", Path(tmp) / "late-load"
            source.write_text(code, encoding="utf8")
            for cmd in ([rustc, "--edition=2024", str(source), "-o", str(binary)], [str(binary)]):
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_successful_dynamic_load_hooks_trigger_fuse_retry(self) -> None:
        source = (ROOT / "src/hook/ops/dlopen.rs").read_text(encoding="utf8")
        for name, event in (
            ("hooked_dlopen", "dlopen"),
            ("hooked_android_dlopen_ext", "android_dlopen_ext"),
        ):
            with self.subTest(hook=name):
                body = extract_fn(source, name)
                _, found, after_success = body.partition("if !result.is_null()")
                self.assertTrue(found, f"{name} must gate late-load work on success")
                success_block, closed, _ = after_success.partition("}")
                self.assertTrue(closed, f"{name} success block is incomplete")
                refresh = f'hub.refresh_hooks_after_late_load("{event}")'
                retry = "retry_fuse_engine_patch_after_late_load(hub)"
                self.assertIn(refresh, success_block)
                self.assertIn(retry, success_block)
                self.assertLess(success_block.index(refresh), success_block.index(retry))

        retry = extract_fn(source, "retry_fuse_engine_patch_after_late_load")
        self.assertIn("is_media_provider_package", retry)
        self.assertIn("super::read::retry_after_late_load()", retry)


if __name__ == "__main__":
    unittest.main()
