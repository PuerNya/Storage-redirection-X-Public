from pathlib import Path
import runpy
import shutil
import subprocess
import unittest


HELPER = Path(__file__).with_name("media-provider-pid.sh")


class CollectionDelegationTest(unittest.TestCase):
    def test_requires_serving_process_matching_caller_thread_and_request_order(self):
        delegated = runpy.run_path(str(HELPER.with_name("verify-collection-delegation.py")))["delegated"]
        result = "PASS [mediastore_open_typed_collection] delegated caller_uid=10192 caller_pid=3201"
        prefix = "11-19 12:01:00.000  9049  108 I SRX     : "
        request = prefix + "java open target=MediaProvider#openTypedAssetFile(Uri) caller_uid=10192 caller_pid=3201 redirect_enabled=true args=[uri=content://media/external/file, ...]"
        marker = prefix + "java open delegate reason=collection_uri_passthrough from=content://media/external/file to= n=3"
        self.assertTrue(delegated(result, request + "\n" + marker, 9049))
        for lines in ((marker,), (request,), (marker, request), (request, marker.replace("108 I", "109 I")),
                      (request.replace("3201", "3202"), marker), (request.replace("10192", "1010192"), marker),
                      (request.replace("true", "false"), marker), (request, request.replace("3201", "3202"), marker),
                      (request, marker.replace("9049", "7800")), (request, marker.replace("/file to=", "/file/1 to="))):
            with self.subTest(lines=lines):
                self.assertFalse(delegated(result, "\n".join(lines), 9049))
        self.assertFalse(delegated(result, request + "\n" + marker, 7800))
        self.assertFalse(delegated("", request + "\n" + marker, 9049))


def provider(user, pid, authority="media"):
    proc = f"ProcessRecord{{a1 {pid}:org.example.provider/u{user}a179}}" if pid else "null"
    return (f"  * ContentProviderRecord{{a2 u{user} org.example.provider/.Provider}}\n"
            f"    proc={proc}\n    uid={user * 100000 + 10179} provider=proxy\n"
            f"    authority={authority}\n")


@unittest.skipUnless(shutil.which("sh") and shutil.which("awk"), "sh and awk required")
class MediaProviderPidTest(unittest.TestCase):
    def select(self, records):
        command = 'dumpsys() { test "$*" = "activity providers" && cat; }; . "$1"'
        return subprocess.run(["sh", "-c", command, "sh", str(HELPER)], input=records,
                              text=True, capture_output=True, check=True).stdout

    def test_published_owner_independent_of_process_order_and_name(self):
        owner = provider(0, 9049, "downloads;media")
        other = provider(10, 7800) + provider(0, 590, "media.documents")
        for records in (owner + other, other + owner):
            with self.subTest(records=records):
                self.assertEqual(self.select(records), "9049\n")

    def test_missing_stopped_malformed_or_ambiguous_owner(self):
        for records in ("", provider(10, 7800), provider(0, None), provider(0, "invalid"),
                        provider(0, 590, "media.documents") + provider(0, None),
                        provider(0, 9049) + provider(0, 9050)):
            with self.subTest(records=records):
                self.assertEqual(self.select(records), "")
