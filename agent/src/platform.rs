//! Process hygiene that differs per OS. Everything here is a no-op or a
//! trivial success on Unix; the Windows side is what the paired runs of
//! 2026-09-23/24 asked for.
//!
//! The replica agent registered by the old logon task died with
//! `0xC000013A` (`STATUS_CONTROL_C_EXIT`): started by the scheduler it owned
//! a console, and a console can be closed, or handed a control event, by
//! anything. A hard kill of the agent then left Blender, the capture helper
//! and ffmpeg running with the agent's ports, and the next agent exited in
//! under a second with nothing written anywhere. So, on Windows:
//!
//! - the release binary is a GUI-subsystem executable (no console at all;
//!   `main.rs` carries the attribute), and re-attaches to a parent console
//!   only when one exists, so `--version` still prints from a terminal;
//! - the agent puts itself in a job object with kill-on-close, so every
//!   child and grandchild (Blender, the helper, the ffmpeg the addon spawns
//!   inside Blender) dies with it, however it dies;
//! - a named mutex per role and config directory refuses a second instance
//!   exactly, where the pid in `agent.json` could be fooled by a recycled pid;
//! - children are started without a console window of their own;
//! - a start failure shows a message box, since there is no console to read.

use std::path::Path;
use std::process::Command;

/// Held for the life of the process; dropping it releases the instance.
pub struct InstanceGuard {
    #[cfg(windows)]
    handle: windows_sys::Win32::Foundation::HANDLE,
}

// A HANDLE is a pointer type, which is neither Send nor Sync by default; a
// mutex handle is safe to hold from any thread.
#[cfg(windows)]
unsafe impl Send for InstanceGuard {}
#[cfg(windows)]
unsafe impl Sync for InstanceGuard {}

#[cfg(windows)]
impl Drop for InstanceGuard {
    fn drop(&mut self) {
        unsafe { windows_sys::Win32::Foundation::CloseHandle(self.handle) };
    }
}

/// A short, stable tag for the config directory, so `--config` in another
/// directory is another instance (tests run two on one box).
fn dir_tag(base: &Path) -> String {
    let key = base.to_string_lossy().to_lowercase();
    let mut h: u64 = 0xcbf2_9ce4_8422_2325; // FNV-1a
    for b in key.bytes() {
        h ^= u64::from(b);
        h = h.wrapping_mul(0x0000_0100_0000_01b3);
    }
    format!("{h:016x}")
}

/// The mutex name a given role and directory claim. Public for the test.
pub fn instance_name(base: &Path, role: &str) -> String {
    format!("Local\\QCBridgeAgent.{role}.{}", dir_tag(base))
}

#[cfg(windows)]
fn wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(std::iter::once(0)).collect()
}

/// Claim "the one <role> agent for this directory". `Err` carries a message
/// for the user when another instance already holds it.
pub fn claim_instance(base: &Path, role: &str) -> Result<InstanceGuard, String> {
    #[cfg(windows)]
    {
        use windows_sys::Win32::Foundation::{CloseHandle, ERROR_ALREADY_EXISTS};
        use windows_sys::Win32::System::Threading::CreateMutexW;
        let name = wide(&instance_name(base, role));
        let handle = unsafe { CreateMutexW(std::ptr::null(), 0, name.as_ptr()) };
        let err = std::io::Error::last_os_error();
        if handle.is_null() {
            // Cannot even ask; do not refuse to run over it, the pid check
            // in agent.json still stands.
            return Ok(InstanceGuard { handle: std::ptr::null_mut() });
        }
        if err.raw_os_error() == Some(ERROR_ALREADY_EXISTS as i32) {
            unsafe { CloseHandle(handle) };
            return Err(format!(
                "another {role} agent is already running for {} — quit it first \
                 (its tray icon), or point --config at a different directory",
                base.display()
            ));
        }
        Ok(InstanceGuard { handle })
    }
    #[cfg(not(windows))]
    {
        let _ = (base, role);
        Ok(InstanceGuard {})
    }
}

