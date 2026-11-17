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


if __name__ == "__main__":
    unittest.main()
