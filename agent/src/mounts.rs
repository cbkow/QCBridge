//! The mount table, for the settings window's folder pickers: when the
//! chosen folder is on a network share, propose the other platform's form
//! of the same path so a mapping row can be filled from one click.
//!
//! macOS: `mount` lists `//user@server/share on /Volumes/share (smbfs, …)`;
//! a path under `/Volumes/share` becomes `\\server\share\…`. Windows: a
//! mapped drive `M:` resolves through `WNetGetConnectionW` to its UNC root,
//! and a UNC path is already one; `\\server\share\…` becomes
//! `/Volumes/share/…`, which is where macOS mounts a share by default
//! (the user may rename it; the field stays editable).
//!
//! Parsing is pure and unit-tested on synthetic text; only `table()` talks
//! to the OS.

use std::path::Path;

/// One network mount: the UNC root and where it is on this machine.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Mount {
    /// `\\server\share`, no trailing separator.
    pub unc: String,
    /// The local root: `/Volumes/share` on macOS, `M:` on Windows.
    pub local: String,
}

/// `//user@server/share on /Volumes/share (smbfs, …)` lines → mounts.
/// Also takes `//server/share` (no user) and the `afpfs`/`nfs` forms whose
/// source is `server:/export`.
pub fn parse_mac_mount(text: &str) -> Vec<Mount> {
    let mut out = Vec::new();
    for line in text.lines() {
        let Some((src, rest)) = line.split_once(" on ") else { continue };
        let local = rest.split(" (").next().unwrap_or("").trim();
        if local.is_empty() || !local.starts_with('/') {
            continue;
        }
        let unc = if let Some(stripped) = src.strip_prefix("//") {
            let host_share = stripped.rsplit_once('@').map(|(_, h)| h).unwrap_or(stripped);
            let mut parts = host_share.splitn(2, '/');
            let (Some(server), Some(share)) = (parts.next(), parts.next()) else { continue };
            format!("\\\\{server}\\{}", share.trim_end_matches('/').replace('/', "\\"))
        } else if let Some((server, export)) = src.split_once(":/") {
            if server.is_empty() || server.contains('/') {
                continue; // a local device like /dev/disk3s1
            }
            format!("\\\\{server}\\{}", export.trim_end_matches('/').replace('/', "\\"))
        } else {
            continue;
        };
        out.push(Mount { unc, local: local.trim_end_matches('/').to_string() });
    }
    out
}

/// `net use` style rows `M: \\server\share` → mounts (Windows).
pub fn parse_win_drives(rows: &[(String, String)]) -> Vec<Mount> {
    rows.iter()
        .filter(|(d, u)| d.len() == 2 && d.ends_with(':') && u.starts_with("\\\\"))
        .map(|(d, u)| Mount { unc: u.trim_end_matches('\\').to_string(), local: d.to_uppercase() })
        .collect()
}

fn starts_with_root(path: &str, root: &str, sep: char, case_insensitive: bool) -> bool {
    let (p, r) = if case_insensitive { (path.to_lowercase(), root.to_lowercase()) } else { (path.to_string(), root.to_string()) };
    p == r || p.starts_with(&format!("{r}{sep}"))
}

/// Given a folder on this machine, the same folder's form on the other
/// platform, if it is on a network mount: (this column, other column).
/// `None` when the folder is local, or the mount is not in the table.
pub fn other_form(local_path: &str, table: &[Mount]) -> Option<(String, String)> {
    let p = local_path.trim_end_matches(['/', '\\']);
    if cfg!(windows) || p.starts_with("\\\\") || (p.len() >= 2 && p.as_bytes()[1] == b':') {
        // Windows side: UNC as given, or a drive through the table.
        let unc = if p.starts_with("\\\\") {
            p.to_string()
        } else {
            let drive = p[..2].to_uppercase();
            let m = table.iter().find(|m| m.local.eq_ignore_ascii_case(&drive))?;
            format!("{}{}", m.unc, &p[2..])
        };
        // \\server\share\a\b -> /Volumes/share/a/b
        let body = unc.trim_start_matches('\\');
        let mut parts = body.splitn(3, '\\');
        let (_server, share) = (parts.next()?, parts.next()?);
        let rest = parts.next().unwrap_or("");
        let mac = if rest.is_empty() { format!("/Volumes/{share}") } else { format!("/Volumes/{share}/{}", rest.replace('\\', "/")) };
        Some((p.to_string(), mac))
    } else {
        let m = table.iter().find(|m| starts_with_root(p, &m.local, '/', false))?;
        let rest = &p[m.local.len()..];
        let win = format!("{}{}", m.unc, rest.replace('/', "\\"));
        Some((p.to_string(), win))
    }
}

