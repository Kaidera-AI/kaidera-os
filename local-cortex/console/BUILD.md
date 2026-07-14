# Building the Kaidera OS Console (R6)

Two ways to run this console. They run the **exact same ASGI app** (`app/main.py`
is shell-agnostic — no pywebview imports leak into it), so there are no code
branches between dev and packaged.

| Mode | Entry point | What you get |
|---|---|---|
| **Dev** | `uvicorn app.main:app` | Browser tab, hot reload, devtools — the tight HTMX/SSE loop |
| **Packaged** | `bootstrap.py` → `dist/Kaidera OS Console.app` | Native macOS window (WKWebView), dock icon, no browser chrome |

---

## Dev run (browser)

From this directory (`local-cortex/console/`):

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Port 8765 — the Cortex API owns 8501.
uvicorn app.main:app --port 8765 --reload
```

Open **http://127.0.0.1:8765/**. The local Cortex API should be running at
`http://localhost:8501` (the page still renders if it's down — the health pill
goes red and panels show empty states).

---

## Native Operator App (menu bar)

E011 uses a Syncthing-style menu-bar controller. It starts/stops/restarts the
existing Kaidera OS service, shows health, opens the real console in the default
browser, and delegates updates to the existing E010 update endpoints. It does
not embed the web UI or duplicate runtime logic.

### Run from source

```bash
swift run --package-path ../../native/macos/KaideraOSOperator KaideraOSOperator
```

### Build

```bash
swift build -c release --package-path ../../native/macos/KaideraOSOperator
```

The Swift app shells out to `local-cortex/console/scripts/kaidera-os operator ...`,
which is backed by `app.native_operator`. Keep menu actions wired through that
CLI seam. Preflight and Run Install / Repair are wrappers around the canonical
install root and `install.sh`; they are not a second installer.
Native dialogs show action results and log paths; the pure Swift formatters are
covered by `native/macos/KaideraOSOperator` package tests.

### Build DMG

From the repo root on macOS:

```bash
scripts/macos/build-operator-dmg.sh
```

Optional signing/notarization:

```bash
KAIDERA_OS_CODESIGN_IDENTITY="Developer ID Application: ..." \
KAIDERA_OS_NOTARY_PROFILE="kaidera-os-notary" \
scripts/macos/build-operator-dmg.sh
```

Output: `dist/macos/kaidera-os-operator-v<version>.dmg` plus `.sha256`,
`.metadata.json`, and `latest-macos.json` for `${KAIDERA_OS_DOWNLOADS_URL}` publication.
The metadata includes `signing.kind`, `signing.notarized`,
`signing.stapled`, and `public_release_ready` so publication surfaces can
distinguish local dogfood DMGs from Developer ID signed/notarized public DMGs.

### Public Release Readiness

Soft preflight for the local Mac packaging host:

```bash
scripts/macos/operator_release_readiness.py
```

Strict public-release preflight:

```bash
KAIDERA_OS_CODESIGN_IDENTITY="Developer ID Application: ..." \
KAIDERA_OS_NOTARY_PROFILE="kaidera-os-notary" \
scripts/macos/operator_release_readiness.py --strict
```

To verify the notary keychain profile with `notarytool history`:

```bash
KAIDERA_OS_CODESIGN_IDENTITY="Developer ID Application: ..." \
KAIDERA_OS_NOTARY_PROFILE="kaidera-os-notary" \
scripts/macos/operator_release_readiness.py --strict --verify-notary-profile
```

To force the DMG build to stop before packaging if public-release credentials
are missing:

```bash
KAIDERA_OS_REQUIRE_RELEASE_SIGNING=1 \
KAIDERA_OS_CODESIGN_IDENTITY="Developer ID Application: ..." \
KAIDERA_OS_NOTARY_PROFILE="kaidera-os-notary" \
scripts/macos/build-operator-dmg.sh
```

If notary profile verification should also run before packaging:

