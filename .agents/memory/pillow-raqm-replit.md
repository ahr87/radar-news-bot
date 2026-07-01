---
name: Pillow Arabic (raqm) text shaping on Replit
description: Pillow needs libraqm to correctly shape/order Arabic (and other complex-script) text; on Replit the Nix-installed libraqm path doesn't automatically reach the running process's LD_LIBRARY_PATH.
---

## Symptom

Drawing Arabic (or other RTL/complex-script) text with `PIL.ImageDraw.text()` produces garbled output:
letters within a word appear reversed, or word order is wrong, even though the same code and fonts work
correctly on another machine/environment. `PIL.features.check("raqm")` returns `False`.

## Root cause

Pillow's correct Arabic/complex-script rendering (shaping + BiDi reordering) requires the native
`libraqm` shared library to be loadable at runtime. Installing it as a Nix system dependency
(`replit.nix` / `installSystemDependencies`) succeeds, and Nix does compute the right path into
`REPLIT_LD_LIBRARY_PATH`, but that value does not automatically flow into the actual `LD_LIBRARY_PATH`
seen by an already-running Python process (env-wiring gap between Nix shell init and process env).
Without raqm, Pillow silently falls back to a "BASIC" layout engine that draws glyphs in raw
logical/memory order — which for RTL scripts visually reverses letters/words.

## Fix

In the app code itself (not just `replit.nix`), before any font is loaded, force-load the library
directly via `ctypes.CDLL(path, mode=ctypes.RTLD_GLOBAL)`, and pass
`layout_engine=ImageFont.Layout.RAQM` to `ImageFont.truetype(...)`. Locate the path by reading the
`REPLIT_LD_LIBRARY_PATH` (and `LD_LIBRARY_PATH`) env vars and checking each directory for
`libraqm.so.0` with `os.path.exists` — this makes the fix self-contained and independent of shell
env timing.

**Why:** This makes text rendering correct regardless of how/when the process was started, and
survives restarts/redeploys without relying on shell environment ordering.

**How to apply:** Any time a Replit Python project needs correct complex-script (Arabic, Hebrew,
Indic scripts, etc.) text shaping via Pillow, check `PIL.features.check("raqm")` first — if `False`,
apply this same preload pattern rather than trying to work around it with manual text reordering.

## Critical gotcha: never glob over `/nix/store`

`glob.glob("/nix/store/*-somepkg-*/...")` or `find /nix/store -maxdepth 1 -iname '*pkg*'` can hang
for 15s+ or indefinitely in this environment — `/nix/store` is enormous and directory listing is
unreliably slow. Never scan it at runtime. Always resolve Nix-provided library paths via the
`REPLIT_LD_LIBRARY_PATH` / `LD_LIBRARY_PATH` env vars (already computed by Nix) and a targeted
`os.path.exists` check instead.
