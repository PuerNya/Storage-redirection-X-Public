use super::read_passthrough_probe;
use std::fs;
use std::os::unix::fs::{MetadataExt, symlink};

#[test]
fn passthrough_probe_reads_only_a_regular_file_on_the_expected_device() {
    let root = std::env::temp_dir().join(format!("srx-probe-{}", std::process::id()));
    fs::create_dir(&root).unwrap();
    let dev = fs::metadata(&root).unwrap().dev();
    let expected = format!("{}:{}", libc::major(dev), libc::minor(dev));
    let marker = root.join(".nomedia");
    let probe = |path: &std::path::Path, device: &str| {
        read_passthrough_probe(path.to_str().unwrap(), device)
    };
    assert_eq!(
        probe(&marker, &expected).unwrap_err().raw_os_error(),
        Some(libc::ENOENT)
    );
    assert_eq!(fs::read_dir(&root).unwrap().count(), 0);
    fs::write(&marker, []).unwrap();
    assert!(probe(&marker, &expected).unwrap());
    fs::write(&marker, b"preserve").unwrap();
    let identity = fs::metadata(&marker).unwrap().ino();
    assert!(probe(&marker, &expected).unwrap());
    assert_eq!(fs::metadata(&marker).unwrap().ino(), identity);
    assert_eq!(fs::read(&marker).unwrap(), b"preserve");
    assert_eq!(
        probe(&marker, "wrong-device").unwrap_err().raw_os_error(),
        Some(libc::EXDEV)
    );
    assert_eq!(
        probe(&root, &expected).unwrap_err().raw_os_error(),
        Some(libc::EXDEV)
    );
    let link = root.join("link");
    symlink(&marker, &link).unwrap();
    assert_eq!(
        probe(&link, &expected).unwrap_err().raw_os_error(),
        Some(libc::EXDEV)
    );
    fs::remove_dir_all(root).unwrap();
}