```bash
KAIDERA_OS_REQUIRE_RELEASE_SIGNING=1 \
KAIDERA_OS_VERIFY_NOTARY_PROFILE=1 \
KAIDERA_OS_CODESIGN_IDENTITY="Developer ID Application: ..." \
KAIDERA_OS_NOTARY_PROFILE="kaidera-os-notary" \
scripts/macos/build-operator-dmg.sh
```

The default build remains soft so local dogfood DMGs can be created without
release credentials. Public/customer DMGs should use the strict path.
Strict public builds should publish only metadata where
`public_release_ready=true`.

### DMG Install-Contract Smoke

After building a DMG, run the install-contract smoke:

```bash
scripts/macos/smoke_operator_dmg.py
```

To write durable release evidence:

```bash
VERSION="$(python3 -c 'from pathlib import Path; ns={}; exec(Path("local-cortex/console/app/version.py").read_text(), ns); print(ns["__version__"])')"
scripts/macos/smoke_operator_dmg.py --output "output/release/kaidera-os-operator-macos/evidence/dmg-smoke-v${VERSION}.json"
```

This mounts the DMG read-only, checks the app bundle, `/Applications` symlink,
README first-run instructions, Info.plist bundle metadata, Mach-O architecture,
codesign verification, metadata sidecar consistency, app-only payload boundary,
copy-only install into an Applications-style directory, and clean detach. It is
a DMG contract smoke, not a replacement for a separate clean-Mac reboot proof.

### Installed Operator Verifier

To verify the app already installed in `/Applications` without starting,
stopping, restarting, or updating Kaidera OS:

```bash
scripts/macos/verify_installed_operator.py --json
```

The verifier checks bundle identity, `CFBundleIconFile`, the `.icns` app icon,
the menu-bar template icon, the executable, and code-signature validity when
`codesign` is available. It is safe to run while local projects are active.

### Mac Operator Lifecycle Proof

After the DMG smoke passes, prove the installed operator can control the
canonical local Kaidera OS service:

```bash
VERSION="$(python3 -c 'from pathlib import Path; ns={}; exec(Path("local-cortex/console/app/version.py").read_text(), ns); print(ns["__version__"])')"
scripts/macos/prove_operator_lifecycle.py \
  --output "output/release/kaidera-os-operator-macos/evidence/operator-lifecycle-v${VERSION}.json"
```

The proof mounts the DMG, copies `Kaidera OS Operator.app` into a temporary
Applications-style directory, verifies `kaidera-os operator home`, reaches the
console, stops the LaunchAgent, starts it again, restarts it, and checks update
status through the canonical console endpoint. It does not apply updates or open
the browser unless explicitly requested:

```bash
scripts/macos/prove_operator_lifecycle.py --apply-update
scripts/macos/prove_operator_lifecycle.py --open-console
```

For reboot survival, run the proof once before reboot, reboot the Mac, then run:

```bash
scripts/macos/prove_operator_lifecycle.py --skip-dmg-install --post-reboot
```

### Cortex Install Preservation Verifier

The canonical installer provisions Cortex/app-DB on a fresh machine and
converges an existing install in place. To verify that contract around an
install/reinstall:

```bash
scripts/install/verify-cortex-install-contract.py static
scripts/install/verify-cortex-install-contract.py snapshot --output output/install-before.json
./install.sh
scripts/install/verify-cortex-install-contract.py snapshot --output output/install-after.json
scripts/install/verify-cortex-install-contract.py compare output/install-before.json output/install-after.json
```

The verifier fails if installer structure becomes destructive, if existing
Cortex/app-DB named volumes disappear or are recreated, or if existing
`local-cortex/.env` secrets are rotated. Snapshot files contain only secret
hashes, not secret values.

### Stage Website Publication Bundle

After the DMG build succeeds, stage the verified files for a website or deploy
pipeline:

```bash
scripts/macos/stage-operator-publication.sh
```

Default output:
`output/release/kaidera-os-operator-macos/`.
The default output directory is cleaned of old staged operator files before the
current release files are copied.

To stage into another directory:

```bash
scripts/macos/stage-operator-publication.sh /path/to/static/downloads/macos
```