/// The live table of this machine's network mounts.
pub fn table() -> Vec<Mount> {
    #[cfg(target_os = "macos")]
    {
        let out = std::process::Command::new("/sbin/mount").output();
        match out {
            Ok(o) => parse_mac_mount(&String::from_utf8_lossy(&o.stdout)),
            Err(_) => Vec::new(),
        }
    }
    #[cfg(windows)]
    {
        use windows_sys::Win32::NetworkManagement::WNet::WNetGetConnectionW;
        let mut rows = Vec::new();
        for letter in b'D'..=b'Z' {
            let drive = format!("{}:", letter as char);
            let name: Vec<u16> = drive.encode_utf16().chain(std::iter::once(0)).collect();
            let mut buf = vec![0u16; 1024];
            let mut len = buf.len() as u32;
            let rc = unsafe { WNetGetConnectionW(name.as_ptr(), buf.as_mut_ptr(), &mut len) };
            if rc == 0 {
                let end = buf.iter().position(|&c| c == 0).unwrap_or(buf.len());
                rows.push((drive, String::from_utf16_lossy(&buf[..end])));
            }
        }
        parse_win_drives(&rows)
    }
    #[cfg(not(any(target_os = "macos", windows)))]
    {
        Vec::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const MAC: &str = "\
/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)
devfs on /dev (devfs, local, nobrowse)
//chris@nas.local/jobs on /Volumes/jobs (smbfs, nodev, nosuid, mounted by chris)
//nas.local/assets/ on /Volumes/assets (smbfs, nodev, nosuid, mounted by chris)
filer:/export/renders on /Volumes/renders (nfs, nodev, nosuid)
";

    #[test]
    fn mac_mount_lines_become_unc_roots() {
        let t = parse_mac_mount(MAC);
        assert_eq!(t, vec![
            Mount { unc: "\\\\nas.local\\jobs".into(), local: "/Volumes/jobs".into() },
            Mount { unc: "\\\\nas.local\\assets".into(), local: "/Volumes/assets".into() },
            Mount { unc: "\\\\filer\\export\\renders".into(), local: "/Volumes/renders".into() },
        ]);
    }

    #[test]
    fn a_mac_folder_on_a_share_gets_its_windows_form() {
        let t = parse_mac_mount(MAC);
        assert_eq!(
            other_form("/Volumes/jobs/2026/spot", &t),
            Some(("/Volumes/jobs/2026/spot".into(), "\\\\nas.local\\jobs\\2026\\spot".into()))
        );
        assert_eq!(other_form("/Volumes/jobs/", &t).unwrap().1, "\\\\nas.local\\jobs");
        assert_eq!(other_form("/Volumes/jobs-archive/x", &t), None, "component boundary");
        assert_eq!(other_form("/Users/chris/Desktop", &t), None, "local folder");
    }

    #[test]
    fn a_windows_folder_gets_its_mac_form() {
        let t = parse_win_drives(&[("M:".into(), "\\\\nas.local\\jobs".into()), ("C:".into(), "local".into())]);
        assert_eq!(t.len(), 1);
        assert_eq!(
            other_form("\\\\nas.local\\jobs\\2026\\spot", &t),
            Some(("\\\\nas.local\\jobs\\2026\\spot".into(), "/Volumes/jobs/2026/spot".into()))
        );
        assert_eq!(other_form("\\\\nas.local\\jobs", &t).unwrap().1, "/Volumes/jobs");
        assert_eq!(
            other_form("m:\\2026\\spot", &t),
            Some(("m:\\2026\\spot".into(), "/Volumes/jobs/2026/spot".into()))
        );
        assert_eq!(other_form("D:\\local", &t), None, "a drive that is not a mapping");
    }
}
