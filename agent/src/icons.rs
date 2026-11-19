//! The icons, embedded: QCView's "Q" glyph in mid grey (tray) and in the
//! family's accent blue (#0189f1) on the dark square (window and app - the
//! owner, 2026-11-19: "lets just use the regular QCView logo with a different
//! color ... the blue"; the QB mark of `revisedLogo.svg` was tried the same
//! day and set aside), rendered by
//! `tools/icons` into `assets/icons/` (decided 2026-09-24). The tray icon
//! carries NO status dot (the owner, 2026-11-19: "maybe the dot is not
//! needed and we just have status in the menu"): how the agent stands is the
//! menu's first line and the tooltip.

pub const TRAY_PLAIN: &[u8] = include_bytes!("../assets/icons/tray-plain-64.png");
pub const APP_256: &[u8] = include_bytes!("../assets/icons/app-256.png");

/// How the agent stands, as the tray tells it (the menu's status line; once the dot's colour).
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
        let _ = self;   // one picture for every state; the state is said in the menu
        TRAY_PLAIN
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
        for bytes in [TRAY_PLAIN] {
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
