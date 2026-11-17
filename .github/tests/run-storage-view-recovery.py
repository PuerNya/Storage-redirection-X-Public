"""在隔离模拟器上构造系统视图覆盖；观察普通 daemon 恢复，不发送重挂或重启命令。"""

import argparse
import base64
import json
import re
import shlex
import subprocess
import time
import uuid
from pathlib import Path

APP = "me.fakerqu.test.storageredirect"
MODULE = "/data/adb/modules/storage.redirect.x"
CONFIG = f"{MODULE}/config/apps/{APP}.json"
GLOBAL = f"{MODULE}/config/global.json"
LOCK = "/data/local/tmp/srx-test-flow.lock"


def fields(text: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def mounts(text: str, target: str) -> list[dict]:
    result = []
    for line in text.splitlines():
        before, separator, after = line.partition(" - ")
        left, right = before.split(), after.split()
        if separator and len(left) >= 5 and len(right) >= 2 and left[4] == target:
            result.append(dict(id=int(left[0]), parent=int(left[1]), dev=left[2], root=left[3], fs=right[0], source=right[1]))
    return result


def top(text: str, target: str) -> dict | None:
    layers = mounts(text, target)
    covered = {layer["parent"] for layer in layers}
    return next((layer for layer in reversed(layers) if layer["id"] not in covered), None)


def stable(before: dict, after: dict) -> bool:
    host_before, host_after = fields(before["host"]), fields(after["host"])
    return all(before[key] == after[key] for key in ("pid", "start", "namespace", "uid", "config", "global")) and all(host_before.get(key) == host_after.get(key) for key in ("pid", "start_ticks", "dev"))


def validate_isolation(before: dict, after: dict) -> None:
    if not view_restored(before, "/storage/emulated", fields(before["host"])["dev"]):
        raise AssertionError("isolation control did not start with a healthy SRX view")
    if not stable(before, after) or any(before[key] != after[key] for key in ("mountinfo", "ledger")):
        raise AssertionError("another user's process, storage view or ledger changed")


def view_restored(snapshot: dict, target: str, dev: str) -> bool:
    live = top(snapshot["mountinfo"], target)
    return bool(
        live and live["dev"] == dev and len(mounts(snapshot["mountinfo"], target)) == 1
        and f"mount={live['id']}\t{dev}\t/dev/fuse\t{target}" in snapshot.get("ledger", "").splitlines()
    )


def validate_app_result(raw: str, case: str) -> None:
    lines = raw.splitlines()
    messages = [line for line in lines if not line.startswith("__SRT_RESULT_")]
    if len(messages) != 1 or not messages[0].startswith(f"PASS [{case}] "):
        raise AssertionError("app result was not one successful operation")
    if len(lines) != 3 or lines.count("__SRT_RESULT_END__=1") != 1 or len([line for line in lines if re.fullmatch(r"__SRT_RESULT_TOKEN__=[0-9a-f]{32}", line)]) != 1:
        raise AssertionError("app result was incomplete or ambiguous")


def storage_parents(user: int) -> list[str]:
    return ["/storage/emulated"] + [f"/mnt/{view}/{user}/emulated" for view in ("user", "installer", "androidwritable")]


def visible_layer(snapshot: dict, target: str) -> dict | None:
    identifier = snapshot["visible"].get(target)
    for line in snapshot["mountinfo"].splitlines():
        left = line.partition(" - ")[0].split()
        if len(left) >= 5 and str(identifier) == left[0]:
            return {**mounts(line, left[4])[0], "target": left[4]}
    return None


def validate_namespace(before: dict, fault: dict, after: dict, user: int, mode: str) -> None:
    if not stable(before, fault) or not stable(before, after) or before["ledger"] != fault["ledger"]:
        raise AssertionError("namespace recovery identity or original ledger changed")
    anchor = top(fault["init_mountinfo"], f"/mnt/user/{user}/emulated")
    if not anchor or (anchor["fs"], anchor["source"], anchor["root"]) != ("fuse", "/dev/fuse", "/"):
        raise AssertionError("trusted system anchor missing")
    for parent in storage_parents(user):
        target = f"{parent}/{user}"
        original = visible_layer(before, target)
        final = visible_layer(after, target)
        if not original or original["target"] != target or original["dev"] == anchor["dev"] or not original["root"].endswith(f"/{user}/Android/data/{APP}/sdcard"):
            raise AssertionError("namespace bind control missing: " + target)
        host_dev = fields(before["host"])["dev"]
        for snapshot, layer in ((before, original), (after, final)):
            if mode == "foreign" and snapshot is after:
                continue
            bind = layer and layer["target"] == target and (layer["dev"], layer["root"]) == (original["dev"], original["root"])
            host = layer and (layer["target"], layer["dev"], layer["root"], layer["fs"], layer["source"]) == (parent, host_dev, "/", "fuse", "/dev/fuse")
            if not layer or not (bind or host) or f"mount={layer['id']}\t{layer['dev']}\t{layer['source']}\t{layer['target']}" not in snapshot["ledger"].splitlines():
                raise AssertionError("namespace view and ledger disagree: " + target)
        changed = visible_layer(fault, parent if mode == "foreign" else target)
        if mode == "foreign":
            if not changed or (changed["fs"], changed["source"]) != ("tmpfs", "issue18-foreign") or visible_layer(after, parent) != changed:
                raise AssertionError("foreign ancestor was missing or altered")
        elif not changed or changed["dev"] != anchor["dev"] or changed["target"] != parent:
            raise AssertionError("system ancestor is not the actual visible view")
        if mode == "cover" and original["id"] not in {layer["id"] for layer in mounts(fault["mountinfo"], target)}:
            raise AssertionError("covered child record did not remain in mountinfo")
        if mode == "missing" and original["id"] in {layer["id"] for layer in mounts(fault["mountinfo"], target)}:
            raise AssertionError("missing child was not detached")
    if mode == "foreign" and (after["mountinfo"] != fault["mountinfo"] or after["ledger"] != fault["ledger"]):
        raise AssertionError("foreign ancestor observation changed the namespace")


def validate_io(record: dict, metadata: str | None = None) -> None:
    validate_app_result(record["raw"], "file_write")
    if record["private"] != record["payload"] or record["real"] != "__absent__" or not record["metadata"]:
        raise AssertionError("app result disagrees with backend evidence")
    if metadata is not None and record["metadata"] != metadata:
        raise AssertionError("recovered file metadata differs from the healthy control")


def validate(before: dict, fault: dict, after: dict, targets: list[str], foreign: bool, missing: bool = False) -> None:
    if not targets:
        raise AssertionError("no storage targets were tested")
    if not stable(before, fault) or not stable(before, after):
        raise AssertionError("process, namespace, policy or host changed during observation")
    dev = fields(before["host"])["dev"]
    user = int(before["uid"]) // 100000
    system = top(fault["init_mountinfo"], f"/mnt/user/{user}/emulated")
    if not system or system["dev"] == dev or system["fs"] != "fuse" or system["source"] != "/dev/fuse" or system["root"] != "/":
        raise AssertionError("trusted system storage anchor missing")
    if fault.get("ledger") != before.get("ledger"):
        raise AssertionError("ledger changed before the fault was captured")
    for target in targets:
        original, changed, final = (top(s["mountinfo"], target) for s in (before, fault, after))
        if not original or original["dev"] != dev or (not changed and not missing) or changed and changed["dev"] == dev:
            raise AssertionError(f"fault was not constructed: {target}")
        if foreign:
            if not changed or changed["fs"] != "tmpfs" or changed["source"] != "issue18-foreign":
                raise AssertionError(f"foreign fault identity missing: {target}")
        elif changed and (changed["dev"], changed["fs"], changed["source"], changed["root"]) != (system["dev"], "fuse", "/dev/fuse", "/"):
            raise AssertionError(f"fault is not the system storage view: {target}")
        if foreign:
            if final != changed:
                raise AssertionError(f"unknown mount was altered: {target}")
        elif not final or final["dev"] != dev or len(mounts(after["mountinfo"], target)) != 1:
            raise AssertionError(f"view was not restored to one SRX layer: {target}")
        elif "ledger" in after and f"mount={final['id']}\t{dev}\t/dev/fuse\t{target}" not in after["ledger"].splitlines():
            raise AssertionError(f"recovery ledger does not match the live view: {target}")


class Device:
    def __init__(self, serial: str, output: Path):
        self.serial, self.output = serial, output
        self.remote = "/data/local/tmp/srx-issue18-" + uuid.uuid4().hex
        self.owned_files = []

    def adb(self, *args: str) -> str:
        run = subprocess.run(["adb", "-s", self.serial, *args], capture_output=True, text=True, encoding="utf8", errors="replace", timeout=45)
        if run.returncode:
            raise RuntimeError(run.stdout + run.stderr)
        return run.stdout.replace("\r", "")

    def su(self, command: str) -> str:
        return self.adb("shell", "su -c " + shlex.quote(command))

    def read(self, path: str) -> str:
        return self.su(f"if test -f {shlex.quote(path)}; then cat {shlex.quote(path)}; else printf __absent__; fi")

    def write(self, path: str, content: str) -> None:
        if content == "__absent__":
            self.su("rm -f " + shlex.quote(path))
            return
        encoded = base64.b64encode(content.encode()).decode()
        self.su(f"printf %s {encoded} | base64 -d > {shlex.quote(path)}.issue18.tmp && chmod 644 {shlex.quote(path)}.issue18.tmp && mv {shlex.quote(path)}.issue18.tmp {shlex.quote(path)}")

    def snapshot(self, pid: str, name: str) -> dict:
        stat = self.su(f"cat /proc/{pid}/stat")
        status = self.su(f"cat /proc/{pid}/status")
        result = dict(
            pid=pid, start=stat[stat.rindex(")") + 2 :].split()[19],
            namespace=self.su(f"readlink /proc/{pid}/ns/mnt").strip(),
            uid=re.search(r"^Uid:\s+(\d+)", status, re.M)[1],
            mountinfo=self.su(f"cat /proc/{pid}/mountinfo"),
            init_mountinfo=self.su("cat /proc/1/mountinfo"),
            config=self.read(CONFIG), global_=self.read(GLOBAL),
            host=self.read(f"{MODULE}/tmp/fuse_host.snapshot"),
            ledger=self.read(f"{MODULE}/tmp/mount_state/identity/{APP}_{pid}.identity"),
            monotonic=time.monotonic(),
        )
        result["global"] = result.pop("global_")
        self.output.joinpath(name + ".json").write_text(json.dumps(result, indent=2), encoding="utf8")
        return result

    def namespace_snapshot(self, pid: str, name: str, user: int) -> dict:
        result = self.snapshot(pid, name)
        targets = [path for parent in storage_parents(user) for path in (parent, f"{parent}/{user}")]
        # 用 shell 内建读取自身 fdinfo，避免 exec 外部命令后关闭探测描述符。
        command = f'''for target in {shlex.join(targets)}; do
id=$( (exec 9<"/proc/{pid}/root$target" && while read -r key value; do
if [ "$key" = mnt_id: ]; then printf '%s\\n' "$value"; fi
done </proc/self/fdinfo/9) 2>/dev/null) || id=missing
printf '%s=%s\\n' "$target" "$id"
done'''
        raw = self.su(command)
        self.output.joinpath(name + "-visible.txt").write_text(raw, encoding="utf8")
        result["visible"] = {path: int(value) if value.isdigit() else None for path, value in fields(raw).items()}
        self.output.joinpath(name + ".json").write_text(json.dumps(result, indent=2), encoding="utf8")
        return result

    def app_case(self, user: int, name: str, case: str, **extras: str) -> str:
        token = uuid.uuid4().hex
        args = ["am", "start-foreground-service", "--user", str(user), "-n", f"{APP}/.TestService", "-a", "me.fakerqu.test.storageredirection.TEST_CASE"]
        for key, value in dict(test_case=case, result_token=token, **extras).items():
            args += ["--es", key, value]
        launch = self.adb("shell", shlex.join(args))
        self.output.joinpath(name + "-launch.txt").write_text(launch, encoding="utf8")
        result = f"/data/user/{user}/{APP}/files/test_case_result/result_current.txt"
        deadline = time.monotonic() + 45
        text = ""
        while time.monotonic() < deadline:
            text = self.read(result)
            if f"__SRT_RESULT_TOKEN__={token}\n" in text:
                break
            time.sleep(0.2)
        else:
            raise AssertionError("test app result missing or stale")
        self.output.joinpath(name + "-result.txt").write_text(text, encoding="utf8")
        return text

    def app_write(self, user: int, name: str, payload: str, path_name: str | None = None) -> dict:
        file_name = path_name or name
        path = f"/storage/emulated/{user}/Download/issue18-{file_name}.txt"
        private = f"/data/media/{user}/Android/data/{APP}/sdcard/Download/issue18-{file_name}.txt"
        real = f"/data/media/{user}/Download/issue18-{file_name}.txt"
        self.owned_files.extend((private, real))
        text = self.app_case(user, name, "file_write", file_path=path, payload=payload, expected_payload=payload)
        metadata = self.su(f"if test -f {shlex.quote(private)}; then stat -c '%u:%g:%a' {shlex.quote(private)}; fi").strip()
        record = dict(raw=text, payload=payload, private=self.read(private), real=self.read(real), metadata=metadata)
        self.output.joinpath(name + "-io.json").write_text(json.dumps(record, indent=2), encoding="utf8")
        return record

    def start(self, user: int, namespace_bind: bool = False) -> str:
        self.adb("shell", f"am force-stop --user {user} {APP}")
        # 后台用户的 Activity 未必启动进程；服务由 shell 显式指定用户发起。
        # file_write 不创建父目录；新用户的沙箱目录必须先由应用自身创建。
        raw = self.app_case(user, f"setup-{user}-mkdir", "file_mkdir", file_dir=f"/storage/emulated/{user}/Download")
        validate_app_result(raw, "file_mkdir")
        validate_io(self.app_write(user, "setup-" + uuid.uuid4().hex[:8], "issue18-setup"))
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            for pid in self.adb("shell", f"pidof {APP} || true").split():
                status = self.su(f"cat /proc/{pid}/status")
                uid = int(re.search(r"^Uid:\s+(\d+)", status, re.M)[1])
                if uid // 100000 == user:
                    state = self.snapshot(pid, "setup")
                    host = fields(state["host"])
                    current = top(state["mountinfo"], f"/storage/emulated/{user}" if namespace_bind else "/storage/emulated")
                    ready = current and (current["root"].endswith(f"/{user}/Android/data/{APP}/sdcard") if namespace_bind else current["dev"] == host.get("dev"))
                    if state["ledger"] != "__absent__" and ready:
                        return pid
            time.sleep(1)
        raise AssertionError("SRX view and ledger did not become ready")

    def cleanup(self, user: int, original: dict[str, str]) -> None:
        actions = [("stop", lambda: self.adb("shell", f"am force-stop --user {user} {APP}"))]
        actions += [(path, lambda path=path, content=content: self.write(path, content)) for path, content in original.items()]
        if self.owned_files:
            actions.append(("files", lambda: self.su("rm -f " + shlex.join(self.owned_files))))
        actions.append(("fixture", lambda: self.su("rm -rf " + self.remote)))
        actions.append(("lock", lambda: self.su(f'test "$(cat {LOCK}/owner)" = {self.remote.rsplit("/", 1)[1]} && rm {LOCK}/owner && rmdir {LOCK}')))
        failures = []
        for name, action in actions:
            try:
                action()
            except Exception as error:
                failures.append(f"{name}: {error}")
        if failures:
            raise RuntimeError("cleanup incomplete: " + "; ".join(failures))


def run_namespace_matrix(device: Device, user: int, timeout: int, reports: list[dict]) -> None:
    device.write(CONFIG, json.dumps({"users": {str(user): {"enabled": True}}}))
    pid = None
    for index, mode in enumerate(("cover", "missing", "cover", "foreign")):
        name = f"namespace-{index}-{mode}"
        try:
            if pid is None:
                pid = device.start(user, namespace_bind=True)
            before = device.namespace_snapshot(pid, name + "-before", user)
            control = device.app_write(user, name + "-control", "issue18-control")
            validate_io(control)
            for parent in storage_parents(user):
                target = f"{parent}/{user}" if mode == "missing" else parent
                layer = visible_layer(before, target)
                if not layer:
                    raise AssertionError("fault baseline missing: " + target)
                device.su(shlex.join([device.remote + "/fixture", pid, before["uid"], str(user), target, mode, layer["dev"]]))
            fault = device.namespace_snapshot(pid, name + "-fault", user)
            deadline = time.monotonic() + timeout
            observations = 0
            while True:
                after = device.namespace_snapshot(pid, f"{name}-observed-{observations}", user)
                observations += 1
                try:
                    validate_namespace(before, fault, after, user, mode)
                    restored = True
                except AssertionError:
                    restored = False
                if not stable(before, after):
                    raise AssertionError("process, policy or host changed during observation")
                if mode == "foreign" and (after["mountinfo"] != fault["mountinfo"] or after["ledger"] != fault["ledger"]):
                    raise AssertionError("foreign namespace changed during observation")
                if restored and mode != "foreign" or time.monotonic() >= deadline:
                    break
                if observations % 6 == 1:
                    print(f"WAIT {name}: ordinary daemon reconciliation", flush=True)
                time.sleep(10)
            validate_namespace(before, fault, after, user, mode)
            if mode != "foreign":
                validate_io(device.app_write(user, name + "-recovered", "issue18-recovered"))
                path = f"/storage/emulated/{user}/Download/issue18-{name}-control.txt"
                validate_app_result(device.app_case(user, name + "-existing-read", "file_read", file_path=path, expected_payload=control["payload"]), "file_read")
                # 新文件的创建后端可变；既有文件跨恢复读写后必须保留原始元数据。
                validate_io(device.app_write(user, name + "-existing-write", "issue18-existing", path_name=name + "-control"), control["metadata"])
            reports.append(dict(case=name, passed=True, mode=mode, observations=observations))
            print("PASS " + name, flush=True)
        except Exception as error:
            reports.append(dict(case=name, passed=False, error=str(error)))
            print(f"FAIL {name}: {error}", flush=True)
        finally:
            # 每轮从自然产生的 bind 回退开始；故障到恢复期间不重启应用。
            device.adb("shell", f"am force-stop --user {user} {APP}")
            pid = None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--user", type=int, default=0)
    parser.add_argument("--isolation-user", type=int)
    parser.add_argument("--timeout", type=int, default=360)
    parser.add_argument("--namespace-bind", action="store_true", help="验证无附加规则的 namespace 回退与父入口覆盖")
    args = parser.parse_args()
    if args.user < 0 or args.timeout < 300:
        parser.error("user must be nonnegative; timeout must cover the 300-second reconcile interval")
    if args.isolation_user is not None and (args.isolation_user < 0 or args.isolation_user == args.user):
        parser.error("isolation user must be a different nonnegative user")
    if args.namespace_bind and args.isolation_user is not None:
        parser.error("namespace matrix does not change another user's policy")
    args.output.mkdir(parents=True, exist_ok=True)
    device = Device(args.serial, args.output)
    if device.adb("shell", "getprop ro.kernel.qemu").strip() != "1":
        raise RuntimeError("namespace fault suite requires an emulator")
    device.su(f"mkdir {LOCK} && printf %s {device.remote.rsplit('/', 1)[1]} > {LOCK}/owner")
    original = {}
    reports = []
    try:
        original = {path: device.read(path) for path in (CONFIG, GLOBAL)}
        args.output.joinpath("original-config.json").write_text(json.dumps(original, indent=2), encoding="utf8")
        device.adb("shell", "mkdir " + device.remote)
        device.adb("push", str(args.fixture), device.remote + "/fixture")
        helper = Path(__file__).with_name("grant-storage-test-permissions.sh")
        device.adb("push", str(helper), device.remote + "/permissions.sh")
        device.su(f"chmod 755 {device.remote}/fixture")
        users = [args.user] + ([] if args.isolation_user is None else [args.isolation_user])
        for user in users:
            device.su(f"sh {device.remote}/permissions.sh {APP} {user}")
        device.write(GLOBAL, json.dumps(dict(file_monitor_enabled=False, fuse_fix_enabled=True, storage_backend_mode="auto", verbose_logging_enabled=True, auto_enable_redirect_for_new_apps=False)))
        if args.namespace_bind:
            run_namespace_matrix(device, args.user, args.timeout, reports)
        else:
            device.write(CONFIG, json.dumps({"users": {str(user): {"enabled": True, "allowed_real_paths": ["Pictures/issue18-control"]} for user in users}}))
        isolation_pid = None
        pid = None
        for index, mode in enumerate(() if args.namespace_bind else ("cover", "replace", "cover", "missing", "foreign")):
            name = f"{index}-{mode}-{uuid.uuid4().hex[:8]}"
            try:
                if args.isolation_user is not None and isolation_pid is None:
                    isolation_pid = device.start(args.isolation_user)
                if pid is None:
                    pid = device.start(args.user)
                before = device.snapshot(pid, name + "-before")
                isolation_before = None
                if isolation_pid is not None:
                    isolation_before = device.snapshot(isolation_pid, name + "-isolation-before")
                    validate_isolation(isolation_before, isolation_before)
                    validate_io(device.app_write(args.isolation_user, name + "-isolation-control", "issue18-other-user"))
                dev = fields(before["host"])["dev"]
                targets = [target for target in storage_parents(args.user) if top(before["mountinfo"], target)]
                control = device.app_write(args.user, name + "-control", "issue18-control")
                validate_io(control)
                for target in targets:
                    command = [device.remote + "/fixture", pid, before["uid"], str(args.user), target, mode, dev]
                    device.su(shlex.join(command))
                fault = device.snapshot(pid, name + "-fault")
                for target in targets:
                    current = top(fault["mountinfo"], target)
                    if (not current and mode != "missing") or current and current["dev"] == dev:
                        raise AssertionError(f"fault not visible: {target}")
                fault_io = device.app_write(args.user, name + "-fault", "issue18-fault")
                # Provider 可能继续保护文件落点；故障和恢复只能由挂载身份判定。
                print(f"FAULT_IO {name}: redirected={fault_io['private'] == fault_io['payload']}", flush=True)
                deadline = time.monotonic() + args.timeout
                observations = 0
                while True:
                    after = device.snapshot(pid, f"{name}-observed-{observations}")
                    observations += 1
                    if not stable(before, after):
                        raise AssertionError("observation identity changed")
                    if isolation_before is not None:
                        isolation_after = device.snapshot(isolation_pid, f"{name}-isolation-observed-{observations}")
                        validate_isolation(isolation_before, isolation_after)
                    if mode == "foreign" and any(top(after["mountinfo"], target) != top(fault["mountinfo"], target) for target in targets):
                        raise AssertionError("foreign takeover was altered during observation")
                    restored = all(view_restored(after, target, dev) for target in targets)
                    if restored and mode != "foreign" or time.monotonic() >= deadline:
                        break
                    if observations % 6 == 1:
                        print(f"WAIT {name}: ordinary daemon reconciliation", flush=True)
                    time.sleep(10)
                validate(before, fault, after, targets, mode == "foreign", mode == "missing")
                if isolation_before is not None:
                    validate_io(device.app_write(args.isolation_user, name + "-isolation-recovered", "issue18-other-user"))
                if mode != "foreign":
                    io = device.app_write(args.user, name + "-recovered", "issue18-recovered")
                    validate_io(io, control["metadata"])
                reports.append(dict(case=name, passed=True, targets=targets, mode=mode, observations=observations))
                print("PASS " + name, flush=True)
            except Exception as error:
                reports.append(dict(case=name, passed=False, error=str(error)))
                print(f"FAIL {name}: {error}", flush=True)
                device.adb("shell", f"am force-stop --user {args.user} {APP}")
                pid = None
                if args.isolation_user is not None:
                    device.adb("shell", f"am force-stop --user {args.isolation_user} {APP}")
                    isolation_pid = None
    except Exception as error:
        reports.append(dict(case="runner", passed=False, error=str(error)))
        raise
    finally:
        cleanup_errors = []
        try:
            if args.isolation_user is not None:
                device.adb("shell", f"am force-stop --user {args.isolation_user} {APP}")
        except Exception as error:
            cleanup_errors.append(str(error))
        try:
            device.cleanup(args.user, original)
        except Exception as error:
            cleanup_errors.append(str(error))
        if cleanup_errors:
            reports.append(dict(case="cleanup", passed=False, error="; ".join(cleanup_errors)))
            print("FAIL cleanup: " + "; ".join(cleanup_errors), flush=True)
        args.output.joinpath("results.json").write_text(json.dumps(reports, indent=2), encoding="utf8")
    if not reports or any(not report["passed"] for report in reports):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
