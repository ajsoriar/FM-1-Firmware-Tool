# Technical Details

This page covers the recovery checks, safety behavior, simulation, and development workflow. For the short setup guide, see [README.md](README.md).

## Supported packages and devices

- The installer accepts a local FM-1 `.fwsc` package. It does not extract firmware from `.exe` or `.zip` installers.
- The app uses the pinned recovery engine in `motor/`. It does not implement flash or SCSI operations itself.
- The recovery engine checks the device identity and boot mode. The MIDI port name can vary (for example, `Felucca`); the reported FM-1 identity is what matters.
- The installer does not connect to the internet. Dependencies must be installed separately from `motor/requirements-lock.txt`.

## Recovery and safety checks

Before writing, the installer checks the package and local environment, detects the FM-1, and reads the full flash twice. The two backup reads must match. The app then shows a summary and requires the exact confirmation `WRITE` before the recovery engine can write firmware.

If the FM-1 is running normal firmware, the app asks for `UBOOT` before sending the ROM key. The screen going black while the device enters ROM mode is expected. In ROM mode, the recovery engine checks for exactly one `WL82 UBOOT1.00` device, chip key `0x980F`, and Flash ID `0x856014`.

Writes are limited to `0x4000..0x92FFF`; the boot head at `0x0000..0x3FFF` is not written. The written region is read back and compared byte for byte. After a successful verification, power-cycle the FM-1 as prompted and use **Check boot** to confirm that it starts.

Do not disconnect or power off the FM-1 during erase, write, or verification. If the app reports that the firmware region could not be verified or may be incomplete, follow the displayed recovery warning and keep the device powered and connected.

Backups are saved by default under `Documents\FM1-Installer\backups`; logs go under `Documents\FM1-Installer\logs`. Backups contain device data and should be handled as private files.

## Package validation and pinned files

- If a package is listed in a local downloader capture (`manifest.json`), its SHA-256 is compared with the recorded hash. A mismatch blocks installation; a missing published hash is reported.
- `motor/pins.json` contains SHA-256 pins for the recovery engine and the included `jl-uboot-tool` files. The local environment is rejected if a pinned file changes.
- After deliberately reviewing an engine change, regenerate the pins with `py motor\update_pins.py` and review the resulting diff.
- The app does not use the recovery tool's `--yes`, `--i-know`, `--force-write`, `--device`, `setup`, or `repair-settings` options.

## Console commands

Run `.\fm1.bat install`, `.\fm1.bat check`, or `.\fm1.bat detect` from the project folder. Run the console as administrator for installation; the console cannot request elevation itself. `check` reads the package and environment without accessing the FM-1. `detect` performs a read-only device check.

`install` uses the same checks and confirmations as the GUI. It asks for `UBOOT` when needed and then `WRITE`; the confirmations cannot be skipped. After the write is verified, it prompts for the manual power cycle and checks boot. `--no-boot-check` exits before the boot check.

Exit codes:

- `0`: completed and verified, or `check`/`detect` passed.
- `1`: failed or cancelled before a partial write was reported.
- `2`: could not start because of an invalid package, environment, or missing device.
- `3`: the firmware region may be incomplete. Do not power off or disconnect the FM-1.
- `4`: write verified, but boot was not confirmed.

## Simulation

Simulation runs the recovery engine with mock devices and its `--dry-run` mode. It does not open a real device or write to flash.

```powershell
.\fm1.bat --simulate
.\fm1.bat install firmware.fwsc --simulate
.\fm1.bat install firmware.fwsc --simulate --simulate-fault read
.\fm1.bat install firmware.fwsc --simulate --simulate-fault write-always
.\fm1.bat install firmware.fwsc --simulate --simulate-start rom
```

Simulated backups go under `Documents\FM1-Installer\simulation`.

## Troubleshooting

- **The FM-1 is not detected:** use a USB data cable, connect directly to the computer, and close other apps that may be using the MIDI port. If needed, choose the FM-1's MIDI port in the app.
- **A package is rejected:** make sure you selected a complete `.fwsc` file. If a local catalog reports a hash mismatch, download the package again from its source.
- **The backup reads do not match:** do not continue. Try another USB cable or port, and avoid a hub.
- **The write cannot be verified:** do not power off or disconnect the FM-1. Follow the warning shown by the app.

## Development and builds

Run the hardware-free test suite from the project folder:

```powershell
py -m unittest discover -s tests
```

Build the Windows app with:

```powershell
./build.ps1
```

The script creates an isolated `.venv`, installs pinned runtime and build dependencies, checks `motor/pins.json`, runs the tests, and creates `dist/FM1FirmwareTool/` and `dist/FM1FirmwareTool-Windows-x64.zip`. The build currently uses Python 3.14. An internet connection is needed the first time to download build dependencies. The ZIP contains the full application folder; do not distribute the EXE on its own.

The GitHub Actions workflow `.github/workflows/build-windows.yml` runs manually or when a `v*` tag is pushed. It uploads the ZIP as a workflow artifact for 30 days.
