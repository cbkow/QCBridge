//! QCView's look, applied to egui: the same fonts (Inter for text,
//! JetBrains Mono for paths), the same sizes, near-square corners, and
//! the palette from QCView's `Theme.qml` — so the agent's window reads as
//! a sibling of the viewer rather than a developer tool.

use eframe::egui::{self, Color32, CornerRadius, FontData, FontDefinitions, FontFamily, FontId, Margin, Stroke, TextStyle, Vec2};
use std::sync::Arc;

// QCView Theme.qml, 2026-09-24.
#[allow(dead_code)]
pub const BG: Color32 = Color32::from_rgb(0x16, 0x16, 0x16);
pub const SURFACE: Color32 = Color32::from_rgb(0x1a, 0x1a, 0x1a);
pub const SURFACE_ALT: Color32 = Color32::from_rgb(0x1d, 0x1d, 0x1d);
pub const SURFACE_RECESS: Color32 = Color32::from_rgb(0x14, 0x14, 0x14);
pub const AFFORDANCE_IDLE: Color32 = Color32::from_rgb(0x26, 0x26, 0x26);
pub const AFFORDANCE_HOVER: Color32 = Color32::from_rgb(0x32, 0x32, 0x32);
pub const BORDER: Color32 = Color32::from_rgb(0x2a, 0x2a, 0x2a);
pub const BORDER_STRONG: Color32 = Color32::from_rgb(0x33, 0x33, 0x33);
pub const TEXT: Color32 = Color32::from_rgb(0xdd, 0xdd, 0xdd);
pub const TEXT_SECONDARY: Color32 = Color32::from_rgb(0x88, 0x88, 0x88);
pub const TEXT_MUTED: Color32 = Color32::from_rgb(0x66, 0x66, 0x66);
pub const TEXT_BRIGHT: Color32 = Color32::from_rgb(0xff, 0xff, 0xff);
pub const ACCENT: Color32 = Color32::from_rgb(0x01, 0x89, 0xf1);
pub const ACCENT_HOVER: Color32 = Color32::from_rgb(0x1b, 0x95, 0xf1);
pub const ACCENT_MUTED: Color32 = Color32::from_rgb(0x10, 0x39, 0x5b);
pub const SUCCESS: Color32 = Color32::from_rgb(0x4c, 0xb0, 0x50);
pub const WARN: Color32 = Color32::from_rgb(0xf5, 0xa6, 0x23);
pub const ERROR: Color32 = Color32::from_rgb(0xc0, 0x40, 0x40);

const INTER: &[u8] = include_bytes!("../../assets/fonts/Inter_18pt-Regular.ttf");
const INTER_BOLD: &[u8] = include_bytes!("../../assets/fonts/Inter_18pt-Bold.ttf");
const MONO: &[u8] = include_bytes!("../../assets/fonts/JetBrainsMono-Regular.ttf");

pub fn bold() -> FontFamily {
    FontFamily::Name("inter-bold".into())
}

