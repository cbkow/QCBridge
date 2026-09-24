//! The session token's home, and what is derived from it.
//!
//! Until 2026-09-24 the token sat in `agent.toml` in clear, was copied into
//! the addon's preferences and its JSON mirror, and rode to Blender in an
//! environment variable. Now the agent is the only holder: the token lives
//! in the OS credential store (macOS Keychain, Windows Credential Manager;
//! a 0600 file for other platforms and for tests), `agent.toml` never
//! carries it, and what the addon receives is derived — a fingerprint to
//! compare across the two ends, the SRT passphrase it must hand to ffmpeg
//! and the viewer, and the hello secret for the application-level check.
//!
//! The derivations are mirrored in `qcbridge/ring1/protocol.py`; the unit
//! test here and `tests/test_protocol.py` pin the same vectors.

use anyhow::{Context, Result, anyhow};
use std::path::Path;

pub const SERVICE: &str = "QCBridge Agent";

/// Where the token is kept. `keychain` is the default; `file` is a
/// owner-only file beside the config (tests, platforms without a store);
/// `toml` keeps the pre-2026-09-24 behaviour of a clear-text field.
pub const STORES: &[&str] = &["keychain", "file", "toml"];

fn sha256_hex(input: &str) -> String {
    hex::encode(ring::digest::digest(&ring::digest::SHA256, input.as_bytes()).as_ref())
}

/// Eight hex characters, enough for two people to see they typed
/// different tokens without either showing the other theirs.
pub fn fingerprint(token: &str) -> String {
    if token.is_empty() { String::new() } else { sha256_hex(&format!("qcb-token:{token}"))[..8].to_string() }
}

/// `protocol.srt_passphrase`: 32 hex chars, empty token = unencrypted.
pub fn srt_passphrase(token: &str) -> String {
    if token.is_empty() { String::new() } else { sha256_hex(&format!("qcb-srt:{token}"))[..32].to_string() }
}

/// `protocol.hello_secret`: what the addon's hello carries and checks in
/// agent mode, so the raw token never reaches Blender.
pub fn hello_secret(token: &str) -> String {
    if token.is_empty() { String::new() } else { sha256_hex(&format!("qcb-hello:{token}")) }
}

/// One account per role and config directory, so `--config` instances
/// (host + replica on a dev box, two agents in a test) keep separate tokens.
fn account(base: &Path, role: &str) -> String {
    format!("{role} @ {}", base.display())
}

fn file_path(base: &Path, role: &str) -> std::path::PathBuf {
    base.join(format!("{role}.token"))
}

/// Read the stored token; `Ok(None)` when there is none.
pub fn load(store: &str, base: &Path, role: &str) -> Result<Option<String>> {
    match store {
        "keychain" => os::read(&account(base, role)),
        "file" => match std::fs::read_to_string(file_path(base, role)) {
            Ok(s) => Ok(Some(s.trim_end_matches(['\r', '\n']).to_string())),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(e).with_context(|| format!("read {}", file_path(base, role).display())),
        },
        "toml" => Ok(None),
        other => Err(anyhow!("unknown token_store {other:?}")),
    }
}

/// Store the token; an empty token removes it.
pub fn store(store: &str, base: &Path, role: &str, token: &str) -> Result<()> {
    match store {
        "keychain" => {
            if token.is_empty() { os::delete(&account(base, role)) } else { os::write(&account(base, role), token) }
        }
        "file" => {
            let path = file_path(base, role);
            if token.is_empty() {
                match std::fs::remove_file(&path) {
                    Ok(()) => Ok(()),
                    Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
                    Err(e) => Err(e).with_context(|| format!("remove {}", path.display())),
                }
            } else {
                std::fs::create_dir_all(base)?;
                std::fs::write(&path, token).with_context(|| format!("write {}", path.display()))?;
                owner_only(&path);
                Ok(())
            }
        }
        "toml" => Ok(()),
        other => Err(anyhow!("unknown token_store {other:?}")),
    }
}

/// Owner-only permissions on a secret-bearing file. On Windows the
/// per-user profile directory already carries that ACL.
pub fn owner_only(path: &Path) {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600));
    }
    #[cfg(not(unix))]
    {
        let _ = path;
    }
}

// ---------------------------------------------------------------- macOS ----

#[cfg(target_os = "macos")]
mod os {
    use super::SERVICE;
    use anyhow::{Result, anyhow};
    use core_foundation::base::{CFType, CFTypeRef, TCFType};
    use core_foundation::boolean::CFBoolean;
    use core_foundation::data::CFData;
    use core_foundation::dictionary::{CFDictionary, CFDictionaryRef};
    use core_foundation::string::{CFString, CFStringRef};

