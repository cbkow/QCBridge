//! Renders the QCBridge icons from the QCView glyph (the "Q" magnifier
//! cutout, `qcviewcutout.svg` in QCView-Player) in QCBridge's own colour:
//!
//! - tray icons: the glyph alone with a status dot in the lower-right
//!   corner (green = paired, amber = waiting, red = off/error), plus one
//!   without a dot; 32 and 64 px;
//! - the window/app icon: the glyph on the dark rounded square QCView
//!   uses, 512 px and the standard sizes; a Windows .ico and, on macOS,
//!   an iconset folder for `iconutil`;
//! - a contact sheet of colour candidates when asked (`--sheet`).
//!
//! usage: qcb-icons <cutout.svg> <out dir> [--color RRGGBB] [--sheet]

use std::path::{Path, PathBuf};
use tiny_skia::{Color, FillRule, Paint, PathBuilder, Pixmap, Rect, Transform};

const DARK_BG: (u8, u8, u8) = (0x1f, 0x1f, 0x1f);

fn render_glyph(svg: &str, size: u32, color: &str) -> Pixmap {
    // The source declares its fill in a <style>; swap the colour there.
    let svg = svg.replace("fill: #e8c21f", &format!("fill: #{color}"));
    let opt = resvg::usvg::Options::default();
    let tree = resvg::usvg::Tree::from_str(&svg, &opt).expect("parse svg");
    let mut pm = Pixmap::new(size, size).unwrap();
    let s = size as f32 / tree.size().width();
    resvg::render(&tree, Transform::from_scale(s, s), &mut pm.as_mut());
    pm
}

fn rounded_square(size: u32, radius: f32, rgb: (u8, u8, u8)) -> Pixmap {
    let mut pm = Pixmap::new(size, size).unwrap();
    let mut paint = Paint::default();
    paint.set_color(Color::from_rgba8(rgb.0, rgb.1, rgb.2, 255));
    paint.anti_alias = true;
    let r = radius;
    let w = size as f32;
    let mut pb = PathBuilder::new();
    pb.move_to(r, 0.0);
    pb.line_to(w - r, 0.0);
    pb.quad_to(w, 0.0, w, r);
    pb.line_to(w, w - r);
    pb.quad_to(w, w, w - r, w);
    pb.line_to(r, w);
    pb.quad_to(0.0, w, 0.0, w - r);
    pb.line_to(0.0, r);
    pb.quad_to(0.0, 0.0, r, 0.0);
    pb.close();
    pm.fill_path(&pb.finish().unwrap(), &paint, FillRule::Winding, Transform::identity(), None);
    pm
}

fn circle(pm: &mut Pixmap, cx: f32, cy: f32, r: f32, rgb: (u8, u8, u8)) {
    let mut paint = Paint::default();
    paint.set_color(Color::from_rgba8(rgb.0, rgb.1, rgb.2, 255));
    paint.anti_alias = true;
    let path = PathBuilder::from_circle(cx, cy, r).unwrap();
    pm.fill_path(&path, &paint, FillRule::Winding, Transform::identity(), None);
}

fn over(dst: &mut Pixmap, src: &Pixmap, x: i32, y: i32) {
    let paint = tiny_skia::PixmapPaint::default();
    dst.draw_pixmap(x, y, src.as_ref(), &paint, Transform::identity(), None);
}

fn scaled(src: &Pixmap, size: u32) -> Pixmap {
    let mut pm = Pixmap::new(size, size).unwrap();
    let s = size as f32 / src.width() as f32;
    let mut paint = tiny_skia::PixmapPaint::default();
    paint.quality = tiny_skia::FilterQuality::Bicubic;
    pm.draw_pixmap(0, 0, src.as_ref(), &paint, Transform::from_scale(s, s), None);
    pm
}

fn app_icon(svg: &str, size: u32, color: &str) -> Pixmap {
    // QCView's app icon: the glyph fills ~88 % of a dark rounded square.
    let mut bg = rounded_square(size, size as f32 * 0.18, DARK_BG);
    let g = render_glyph(svg, (size as f32 * 0.86) as u32, color);
    let off = ((size - g.width()) / 2) as i32;
    over(&mut bg, &g, off, off);
    bg
}

fn tray_icon(svg: &str, size: u32, color: &str, dot: Option<(u8, u8, u8)>) -> Pixmap {
    let mut pm = Pixmap::new(size, size).unwrap();
    let g = render_glyph(svg, size, color);
    over(&mut pm, &g, 0, 0);
    if let Some(rgb) = dot {
        let r = size as f32 * 0.17;
        let c = size as f32 - r - 1.0;
        // A dark ring so the dot reads on the glyph and on any bar.
        circle(&mut pm, c, c, r + size as f32 * 0.05, DARK_BG);
        circle(&mut pm, c, c, r, rgb);
    }
    pm
}

