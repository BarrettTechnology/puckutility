# Changelog

All notable changes to Puck Utility are documented in this file. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Entries
before 1.4.0 were reconstructed from the git history: version numbers come from
the release tags and the version shown in the window title at the time, and an
untagged version is dated by the commit that set it. Versions marked "not
released" were never tagged and were internal development builds.

## [Unreleased]

### Changed
- Saves follow firmware 5.0.7's procedure (p4core): a puck in NMT OPERATIONAL is sent to PRE-OPERATIONAL, the save is written, 0x1010 is read until bit 2 (save pending) clears, and the puck is started again. 5.0.7 refuses NV writes outside PRE-OP (abort 0x08000022); a puck already in PRE-OP gets no NMT command. Restoring defaults and changing the node ID go to PRE-OP the same way and reboot.
- The EDS is firmware 5.0.7's.
- The bench scripts `pos_noise_log`, `pwm_ceiling_walk`, `pdo_persistence_probe` and `settling_behavioral` save through p4core instead of writing 0x1010 themselves.

## [1.4.2] - 2026-11-12

### Added
- Command line: `serial --id N` reads the serial number (0x1018,4); `--set S` writes it (with the firmware's serial key where it has one), reads it back and saves it. A serial of 0 or 0xFFFFFFFF shows as "not set" and cannot be written.
- Command line: `set-id --pdo-ids predefined` also moves the PDO COB-IDs left on node 127's identifiers (pucks configured at 127 and re-numbered by a tool that kept the PDOs) to the puck's node ID, keeping disabled PDOs disabled. Without a new ID (`--id 13 set-id --pdo-ids predefined`) it fixes the puck's current ID with no reboot. It lists each change and asks first (`-y` to skip), then saves communication (Save.Comm). A PDO keyed to any other node is left alone. A COB-ID change disables the PDO first, as 5.x firmware requires.
- A/B package: square runs report the error held on each set-point plateau (set-point - 0x6064 over its last 0.5 s, with mean and max |e|) in `metrics.json` and the compare tables; `csvaudit.py` warns when a geared actuator's CSV leaves the settle integrator 0x2381,4 at 0.

### Fixed
- Saving to NV waits until the puck has committed the save (0x1010 save-pending bit), so a reboot right after a save (configuration load) no longer loses it, and a save the firmware refuses while the drive is enabled says "disable the drive before saving". The current-sense bias calibration now saves after the drive is back in IDLE.
- A failed calibration step no longer locks the Test menu: a one-time reminder names the steps that did not pass (the rest are saved), and All Pucks reports every puck's outcome.
- Calibration messages: friction asks before rescaling the velocity gains (old and new values shown), a cogging failure leaves the table as it was, CAN loss is named as such, one verdict line per step, and tracebacks go to the debug log only.
- EMCY 0x8140 is reported as CAN bus-off (a communication fault, not the supply) with the remedy.
- Scanning reports a CAN transmit-buffer error as such instead of "No Pucks Found", and scan sends now time out properly.
- The Linux launcher now exits with the app's status, so a failed command-line calibrate or flash no longer looks like success.
- Linux package: the desktop shortcut is written as the user rather than as root (a planted symlink could redirect it), and the stale per-user menu entry left by the v1.2.x installer is removed.

### Changed
- Command line: `--id N` asks that node only (one SDO read) instead of scanning the bus; `scan`, and commands without `--id` or with `--all`, still scan.
- A/B package: `compare` writes `./compare-<A>-<B>.json` (or `--out FILE`) and never into a run folder; the guide counts the resets `prepare` makes.
- At Scan the log shows the CAN link's bit timing, and a dialog warns once about old `/etc` CAN files that override the installed bring-up.
- CAN packages: `/etc` copies left by older Barrett apps are diverted so they no longer override the `/usr/lib` bring-up (restored on removal); the installer never downgrades a shared CAN package and keeps an adapter another puck app still needs.
- `setup-socketcan.sh` installs to `/usr/lib` and moves `/etc` copies aside.
- Requires p4core 0.1.1 or later (`set-id --pdo-ids`); install both wheels together.

## [1.4.1] - 2026-11-03

### Added
- Command line: `version`, NV save/restore, and `wipe-nvm --no-template` for models without a shipped template.
- `calibrate` takes step groups (`offsets`, `gainfactor`, `encoder-offset`, `ab`) and `--json` emits one row per step with status, detail and duration.
- A/B test package tooling (`abtest/`): CSV audit, CSV diff against a puck's read-back, per-firmware CSV templates, an A/B runner and a package builder.
- `sandbox/testp4.py` rewritten as one CiA 402 test per mode, reporting PASS/FAIL/SKIP on both the 4.3.x and 5.x firmware lines.

### Changed
- Calibration step 8 is now "Friction calibration" (drag J/B/Tc followed by the Coulomb feed-forward), replacing the separate friction feed-forward step.
- Configure saves settings by range (Manufacturer + Application) instead of Save All, and maps the default PDOs from p4core.
- `calibrate` only asks for confirmation when the motor will turn, and mentions cogging only when that step runs.
- CAN bring-up tries the bench CAN FD timing first, then default FD timing, then classic CAN; the CANable fallback no longer passes FD without a data bitrate.
- Linux builds run only on Ubuntu 20.04 (the builder image is used everywhere else).
- Uninstalling keeps CAN support that another installed puck app uses, and upgrading the deb keeps the NetworkManager setting.

## [1.4.0] - 2026-11-02

### Added
- `puckutility/version.py` holds the version, shown in the title bar and by `--version` / `--help` (also for `puckutility-gui`); build scripts, CI and installers read it.
- Windows installer can be built on Linux under wine (`build-win32-wine.sh`).
- Builds ship an empty `firmware/` folder with a README pointing to the firmware download page; it is user-writable.

### Changed
- The GUI opens no CAN port until Scan Pucks, and no longer broadcasts an NMT reset at startup (opening an unconnected adapter could hang the machine and the reset disturbed drives on a shared bus).
- Calibration steps save to NV as they pass, so a late failure no longer loses earlier results; the Save-to-NV prompts are gone.
- `calibrate --no-save` leaves the winding, runout and cogging results in RAM (replaces the ignored `--save`).
- Linux GUI install from pip uses the prebuilt wxPython wheel for the running Ubuntu release; the install hint and README give the exact command.
- The window title no longer carries "- DEV" for release builds.
- `build-win32-installer.sh` accepts an activated venv and gives clearer errors.

## [1.3.0] - 2026-05-15 (not released)

### Added
- Command-line interface with subcommands (`scan`, `info`, `flash`, `config`, `set-id`, `reset`, `calibrate [STEP]`, `system-config`, `flash-canable`); the old flags still work, and the CLI runs without wxPython.
- Installable package: `puckutility` (CLI) and `puckutility-gui`, built on the shared p4core library.
- Actuator Calibration menu (Active Puck Ctrl+C / All Pucks) running numbered steps in dependency order: encoder filter + rest test, current-sense bias, gain, slope and offset fold, electrical zero + winding order, encoder runout table, friction, cogging feed-forward; each step can also be run on its own.
- Current-sense slope and drive-gated offset calibration, and current-sense settling-time calibration.
- Encoder error compensation calibration (top two harmonics uploaded, with the encoder tracking-filter model).
- Electrical zero from a whole-revolution sweep, replacing the single-point alignment.
- CANable adapter support: device selection from the menu, `flash-canable` to load its firmware, on Linux and Windows.
- CAN FD support, and a Candlelight 2.5 driver note for Windows.
- Set ID remaps and saves all PDO COB-IDs to the new node ID (GUI and `--set-id`).
- Detection of multiple pucks with the same ID.
- Firmware-aware menus: items the puck's firmware cannot run are hidden, and Calibrate Active Puck lists only supported steps.
- Support for firmware 4.4, 4.5 and 5.x object dictionaries (EDS updated), including DS402 user polarity (0x607E) and current shown per-mille of rated current.
- Linux `.run` installer (Ubuntu 20.04 to 26.04) and Windows setup (10/11), each with optional PEAK and CANable driver components; separate `puck-can-*` packages own the udev rules and CAN bring-up service.
- GitLab CI builds the release on tags and checks the tag against the version; `CONTRIBUTING.md` documents tests and the release checklist.
- Fault descriptions for manufacturer codes 0x2312 to 0x2315 (0x2315 is bus under-voltage).
- Developer and diagnostic scripts: `read_cal.py` calibration auditor, `set-can-iface.sh`, top-speed and noise tools, SIL simulator support over vcan.

### Changed
- Calibrations moved into libraries (p4core); the GUI calibration menu is UI only.
- A calibration session puts every other node on the bus in PRE-OPERATIONAL first.
- The puck is put in PRE-OPERATIONAL before the first reads at connect, so leftover PDO streams cannot flood the bus.
- Homing programs the homing method and distinguishes a homing error from homing attained.
- A drive fault during a calibration sweep is named instead of being reported as a stall.
- One pinned requirements set shared with the other puck apps; Python 3.12.
- Debian packaging works across Ubuntu versions; the Linux build no longer aborts on newer gdk-pixbuf.

### Fixed
- Flash and config upload can no longer be re-entered by a second drop or click mid-upload; a failed config upload stops instead of continuing into save and calibration.
- Firmware update from a puck already in its flashloader.
- Startup crash when greying out menu items that no longer exist.
- Encoder calibration current ramp no longer overshoots into the winding.

### Removed
- Host-side cogging compensation and its calibration (the firmware dropped the objects; cogging is now a feed-forward calibration step).
- Quick/Thorough calibration variants, the stand-alone Current Sense Timing and Encoder Lag items, and the velocity/position gain search (now in pucktuner).

## [1.2.1] - 2026-05-11

### Added
- Current-sense (iSense) timing / settling-time calibration.
- Progress bar for the calibration routine.

### Changed
- Encoder zero calibration and calibration error bounds updated; calibration runs faster.
- `system-config.ini` processing holds its pop-ups until the end.
- Window title marks development installs.

### Fixed
- White labels on some Ubuntu versions.
- Command-line calibration.

## [1.2.0] - 2026-05-05

### Added
- Drive test controls for torque, velocity and position modes with a dial, a fault state and homing.
- Headless command-line operation (`--scan`, `--flash`, `--config`, `--calibrate`, `--system-config`).
- Check that a puck is configured after a firmware update and calibrated after configuring.
- Tab-key navigation on Windows.

### Changed
- Default PDO mapping fixed; the ADC monitor no longer appears because of manual SYNC messages.
- `system-config.ini` paths are resolved relative to the app (no absolute paths needed).
- Updated EDS and P4-16 DevKit configuration; Linux and Windows installers updated.

### Fixed
- Idle not always applied on exit, and inconsistent mode switching.
- Scan Pucks not loading the puck ID (and blocking the window from closing) and not resetting nodes.
- Configure button and config/firmware folders not opening.
- Progress bar flicker and focus loss.
- Updating from old firmware versions.
- On/off button and dial backgrounds, and the app icon, on Windows.

## [1.1.5] - 2025-08-14 (not released)

### Added
- Drag-and-drop of firmware and configuration files, auto-detected and applied (invalid extensions are rejected).
- Progress bar for firmware and configuration uploads.
- Update All Pucks (Firmware) menu item.
- Upload System Config: flash and configure a whole robot from a `system-config.ini`.
- Keyboard shortcuts (Scan, Calibrate, ADC monitor, Update All) and tool tips.
- Initial command-line operation.
- Motor thermistor readings in the ADC monitor.
- Debug logging.

### Changed
- Calibration limits current to i_peak, accounts for encoder lag, and runs faster.

### Fixed
- Crash at startup with no puck or CAN adapter, and ADC monitor failures with no puck connected.
- Failed calibrations freezing the ADC monitor and app, and calibration crashes with no active node.
- A "-0 A" current reading.
- ADC monitor switch out of step with the monitor after a firmware update or rescan.
- Looping file-browser pop-up in Update All and looping calibrate hotkey.
- A temporary maximum-velocity limit was added for a firmware bug and removed once the firmware was fixed.

## [1.1.4] - 2025-08-14

### Added
- ADC monitor shows motor current, color coded for continuous versus peak use.
- On/off slide button, background image and support pop-up.
- Automatic gear ratio read from the puck configuration (needs newer firmware).
- User guide (PDF) included in the installers.

### Changed
- Calibration bounds updated; gain-factor test waits for filters after enabling.
- Control uses SDOs again, and all TPDOs are cleared.

## [1.1.3] - 2025-05-16

### Added
- Magnetic encoder test in the calibration procedure, with the option to continue or stop.
- Error pop-up when configuration fails.

### Changed
- Gain-factor calibration accounts for the LDO bias in Beta; gain-factor bounds and iBias smoothing updated.
- Encoder zero calibration uses a 22.5 degree arc with boundaries, and saturates id to prevent stalling.
- The CANopen runner is imported instead of run as a subprocess and reports upload errors in a dialog.
- P4-16 DevKit 24 V configuration updated (velocity filter, firmware 3.3.4).

### Fixed
- CAN port dropping after a firmware update.
- P4-16 calibration.
- Firmware update and installers on Windows; missing files in the Linux and Windows installers.

## [1.1.2] - 2025-01-31

### Added
- Calibrate All restored (missing functions brought over).

### Changed
- Windows builder updated.

## [1.1.1] - 2025-01-22

### Added
- First version in this repository: find pucks on the CAN bus, change CAN IDs, update firmware, configure the CANopen object dictionary, calibrate, and test the puck.
- Linux SocketCAN setup (PEAK USB-CAN) and installation scripts; Windows builder.
- Firmware folder with import instructions.
