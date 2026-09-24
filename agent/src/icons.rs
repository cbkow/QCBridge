//! The icons, embedded: QCView's "Q" glyph in mid grey (tray) and near
//! white on the dark square (window and app), rendered by
//! `tools/icons` into `assets/icons/` (decided 2026-09-24). The tray's
//! dot, lower-left, says how the agent stands.

pub const TRAY_PLAIN: &[u8] = include_bytes!("../assets/icons/tray-plain-64.png");
pub const TRAY_LIVE: &[u8] = include_bytes!("../assets/icons/tray-live-64.png");
pub const TRAY_WAIT: &[u8] = include_bytes!("../assets/icons/tray-wait-64.png");
pub const TRAY_OFF: &[u8] = include_bytes!("../assets/icons/tray-off-64.png");
pub const APP_256: &[u8] = include_bytes!("../assets/icons/app-256.png");

/// What the tray's dot shows.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum TrayState {
    /// A session is up.
    Live,
    /// Listening or dialling, nobody there yet.
    Waiting,
    /// No role runtime, a failed bind, or a switch that did not take.
    Off,
}

impl TrayState {
    pub fn png(self) -> &'static [u8] {
        match self {
            TrayState::Live => TRAY_LIVE,
            TrayState::Waiting => TRAY_WAIT,
            TrayState::Off => TRAY_OFF,
        }
    }

    /// From what the tray already knows: pairing first, then the status
    /// line's failure words.
    pub fn from_status(peer_up: bool, status: &str) -> TrayState {
        if peer_up {
            return TrayState::Live;
        }
        let s = status.to_ascii_lowercase();
        if s.starts_with("no role") || s.contains("failed") || s.contains("could not") || s.contains("cannot") {
            TrayState::Off
        } else {
            TrayState::Waiting
        }
    }
}

/// Decode one of the embedded PNGs to RGBA8.
pub fn rgba(png_bytes: &[u8]) -> (Vec<u8>, u32, u32) {
    let decoder = png::Decoder::new(png_bytes);
    let mut reader = decoder.read_info().expect("embedded png");
    let mut buf = vec![0u8; reader.output_buffer_size()];
    let info = reader.next_frame(&mut buf).expect("embedded png frame");
    let (w, h) = (info.width, info.height);
    let rgba = match info.color_type {
        png::ColorType::Rgba => buf[..info.buffer_size()].to_vec(),
        png::ColorType::Rgb => buf[..info.buffer_size()].chunks(3).flat_map(|p| [p[0], p[1], p[2], 255]).collect(),
        other => panic!("embedded png is {other:?}, expected RGBA"),
    };
    (rgba, w, h)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn embedded_icons_decode_to_64_px_rgba() {
        for bytes in [TRAY_PLAIN, TRAY_LIVE, TRAY_WAIT, TRAY_OFF] {
            let (rgba, w, h) = rgba(bytes);
            assert_eq!((w, h), (64, 64));
            assert_eq!(rgba.len(), 64 * 64 * 4);
        }
        let (rgba, w, h) = rgba(APP_256);
        assert_eq!((w, h), (256, 256));
        assert_eq!(rgba[3], 0, "the app icon's corner is transparent (rounded square)");
    }

    #[test]
    fn tray_state_follows_pairing_then_the_status_words() {
        assert_eq!(TrayState::from_status(true, "anything"), TrayState::Live);
        assert_eq!(TrayState::from_status(false, "listening"), TrayState::Waiting);
        assert_eq!(TrayState::from_status(false, "connecting to x"), TrayState::Waiting);
        assert_eq!(TrayState::from_status(false, "no role: listen on 0.0.0.0:1: permission denied"), TrayState::Off);
        assert_eq!(TrayState::from_status(false, "Blender launch failed: …"), TrayState::Off);
    }
}
