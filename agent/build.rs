// On Windows, the exe carries the app icon as a resource (Explorer, the
// taskbar, the settings window's title bar). Nothing to do elsewhere.
fn main() {
    println!("cargo:rerun-if-changed=assets/qcbridge.rc");
    println!("cargo:rerun-if-changed=assets/icons/qcbridge.ico");
    #[cfg(windows)]
    {
        let _ = embed_resource::compile("assets/qcbridge.rc", embed_resource::NONE);
    }
}