pub fn apply(ctx: &egui::Context) {
    // Fonts: Inter proportional, JetBrains Mono monospace, Inter Bold as
    // a named family for headings.
    let mut fonts = FontDefinitions::default();
    fonts.font_data.insert("inter".into(), Arc::new(FontData::from_static(INTER)));
    fonts.font_data.insert("inter-bold".into(), Arc::new(FontData::from_static(INTER_BOLD)));
    fonts.font_data.insert("jetbrains-mono".into(), Arc::new(FontData::from_static(MONO)));
    fonts.families.entry(FontFamily::Proportional).or_default().insert(0, "inter".into());
    fonts.families.entry(FontFamily::Monospace).or_default().insert(0, "jetbrains-mono".into());
    fonts.families.insert(bold(), vec!["inter-bold".into(), "inter".into()]);
    ctx.set_fonts(fonts);

    ctx.all_styles_mut(|style| {
        // QCView's sizes: base 12, small 11, tiny 10, medium 13, large 14.
        style.text_styles = [
            (TextStyle::Small, FontId::new(11.0, FontFamily::Proportional)),
            (TextStyle::Body, FontId::new(12.5, FontFamily::Proportional)),
            (TextStyle::Button, FontId::new(12.0, FontFamily::Proportional)),
            (TextStyle::Heading, FontId::new(14.0, bold())),
            (TextStyle::Monospace, FontId::new(11.0, FontFamily::Monospace)),
        ]
        .into();
        style.spacing.item_spacing = Vec2::new(8.0, 5.0);
        style.spacing.button_padding = Vec2::new(9.0, 3.0);
        style.spacing.interact_size = Vec2::new(40.0, 21.0);
        style.spacing.indent = 14.0;
        style.spacing.window_margin = Margin::same(12);
        style.spacing.combo_width = 120.0;

        let v = &mut style.visuals;
        *v = egui::Visuals::dark();
        v.panel_fill = SURFACE;
        v.window_fill = SURFACE;
        v.extreme_bg_color = SURFACE_RECESS; // text edits, the "well"
        v.faint_bg_color = SURFACE_ALT; // striped rows
        v.window_stroke = Stroke::new(1.0, BORDER);
        v.override_text_color = None;
        v.weak_text_color = Some(TEXT_SECONDARY);
        v.hyperlink_color = ACCENT;
        v.selection.bg_fill = ACCENT_MUTED;
        v.selection.stroke = Stroke::new(1.0, ACCENT);
        v.text_cursor.stroke = Stroke::new(1.0, ACCENT);
        v.error_fg_color = ERROR;
        v.warn_fg_color = WARN;

        let r = CornerRadius::same(2);
        let w = &mut v.widgets;
        // Labels, separators, disabled things.
        w.noninteractive.bg_fill = SURFACE;
        w.noninteractive.weak_bg_fill = SURFACE;
        w.noninteractive.bg_stroke = Stroke::new(1.0, BORDER);
        w.noninteractive.fg_stroke = Stroke::new(1.0, TEXT);
        w.noninteractive.corner_radius = r;
        // Buttons, checkboxes, text-edit frames at rest: a filled
        // affordance with no border, like QCView's FlatButton.
        w.inactive.bg_fill = AFFORDANCE_IDLE;
        w.inactive.weak_bg_fill = AFFORDANCE_IDLE;
        w.inactive.bg_stroke = Stroke::NONE;
        w.inactive.fg_stroke = Stroke::new(1.0, TEXT);
        w.inactive.corner_radius = r;
        w.hovered.bg_fill = AFFORDANCE_HOVER;
        w.hovered.weak_bg_fill = AFFORDANCE_HOVER;
        w.hovered.bg_stroke = Stroke::new(1.0, BORDER_STRONG);
        w.hovered.fg_stroke = Stroke::new(1.0, TEXT_BRIGHT);
        w.hovered.corner_radius = r;
        w.hovered.expansion = 0.0;
        w.active.bg_fill = ACCENT;
        w.active.weak_bg_fill = ACCENT;
        w.active.bg_stroke = Stroke::new(1.0, ACCENT_HOVER);
        w.active.fg_stroke = Stroke::new(1.0, TEXT_BRIGHT);
        w.active.corner_radius = r;
        w.active.expansion = 0.0;
        w.open.bg_fill = AFFORDANCE_HOVER;
        w.open.weak_bg_fill = AFFORDANCE_HOVER;
        w.open.bg_stroke = Stroke::new(1.0, BORDER_STRONG);
        w.open.fg_stroke = Stroke::new(1.0, TEXT);
        w.open.corner_radius = r;
    });
}

/// A section title the way QCView's inspector draws them: small, upper
/// case, secondary tone, a hairline underneath.
pub fn section(ui: &mut egui::Ui, title: &str) {
    ui.add_space(6.0);
    ui.label(egui::RichText::new(title.to_uppercase()).small().color(TEXT_SECONDARY).font(FontId::new(10.5, bold())));
    let rect = ui.available_rect_before_wrap();
    let y = rect.top() + 2.0;
    ui.painter().hline(rect.left()..=rect.right(), y, Stroke::new(1.0, BORDER));
    ui.add_space(6.0);
}