Custom output directories are not cleaned by default. To explicitly prune stale
operator files in a custom staging directory:

```bash
scripts/macos/stage-operator-publication.sh --clean /path/to/staging/downloads/macos
```

The staging step verifies that the DMG, `.sha256`, versioned metadata, and
`latest-macos.json` agree before copying anything. It deliberately does not know
about a specific website repo; Kaidera OS owns release artifact correctness, while
the deployment pipeline owns publication.

## Historical Packaged Window (superseded)

The old pywebview app remains as a reference/build artifact, but E011 supersedes
it for Mac distribution. The target product is the menu-bar operator above plus
a DMG that installs the canonical browser-based console and local Cortex stack.

## Packaged run (legacy native window)

### 1. Install the build deps

The packaging stack (PyInstaller + pywebview's macOS PyObjC backend) lives in
`requirements-build.txt`, on top of the runtime deps:

```bash
source .venv/bin/activate
pip install -r requirements.txt -r requirements-build.txt
```

> **Python 3.14 note:** PyInstaller needs **>= 6.15** on Python 3.14 (older
> releases cap at `<3.14`). `requirements-build.txt` already pins `>=6.15.0`.

### 2. Build

```bash
pyinstaller console.spec
```

Output: **`dist/Kaidera OS Console.app`** (≈40 MB — WebKit is the OS's, not bundled).
A clean rebuild: `rm -rf build dist && pyinstaller console.spec`.

### 3. Launch (CTO / operator)

```bash
open "dist/Kaidera OS Console.app"
```

The launcher (`bootstrap.py`) starts uvicorn on a background **daemon thread** on
a **dynamic free loopback port**, then opens a native pywebview window pointed at
it. Closing the window flips `server.should_exit = True` so uvicorn (and its SSE
generators) shut down cleanly. The Cortex API at `:8501` should be up, same as
the dev run.

#### Run the launcher without building (quick window check)

```bash
python bootstrap.py
```

Same window, straight from source — handy for iterating on the window/lifecycle
before a full PyInstaller build.

---

## How it's wired (architecture)

- **pywebview owns the main (Cocoa) thread**; uvicorn runs off-main on a daemon
  thread. All WebKit ops must be on the main thread, so the server can't be there.
- **Single process** (`workers=1`, in-thread) — no `sys.executable` subprocess,
  no `--workers`. Both would re-trigger the multiprocessing spawn loop when frozen.
- **`multiprocessing.freeze_support()` is the first line of `bootstrap.main()`** —
  without it a frozen app that touches multiprocessing endlessly relaunches itself.
- **Resources** (`app/templates`, `app/static`) ship via the spec's `datas` and
  resolve at runtime through `app/main.py`'s `Path(__file__).parent` (PyInstaller
  rewrites that to the frozen `app/` dir under `sys._MEIPASS`). HTMX/CSS are
  vendored locally — the window works offline.

See `Program/Release_v0.1.0/E007_KAIDERA_OS_HARNESS_PLATFORM/research/2026-06-01-desktop-packaging.md`
for the full stack rationale and ranked risks, and `console.spec` for the
per-setting rationale.

---

## Code signing / Gatekeeper

PyInstaller **ad-hoc-signs** the Mach-O binaries by default (required on Apple
Silicon to load the bundled dylibs) — verify with `codesign -dv "dist/Kaidera OS
Console.app"` (`Signature=adhoc`). A locally-built `.app` carries no
`com.apple.quarantine` attribute, so Gatekeeper does **not** gate it on this Mac.

Shipping to *another* Mac needs a Developer ID signature + notarization. Escape
hatch on transfer: `xattr -d com.apple.quarantine "Kaidera OS Console.app"`.

## App icon (follow-up)

No icon is bundled yet — the only brand asset is a wide **white wordmark** SVG
(`app/static/kaidera-logo-official-white.svg`), which is the wrong aspect ratio
for an app icon and invisible on light backgrounds. The `.app` uses the default
macOS app icon. To brand it: drop a square `app.icns` next to `console.spec` and
set `ICON = "app.icns"` at the top of the spec.
