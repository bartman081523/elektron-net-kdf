// The `uint` crate's `construct_uint!` macro (v0.9.3, upstream) expands to
// code that trips `deprecated` (max_value()) and the
// `semicolon_in_expressions_from_non_local_macros` rustc lint on newer
// toolchains; the diagnostics land at the expansion site below, so allow
// them here (clippy runs CI with `-D warnings`).
#![expect(clippy::assign_op_pattern)]
#![expect(clippy::manual_div_ceil)]
#![allow(deprecated)]
#![allow(semicolon_in_expressions_from_non_local_macros)]

extern crate bitcoin_hashes;
extern crate byteorder;
extern crate rustc_hex as hex;
extern crate uint;

use uint::construct_uint;

construct_uint! {
    pub struct U256(4);
}

pub mod bytes;
pub mod compact;
pub mod hash;
