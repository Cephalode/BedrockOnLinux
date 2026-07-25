# Changelog

## 2.1.0 — 2026-07-25

- Fix Minecraft's in-game world and custom-skin file picker with a pinned,
  reproducible WineGDK engine patch.
- Add isolated local Xbox profiles that share large game, engine and runtime
  downloads under a shared lock while keeping accounts, prefixes, settings and
  worlds separate.
- Improve startup and dialogs across X11, XWayland, Hyprland, scaled displays
  and multi-monitor desktops, including automatic recovery from global native
  Wayland overrides and XWayland presentation issues.
- Fix borderless fullscreen in Steam Game Mode and on Steam Deck.
- Configure SDL or Steam Input automatically for DualSense and DualSense Edge
  controllers.
- Add read-only Xbox and LAN diagnostics, including routes, DNS, TLS, clock,
  VPN observations, version mismatches and actionable full-world guidance.
- Repair partial Wine prefixes and WineGDK activation safely, and refresh
  replaced same-tag game archives transactionally.
- Fix Wine boot failures by providing the missing native
  `cryptbase.SystemFunction036` random-number implementation.
- Add a guarded legacy WineD3D renderer for systems limited to Vulkan 1.2.
- Make GPU incident acknowledgement conditional, revalidated and available in
  the graphical interface, and remove cleanly completed launch markers
  automatically.
- Migrate Flatpak data to private XDG storage without destructive merging.
- Harden external client DLL injection and report immediate game crashes.
- Add `.mcskin` imports, clearer Xbox/connection errors and stricter release
  provenance, integrity and packaging checks.

Known limitations:

- Xodus support is not part of this release.
- Achievement submission has not been validated end to end.
- Hardware-specific stutter reported on the RX 9060 XT remains under
  investigation.
- Borion and other version-specific third-party DLLs remain unsupported and
  may crash the game.
