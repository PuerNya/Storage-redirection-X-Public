// 只替换内核观测输入，身份、用户路径、恢复优先级均执行抽取的生产代码。
#![allow(dead_code, unused_variables)]
use std::sync::Mutex;
use std::ffi::CString;
extern crate self as log;
#[macro_export]
macro_rules! warn { ($($arg:tt)*) => {{}}; }
#[macro_export]
macro_rules! info { ($($arg:tt)*) => {{}}; }
const MAX_UNMOUNT_PASSES_HARD_LIMIT: usize = 8;
const MAX_UNMOUNT_PASSES_PER_TARGET: usize = 4;
const MNT_DETACH: i32 = 2;
mod libc { pub const EINVAL: i32 = 22; pub const ENOENT: i32 = 2; }
mod platform {
    pub use crate::paths;
    pub fn user_id_from_uid(uid: i32) -> i32 { uid / 100000 }
}
mod mount_ledger { pub use crate::history::known_module_fuse_devs; }
mod mount_identity {
    pub use crate::{LiveMount, topmost_live_mount, is_system_storage_view, classify_mount, namespace_identity};
    pub fn load(_: &str, _: i32) -> Option<crate::MountLedger> {
        Some(crate::MountLedger { namespace: crate::NamespaceIdentity { dev: 4, ino: 99 }, target_pid: 40,
            mounts: vec![crate::MountIdentity { mount_point: "/storage/emulated".into(), mount_id: 200, dev: "0:38".into() }] })
    }
    pub fn remove(_: &str, _: i32) -> bool { true }
    pub fn try_visible_mount_at(pid: i32, target: &str) -> std::io::Result<Option<LiveMount>> { try_topmost_live_mount(pid, target) }
    pub fn try_topmost_live_mount(pid: i32, target: &str) -> std::io::Result<Option<LiveMount>> {
        if *crate::OBSERVATION_FAILED.lock().unwrap() { return Err(std::io::Error::from_raw_os_error(13)); }
        Ok(topmost_live_mount(pid, target))
    }
    pub fn system_storage_view_anchor(_: i32) -> Option<LiveMount> { Some(crate::layer(9, "0:95", "/")) }
}
unsafe fn umount2(_: *const std::ffi::c_char, _: i32) -> i32 {
    if *UNMOUNT_ERRNO.lock().unwrap() != 0 { return -1; }
    *LIVE.lock().unwrap() = None;
    0
}
fn last_errno() -> i32 { *UNMOUNT_ERRNO.lock().unwrap() }
fn errno_text(_: i32) -> &'static str { "denied" }
pub mod paths {
    const STORAGE_EMULATED_PREFIX: &str = "/storage/emulated/";
    const DATA_MEDIA_PREFIX: &str = "/data/media/";
    // __PATHS__
}
mod fuse_host {
    pub fn current_host_dev() -> Option<String> { Some("0:38".into()) }
}
mod mountinfo {
    // __MOUNTINFO__
}
static INIT_READ_FAILED: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
mod history {
    use super::known_devs_for_boot;
    use crate::mountinfo;
    mod fs {
        pub fn read_to_string(path: &str) -> Result<String, ()> {
            if path == "/proc/1/mountinfo" && crate::INIT_READ_FAILED.load(std::sync::atomic::Ordering::Relaxed) { return Err(()); }
            Ok(match path {
                "history" => "boot_id=current\n0:37\n0:95\n",
                "/proc/sys/kernel/random/boot_id" => "current\n",
                "/proc/1/mountinfo" => "7 1 0:95 / /mnt/user/0/emulated rw - fuse /dev/fuse rw\n",
                _ => panic!("普通账本不能授权整个超级块：{path}"),
            }.into())
        }
    }
    fn known_devs_path() -> &'static str { "history" }
    // __HISTORY__
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct NamespaceIdentity { dev: u64, ino: u64 }
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LiveMount {
    mount_id: u64, parent_mount_id: u64, dev: String,
    source: String, fs_type: String, root: String,
}
struct MountIdentity { mount_point: String, mount_id: u64, dev: String }
struct MountLedger { namespace: NamespaceIdentity, mounts: Vec<MountIdentity>, target_pid: i32 }
static LIVE: Mutex<Option<LiveMount>> = Mutex::new(None);
static NS: Mutex<Option<NamespaceIdentity>> = Mutex::new(None);
static ALIVE: Mutex<bool> = Mutex::new(true);
static OBSERVATION_FAILED: Mutex<bool> = Mutex::new(false);
static UNMOUNT_ERRNO: Mutex<i32> = Mutex::new(0);
impl MountLedger {
    fn target_is_current(&self) -> bool { *ALIVE.lock().unwrap() }
    fn is_poisoned(&self) -> bool { false }
}
pub fn namespace_identity(_: i32) -> Option<NamespaceIdentity> { *NS.lock().unwrap() }
pub fn topmost_live_mount(_: i32, _: &str) -> Option<LiveMount> { LIVE.lock().unwrap().clone() }
#[derive(Debug, PartialEq, Eq)]
enum MountVerdict { Owned(LiveMount), Detached, Superseded(LiveMount), StaleNamespace }
#[derive(Clone, Copy)]
enum EndpointHealth { Healthy, Missing, DeadConnection(i32), Unprobed(i32) }
impl EndpointHealth {
    fn is_absent(&self) -> bool { matches!(self, Self::Missing) }
    fn is_dead_connection(&self) -> bool { matches!(self, Self::DeadConnection(_)) }
}
struct MountRequest { pid: i32, uid: i32, package_name: String }
mod config {
    pub struct SettingsHub;
    impl SettingsHub {
        pub fn instance() -> Self { Self }
        pub fn has_effective_enabled_redirect_apps_for_user(&self, _: i32) -> bool { true }
    }
}
mod fuse_redirect { pub mod config {
    pub fn mount_request_has_policy_rules(_: &crate::MountRequest) -> bool { true }
} }
mod fuse_supervisor {
    pub use crate::{plan_recovery, aggregate_actions};
    pub static PROBES: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
    pub fn probe_endpoint(_: i32, _: &str) -> crate::EndpointHealth {
        PROBES.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        crate::EndpointHealth::Healthy
    }
    pub fn record_action(_: crate::RecoveryAction, _: crate::EndpointHealth) {}
    pub struct NamespaceSupervision { pub last_action: crate::RecoveryAction }
    impl NamespaceSupervision {
        pub fn from_ledger(_: &crate::MountLedger, _: crate::EndpointHealth, last_action: crate::RecoveryAction) -> Self { Self { last_action } }
    }
}
// __ACTIONS__
// __FUNCTIONS__
fn layer(id: u64, dev: &str, root: &str) -> LiveMount {
    LiveMount { mount_id: id, parent_mount_id: 0, dev: dev.into(), source: "/dev/fuse".into(), fs_type: "fuse".into(), root: root.into() }
}
fn main() {
    let devs = history::known_module_fuse_devs();
    assert_eq!(devs.len(), 2);
    assert!(devs.contains("0:38") && devs.contains("0:37"));
    assert!(!devs.contains("0:95"));
    INIT_READ_FAILED.store(true, std::sync::atomic::Ordering::Relaxed);
    assert_eq!(history::known_module_fuse_devs(), std::collections::HashSet::from(["0:38".into()]));
    INIT_READ_FAILED.store(false, std::sync::atomic::Ordering::Relaxed);
    let namespace = NamespaceIdentity { dev: 4, ino: 99 };
    let target = "/storage/emulated";
    let ledger = MountLedger { namespace, target_pid: 40, mounts: vec![MountIdentity { mount_point: target.into(), mount_id: 3, dev: "0:95".into() }] };
    for (live, allowed, retained) in [
        (layer(3, "0:95", "/"), true, false),
        (layer(2, "0:95", "/"), true, true),
        (layer(1000, "0:37", "/"), true, false),
        (layer(3, "0:999", "/"), false, true),
    ] {
        *LIVE.lock().unwrap() = Some(live);
        assert_eq!(clear_mount_target_stack_verified(target, Some(&ledger), 10100), allowed);
        assert_eq!(LIVE.lock().unwrap().is_some(), retained);
    }
    *LIVE.lock().unwrap() = Some(layer(3, "0:95", "/"));
    *OBSERVATION_FAILED.lock().unwrap() = true;
    assert!(!clear_mount_target_stack_verified(target, Some(&ledger), 10100));
    assert!(LIVE.lock().unwrap().is_some(), "观测失败不能授权摘除");
    *OBSERVATION_FAILED.lock().unwrap() = false;
    for errno in [libc::EINVAL, libc::ENOENT, 13] {
        *UNMOUNT_ERRNO.lock().unwrap() = errno;
        assert!(!clear_mount_target_stack_verified(target, Some(&ledger), 10100));
        assert!(LIVE.lock().unwrap().is_some(), "路径错误不能证明账本层已清空");
    }
    *UNMOUNT_ERRNO.lock().unwrap() = 0;
    assert_eq!(known_devs_for_boot("boot_id=current\n0:38\n", "current\n"), vec!["0:38"]);
    for (content, boot) in [("0:38\n", "current"), ("boot_id=old\n0:38\n", "current"), ("boot_id=\n0:38\n", "")] {
        assert!(known_devs_for_boot(content, boot).is_empty(), "旧开机设备号不得授权清理");
    }
    for user in [0, 11, 13, 999] {
        assert_eq!(storage_alias_relative_path(&format!("/storage/emulated/{user}"), user), Some(String::new()));
        assert_eq!(storage_alias_relative_path(&format!("/storage/emulated/{user}/Download"), user), Some("Download".into()));
        assert_eq!(storage_alias_relative_path(&format!("/storage/emulated/{user}1/Download"), user), None);
        assert_eq!(storage_alias_relative_path(&format!("/storage/emulated/{}/Download", user + 1), user), None);
        let anchor = layer(10, "0:95", "/");
        let namespace = NamespaceIdentity { dev: 4, ino: 99 };
        *NS.lock().unwrap() = Some(namespace);
        *ALIVE.lock().unwrap() = true;
        let targets = [
            format!("/storage/emulated"),
            format!("/mnt/user/{user}/emulated"),
            format!("/mnt/installer/{user}/emulated"),
            format!("/mnt/androidwritable/{user}/emulated"),
            format!("/storage/emulated/{user}/Download"),
        ];
        for target in targets {
            let root = if target.ends_with("/emulated") { "/".into() } else { format!("/{user}/Download") };
            let system = layer(5, "0:95", &root);
            assert!(is_system_storage_view(&target, &system, user, Some(&anchor)), "{user}: {target}");
            assert!(!is_system_storage_view(&target, &system, user, None));
            let mut foreign = system.clone();
            foreign.dev = "0:999".into();
            assert!(!is_system_storage_view(&target, &foreign, user, Some(&anchor)));
            foreign = system.clone();
            foreign.root = format!("/{user}/unrelated");
            assert!(!is_system_storage_view(&target, &foreign, user, Some(&anchor)));
            foreign = system.clone();
            foreign.fs_type = "tmpfs".into();
            assert!(!is_system_storage_view(&target, &foreign, user, Some(&anchor)));
            foreign = system.clone();
            foreign.source = "other".into();
            assert!(!is_system_storage_view(&target, &foreign, user, Some(&anchor)));
            let own = layer(200, "0:38", "/");
            let ledger = MountLedger { target_pid: 40, namespace, mounts: vec![MountIdentity { mount_point: target.clone(), mount_id: own.mount_id, dev: own.dev.clone() }] };
            *LIVE.lock().unwrap() = Some(own.clone());
            assert!(recorded_mounts_current(&ledger));
            *LIVE.lock().unwrap() = Some(system.clone());
            assert!(!recorded_mounts_current(&ledger));
            let covered = classify_mount(&ledger, &target, Some(&system), Some(namespace), true);
            for health in [EndpointHealth::Healthy, EndpointHealth::DeadConnection(107), EndpointHealth::Unprobed(13)] {
                let action = plan_recovery(&covered, health, false, true);
                assert_eq!(action, RecoveryAction::DetachThenReinject);
                assert!(action.allows_inject());
                assert_eq!(plan_recovery(&covered, health, false, false), RecoveryAction::SkipSuperseded);
                assert_eq!(plan_recovery(&covered, health, true, true), RecoveryAction::RefusePoisoned);
            }
            assert_eq!(plan_recovery(&MountVerdict::StaleNamespace, EndpointHealth::Healthy, false, true), RecoveryAction::DropStale);
            *LIVE.lock().unwrap() = None;
            assert!(!recorded_mounts_current(&ledger));
            *LIVE.lock().unwrap() = Some(own);
            *ALIVE.lock().unwrap() = false;
            assert!(!recorded_mounts_current(&ledger));
            *ALIVE.lock().unwrap() = true;
            *NS.lock().unwrap() = None;
            assert!(!recorded_mounts_current(&ledger));
            *NS.lock().unwrap() = Some(namespace);
        }
        let other_user = layer(20, "0:95", &format!("/{}/Download", user + 1));
        assert!(!is_system_storage_view(&format!("/storage/emulated/{user}/Download"), &other_user, user, Some(&anchor)));
        assert!(!is_system_storage_view(&format!("/data/media/{user}"), &anchor, user, Some(&anchor)));
    }
    assert_eq!(aggregate_actions([RecoveryAction::DetachThenReinject, RecoveryAction::SkipSuperseded]), RecoveryAction::SkipSuperseded);
    assert_eq!(aggregate_actions([RecoveryAction::DetachThenReinject, RecoveryAction::RefusePoisoned]), RecoveryAction::RefusePoisoned);
    let request = MountRequest { pid: 40, uid: 10100, package_name: "test".into() };
    for (live, current, expected, probes) in [
        (layer(200, "0:38", "/"), true, RecoveryAction::DetachThenReinject, 1),
        (layer(5, "0:95", "/"), true, RecoveryAction::DetachThenReinject, 0),
        (layer(5, "0:999", "/"), true, RecoveryAction::SkipSuperseded, 0),
        (layer(200, "0:38", "/"), false, RecoveryAction::DropStale, 0),
    ] {
        *LIVE.lock().unwrap() = Some(live);
        *ALIVE.lock().unwrap() = current;
        fuse_supervisor::PROBES.store(0, std::sync::atomic::Ordering::Relaxed);
        assert_eq!(supervise_mount_request(&request).unwrap().last_action, expected);
        assert_eq!(fuse_supervisor::PROBES.load(std::sync::atomic::Ordering::Relaxed), probes, "归属已变的路径不得发起可能阻塞的端点 I/O");
    }
    println!("storage view recovery matrix passed");
}
