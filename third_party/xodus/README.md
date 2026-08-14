# Xodus — legitimate Minecraft Bedrock acquisition

Upstream: https://github.com/xodus-gaming/xodus (GPL-3.0)
Pinned commit: `4615749c6e02cc3b9acce2abbe9916fe8c376f9a`

## Why this is here

BedrockOnLinux used to install Minecraft by downloading a pre-extracted,
DRM-stripped copy of the game from a third-party GitHub repository. That
redistributes the game, and it let anyone install it without owning it.

Xodus replaces that with the path Windows itself uses. It signs in to the
user's own Microsoft account, asks Microsoft's licensing service for the
title license (which carries the content key), then streams the MSIXVC
package from the official Xbox CDN and decrypts it on the fly. The result is
the same extracted game directory the launcher already knows how to drive —
but the account has to actually own Minecraft.

Only `xodus-cli` is used. `xodus-service`, and the separate `xodus-gaming/wine`
and `xodus-gaming/Proton` trees, are not: BedrockOnLinux keeps its own WineGDK
engine, which carries the native Xbox identity, XUser and online-login work
that Xodus's stack does not have.

## What the launcher calls

| Command | Used for |
|---|---|
| `xodus-cli login` | One-time Microsoft sign-in (`InlineLogin.srf` in an embedded webview). |
| `xodus-cli streaming <product-id> <dir>` | Download + decrypt the current store build into `<dir>`. Incremental: it compares local segment hashes and fetches only the delta. |
| `xodus-cli download --dry-run <product-id>` | Resolve the store build number without downloading. |
| `xodus-cli run <dir> <wine>` | Decrypt the `keep_encrypted` segments into anonymous memfds and hand them to Wine. |

Product ids, resolved through DisplayCatalog:

| Edition | Product id | ContentId |
|---|---|---|
| Minecraft for Windows | `9NBLGGH2JHXJ` | `7792d9ce-355a-493c-afbd-768f4a77c3b0` |
| Minecraft Preview for Windows | `9P5X4QVLC2XR` | `98bd2335-9b01-4e4c-bd05-ccc01614078b` |

The Xbox CDN only serves the *current* build of a product id — there is no
back catalogue. This is why the launcher offers Release and Preview rather
than a list of Bedrock versions.

## Build flags that matter

`--features xodus/key-chain-file` is **required**, not optional. Without it
Xodus stores its tokens through a D-Bus secret service, which does not exist
in a Steam Deck Game Mode session or inside a Flatpak sandbox, and every
command that needs device credentials fails outright. With it, tokens go to
`~/.xodus-keyring.ron`.

`xodus-cli` links `wry`/`tao` unconditionally for the login webview, so
`libwebkit2gtk-4.1` is a build *and* runtime dependency. The `xodus` crate
runs `prost-build`, so `protoc` is needed at build time. It also depends on
`dbus-secret-service-keyring-store` with `crypto-openssl` unconditionally on
Linux — `key-chain-file` stops that store from being *used*, but it is still
compiled, so `libssl-dev` is required to build even though the file keyring
never touches it.

## License

Xodus is GPL-3.0; BedrockOnLinux is MIT. The launcher never links Xodus code —
it executes `xodus-cli` as a separate program, which is mere aggregation. The
published `xodus-cli-<rev>.tar.gz` carries `LICENSE.GPL-3.0` and a
`SOURCE-COMMIT` file, and `.github/workflows/build-xodus.yml` publishes the
matching source tarball alongside it so the GPL source requirement is met by
the same release that ships the binary.