fn save(pm: &Pixmap, path: &Path) {
    pm.save_png(path).expect("write png");
    println!("wrote {}", path.display());
}

/// A .ico holding PNG-compressed images (Vista+), which is what Windows
/// wants for an exe resource and the tray.
fn write_ico(images: &[Pixmap], path: &Path) {
    let mut out = Vec::new();
    out.extend_from_slice(&0u16.to_le_bytes());
    out.extend_from_slice(&1u16.to_le_bytes());
    out.extend_from_slice(&(images.len() as u16).to_le_bytes());
    let mut blobs: Vec<Vec<u8>> = images.iter().map(|p| p.encode_png().unwrap()).collect();
    let mut offset = 6 + 16 * images.len();
    for (pm, blob) in images.iter().zip(&blobs) {
        let w = if pm.width() >= 256 { 0u8 } else { pm.width() as u8 };
        out.push(w);
        out.push(w);
        out.push(0);
        out.push(0);
        out.extend_from_slice(&1u16.to_le_bytes());
        out.extend_from_slice(&32u16.to_le_bytes());
        out.extend_from_slice(&(blob.len() as u32).to_le_bytes());
        out.extend_from_slice(&(offset as u32).to_le_bytes());
        offset += blob.len();
    }
    for blob in blobs.drain(..) {
        out.extend_from_slice(&blob);
    }
    std::fs::write(path, out).expect("write ico");
    println!("wrote {}", path.display());
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() < 3 {
        eprintln!("usage: qcb-icons <cutout.svg> <out dir> [--color RRGGBB] [--sheet]");
        std::process::exit(2);
    }
    let svg = std::fs::read_to_string(&args[1]).expect("read svg");
    let out = PathBuf::from(&args[2]);
    std::fs::create_dir_all(&out).unwrap();
    let color = args.iter().position(|a| a == "--color").and_then(|i| args.get(i + 1)).cloned().unwrap_or_else(|| "8b7cf6".into());
    let sheet = args.iter().any(|a| a == "--sheet");

    if sheet {
        // Candidates side by side: app icon at 96, tray at 32 with the
        // three dots, and the QCView yellow first for the comparison.
        let cands = [("e8c21f", "QCView yellow"), ("8b7cf6", "violet"), ("2dd4bf", "teal"), ("ff7a45", "orange"), ("4f8ef7", "blue")];
        let cell = 140u32;
        let mut pm = rounded_square(cell * cands.len() as u32, 0.0, (0x2a, 0x2a, 0x2a));
        for (i, (c, _)) in cands.iter().enumerate() {
            let x = (i as u32 * cell) as i32;
            over(&mut pm, &app_icon(&svg, 96, c), x + 22, 10);
            over(&mut pm, &tray_icon(&svg, 32, c, Some((0x3d, 0xdc, 0x84))), x + 14, 112);
            over(&mut pm, &tray_icon(&svg, 32, c, Some((0xf5, 0xa6, 0x23))), x + 54, 112);
            over(&mut pm, &tray_icon(&svg, 32, c, Some((0xe5, 0x48, 0x48))), x + 94, 112);
        }
        save(&pm, &out.join("colour-sheet.png"));
        return;
    }

    // Tray: 32 and 64 px, four states.
    let dots: [(&str, Option<(u8, u8, u8)>); 4] = [
        ("plain", None),
        ("live", Some((0x3d, 0xdc, 0x84))),
        ("wait", Some((0xf5, 0xa6, 0x23))),
        ("off", Some((0xe5, 0x48, 0x48))),
    ];
    for (name, dot) in dots {
        for size in [32u32, 64] {
            save(&tray_icon(&svg, size, &color, dot), &out.join(format!("tray-{name}-{size}.png")));
        }
    }
    // App / window icon.
    let big = app_icon(&svg, 1024, &color);
    save(&big, &out.join("app-1024.png"));
    let mut ico_images = Vec::new();
    let iconset = out.join("qcbridge.iconset");
    std::fs::create_dir_all(&iconset).unwrap();
    for size in [16u32, 32, 48, 64, 128, 256, 512, 1024] {
        let pm = scaled(&big, size);
        if size <= 256 {
            ico_images.push(pm.clone());
        }
        if size != 48 && size != 1024 {
            save(&pm, &iconset.join(format!("icon_{size}x{size}.png")));
        }
        // The @2x names iconutil wants.
        if size >= 32 {
            save(&pm, &iconset.join(format!("icon_{0}x{0}@2x.png", size / 2)));
        }
        if size == 256 {
            save(&pm, &out.join("app-256.png"));
        }
    }
    write_ico(&ico_images, &out.join("qcbridge.ico"));
    println!("iconset at {} — run: iconutil -c icns {}", iconset.display(), iconset.display());
}
