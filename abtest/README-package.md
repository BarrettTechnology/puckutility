# P4 firmware A/B test: 4.3.5 against 5.0.5

Start with `ab-testing.md`. It is the whole procedure, written so that a
person or an AI assistant can follow it step by step. Every command in it
was run on the bench while it was written, and the output shown is that
run's output.

| | |
|---|---|
| `ab-testing.md` | The guide: setup, the CLI, calibration, the A/B procedure, the 5.x logger, CANopen changes, analysis, troubleshooting |
| `install.md` | Installing the CLI (Python wheels) and the CAN support (installers) |
| `can-adapters.md` | PEAK PCAN-USB and CANable: drivers, bring-up, which one you have |
| `ab_testing.py`, `testp4.py`, `csvdiff.py` | The A/B runner and the two scripts it imports. Keep them together, next to `csv/` and `firmware/` |
| `csv/` | One configuration CSV per actuator and firmware. The name says which (`fw4.3.5`, `fw5.0.5`) |
| `firmware/` | STM32 application images for 4.3.5 and 5.0.5 |
| `eds/` | The EDS of each firmware |
| `docs/` | The CANopen manual for each firmware (.pdf, and the .odt source), and the 4.3.3 to 5.0.5 release report |
| `wheels/` | `p4core` and `puckutility`, for `install.md` |
| `installers/` | Puck Utility for Linux (.run) and Windows (setup.exe), with the CAN support |
| `SHA256SUMS` | Checksums of every file above: `sha256sum -c SHA256SUMS` |

The release report in `docs/` compares 5.0.5 with 4.3.3, the earlier
shipping firmware. 4.3.5 is 4.3.3 plus the SYNC lockup, bus-off and
heartbeat-producer fixes, the 100 ms watchdog with its NV-save fix, and the
serial-number key. Those are also in 5.0.5. The CANopen differences between
4.3.5 and 5.x are in section 7 of `ab-testing.md`.