/// Put this process in a job that kills everything in it when the last
/// handle closes, i.e. when this process ends. Children inherit the job.
/// The handle is deliberately never closed while we run.
pub fn install_job_object() -> Result<(), String> {
    #[cfg(windows)]
    {
        use windows_sys::Win32::System::JobObjects::{
            AssignProcessToJobObject, CreateJobObjectW, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
            JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JobObjectExtendedLimitInformation, SetInformationJobObject,
        };
        use windows_sys::Win32::System::Threading::GetCurrentProcess;
        unsafe {
            let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            if job.is_null() {
                return Err(format!("CreateJobObject: {}", std::io::Error::last_os_error()));
            }
            let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            let ok = SetInformationJobObject(
                job,
                JobObjectExtendedLimitInformation,
                &info as *const _ as *const core::ffi::c_void,
                std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            );
            if ok == 0 {
                return Err(format!("SetInformationJobObject: {}", std::io::Error::last_os_error()));
            }
            // Nested jobs are allowed since Windows 8, so this works under
            // Task Scheduler's own job too.
            if AssignProcessToJobObject(job, GetCurrentProcess()) == 0 {
                return Err(format!("AssignProcessToJobObject: {}", std::io::Error::last_os_error()));
            }
        }
        Ok(())
    }
    #[cfg(not(windows))]
    {
        Ok(())
    }
}

/// A GUI-subsystem process has no console; when it was started from one
/// (a terminal running `qcbridge-agent --version`), attach to it so the
/// output lands there. Started by the Run key or a task there is no parent
/// console and this does nothing, which is the point.
pub fn attach_parent_console() {
    #[cfg(windows)]
    unsafe {
        use windows_sys::Win32::System::Console::{ATTACH_PARENT_PROCESS, AttachConsole};
        AttachConsole(ATTACH_PARENT_PROCESS);
    }
}

/// Start a child without a console window of its own. Blender, the capture
/// helper and ffmpeg are console programs; launched from a process without
/// a console each would open one on the desktop, and closing that ends the
/// child. Their output is redirected to files by the callers.
pub fn no_window(cmd: &mut Command) {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        use windows_sys::Win32::System::Threading::CREATE_NO_WINDOW;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    #[cfg(not(windows))]
    {
        let _ = cmd;
    }
}

/// Tell the user the agent could not start. Modal; called only on the way
/// out, from the main thread, when there is no console to print to.
pub fn fatal_dialog(title: &str, text: &str) {
    #[cfg(windows)]
    unsafe {
        use windows_sys::Win32::UI::WindowsAndMessaging::{MB_ICONERROR, MB_OK, MessageBoxW};
        MessageBoxW(std::ptr::null_mut(), wide(text).as_ptr(), wide(title).as_ptr(), MB_OK | MB_ICONERROR);
    }
    #[cfg(not(windows))]
    {
        let _ = (title, text);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn instance_names_differ_by_role_and_directory() {
        let a = instance_name(Path::new("/tmp/a"), "replica");
        let b = instance_name(Path::new("/tmp/b"), "replica");
        let c = instance_name(Path::new("/tmp/a"), "host");
        assert_ne!(a, b);
        assert_ne!(a, c);
        assert!(a.starts_with("Local\\QCBridgeAgent.replica."));
        // Windows paths compare case-insensitively; so must the tag.
        assert_eq!(instance_name(Path::new("C:\\Users\\X"), "host"), instance_name(Path::new("c:\\users\\x"), "host"));
    }

    #[test]
    fn claim_is_reentrant_on_unix_and_exclusive_on_windows() {
        let dir = std::env::temp_dir().join("qcb-agent-claim-test");
        let first = claim_instance(&dir, "replica");
        assert!(first.is_ok());
        let second = claim_instance(&dir, "replica");
        if cfg!(windows) {
            assert!(second.is_err(), "second claim must be refused");
        } else {
            assert!(second.is_ok());
        }
        drop(first);
        assert!(claim_instance(&dir, "replica").is_ok(), "released on drop");
    }
}