    #[link(name = "Security", kind = "framework")]
    unsafe extern "C" {
        static kSecClass: CFStringRef;
        static kSecClassGenericPassword: CFStringRef;
        static kSecAttrService: CFStringRef;
        static kSecAttrAccount: CFStringRef;
        static kSecValueData: CFStringRef;
        static kSecReturnData: CFStringRef;
        static kSecMatchLimit: CFStringRef;
        static kSecMatchLimitOne: CFStringRef;
        fn SecItemAdd(attributes: CFDictionaryRef, result: *mut CFTypeRef) -> i32;
        fn SecItemCopyMatching(query: CFDictionaryRef, result: *mut CFTypeRef) -> i32;
        fn SecItemUpdate(query: CFDictionaryRef, attributes: CFDictionaryRef) -> i32;
        fn SecItemDelete(query: CFDictionaryRef) -> i32;
    }

    const ERR_SEC_ITEM_NOT_FOUND: i32 = -25300;
    const ERR_SEC_DUPLICATE_ITEM: i32 = -25299;

    fn key(k: CFStringRef) -> CFString {
        unsafe { CFString::wrap_under_get_rule(k) }
    }

    fn query(account: &str) -> Vec<(CFString, CFType)> {
        unsafe {
            vec![
                (key(kSecClass), key(kSecClassGenericPassword).as_CFType()),
                (key(kSecAttrService), CFString::new(SERVICE).as_CFType()),
                (key(kSecAttrAccount), CFString::new(account).as_CFType()),
            ]
        }
    }

    fn dict(pairs: &[(CFString, CFType)]) -> CFDictionary<CFString, CFType> {
        CFDictionary::from_CFType_pairs(pairs)
    }

    fn status(what: &str, s: i32) -> anyhow::Error {
        anyhow!("Keychain {what} failed (OSStatus {s})")
    }

    pub fn read(account: &str) -> Result<Option<String>> {
        let mut q = query(account);
        unsafe {
            q.push((key(kSecReturnData), CFBoolean::true_value().as_CFType()));
            q.push((key(kSecMatchLimit), key(kSecMatchLimitOne).as_CFType()));
        }
        let q = dict(&q);
        let mut out: CFTypeRef = std::ptr::null();
        let s = unsafe { SecItemCopyMatching(q.as_concrete_TypeRef(), &mut out) };
        if s == ERR_SEC_ITEM_NOT_FOUND {
            return Ok(None);
        }
        if s != 0 || out.is_null() {
            return Err(status("read", s));
        }
        let data: CFData = unsafe { CFData::wrap_under_create_rule(out as _) };
        Ok(Some(String::from_utf8_lossy(data.bytes()).into_owned()))
    }

    pub fn write(account: &str, token: &str) -> Result<()> {
        let value = CFData::from_buffer(token.as_bytes());
        let mut attrs = query(account);
        unsafe { attrs.push((key(kSecValueData), value.as_CFType())) };
        let attrs = dict(&attrs);
        let s = unsafe { SecItemAdd(attrs.as_concrete_TypeRef(), std::ptr::null_mut()) };
        if s == 0 {
            return Ok(());
        }
        if s != ERR_SEC_DUPLICATE_ITEM {
            return Err(status("add", s));
        }
        let q = dict(&query(account));
        let upd = unsafe { dict(&[(key(kSecValueData), value.as_CFType())]) };
        let s = unsafe { SecItemUpdate(q.as_concrete_TypeRef(), upd.as_concrete_TypeRef()) };
        if s == 0 { Ok(()) } else { Err(status("update", s)) }
    }

    pub fn delete(account: &str) -> Result<()> {
        let q = dict(&query(account));
        let s = unsafe { SecItemDelete(q.as_concrete_TypeRef()) };
        if s == 0 || s == ERR_SEC_ITEM_NOT_FOUND { Ok(()) } else { Err(status("delete", s)) }
    }
}

// -------------------------------------------------------------- Windows ----

#[cfg(windows)]
mod os {
    use super::SERVICE;
    use anyhow::{Result, anyhow};
    use windows_sys::Win32::Foundation::ERROR_NOT_FOUND;
    use windows_sys::Win32::Security::Credentials::{
        CRED_PERSIST_LOCAL_MACHINE, CRED_TYPE_GENERIC, CREDENTIALW, CredDeleteW, CredFree, CredReadW, CredWriteW,
    };

    fn wide(s: &str) -> Vec<u16> {
        s.encode_utf16().chain(std::iter::once(0)).collect()
    }

