//! The settings window (`qcbridge-agent --settings`): the tray's UI, as
//! decided 2026-09-24 (DESIGN-NOTES agent §8). One native window, its own
//! process, attached to the running agent as a control client. Every
//! value shown is the agent's: a field commits through `set_config` and
//! the `config` event that answers is what the field then shows.
//!
//! Since 2026-11-19 it is built on the family's UI kit (min-ui, min-theme,
//! min-present): `window.rs`. The egui window it replaced is gone, and with
//! it eframe and the fonts it embedded (the kit brings its own).
//!
//! Sections: this machine (name, Send/Receive, Blender), pairing (network,
//! phonebook, token, the peer and the replicas found), storage (cache
//! root, the mapping table with a picker per cell that proposes the other
//! platform's path from the mount table), stream, diagnostics.

mod client;
mod window;

pub use window::run;