    fn target(account: &str) -> Vec<u16> {
        wide(&format!("{SERVICE}/{account}"))
    }

    pub fn read(account: &str) -> Result<Option<String>> {
        let t = target(account);
        let mut ptr: *mut CREDENTIALW = std::ptr::null_mut();
        unsafe {
            if CredReadW(t.as_ptr(), CRED_TYPE_GENERIC, 0, &mut ptr) == 0 {
                let e = std::io::Error::last_os_error();
                if e.raw_os_error() == Some(ERROR_NOT_FOUND as i32) {
                    return Ok(None);
                }
                return Err(anyhow!("Credential Manager read failed: {e}"));
            }
            let c = &*ptr;
            let bytes = if c.CredentialBlob.is_null() || c.CredentialBlobSize == 0 {
                Vec::new()
            } else {
                std::slice::from_raw_parts(c.CredentialBlob, c.CredentialBlobSize as usize).to_vec()
            };
            CredFree(ptr as *const _);
            Ok(Some(String::from_utf8_lossy(&bytes).into_owned()))
        }
    }

    pub fn write(account: &str, token: &str) -> Result<()> {
        let t = target(account);
        let user = wide(account);
        let blob = token.as_bytes().to_vec(); // UTF-8, as UFB's generic entries
        let cred = CREDENTIALW {
            Type: CRED_TYPE_GENERIC,
            TargetName: t.as_ptr() as *mut _,
            UserName: user.as_ptr() as *mut _,
            CredentialBlobSize: blob.len() as u32,
            CredentialBlob: blob.as_ptr() as *mut _,
            Persist: CRED_PERSIST_LOCAL_MACHINE,
            ..Default::default()
        };
        if unsafe { CredWriteW(&cred, 0) } == 0 {
            return Err(anyhow!("Credential Manager write failed: {}", std::io::Error::last_os_error()));
        }
        Ok(())
    }

    pub fn delete(account: &str) -> Result<()> {
        let t = target(account);
        if unsafe { CredDeleteW(t.as_ptr(), CRED_TYPE_GENERIC, 0) } == 0 {
            let e = std::io::Error::last_os_error();
            if e.raw_os_error() == Some(ERROR_NOT_FOUND as i32) {
                return Ok(());
            }
            return Err(anyhow!("Credential Manager delete failed: {e}"));
        }
        Ok(())
    }
}

// ---------------------------------------------------------------- other ----

#[cfg(not(any(target_os = "macos", windows)))]
mod os {
    use anyhow::{Result, anyhow};
    pub fn read(_account: &str) -> Result<Option<String>> {
        Err(anyhow!("no OS credential store on this platform; set token_store = \"file\""))
    }
    pub fn write(_account: &str, _token: &str) -> Result<()> {
        Err(anyhow!("no OS credential store on this platform; set token_store = \"file\""))
    }
    pub fn delete(_account: &str) -> Result<()> {
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The same vectors as tests/test_protocol.py: the two languages must
    /// derive identical values or the two ends never pair.
    #[test]
    fn derivations_match_the_python_side() {
        assert_eq!(fingerprint("tok"), "e796aeb8");
        assert_eq!(srt_passphrase("tok"), "394281a840f6f63396e7ee2ea3acc796");
        assert_eq!(hello_secret("tok"), "19d5258551c4531785e33f9c5b1a342d065834ad401871638842e198d69bc92a");
        assert_eq!(srt_passphrase("devtoken"), "22ac324cdd19e313f70edd1316823d94");
        assert_eq!(fingerprint(""), "");
        assert_eq!(srt_passphrase(""), "");
        assert_eq!(hello_secret(""), "");
    }

    #[test]
    fn file_store_round_trip_and_removal() {
        let dir = std::env::temp_dir().join(format!("qcb-secrets-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        assert_eq!(load("file", &dir, "replica").unwrap(), None);
        store("file", &dir, "replica", "s3cret").unwrap();
        assert_eq!(load("file", &dir, "replica").unwrap().as_deref(), Some("s3cret"));
        assert_eq!(load("file", &dir, "host").unwrap(), None, "per role");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = std::fs::metadata(dir.join("replica.token")).unwrap().permissions().mode() & 0o777;
            assert_eq!(mode, 0o600);
        }
        store("file", &dir, "replica", "").unwrap();
        assert_eq!(load("file", &dir, "replica").unwrap(), None);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn unknown_store_is_refused() {
        assert!(load("vault", Path::new("/tmp"), "host").is_err());
        assert!(store("vault", Path::new("/tmp"), "host", "x").is_err());
    }
}
