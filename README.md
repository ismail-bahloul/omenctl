# omenctl

Fans and power modes for the **HP OMEN Laptop 15-en1xxx** (Ryzen 7 5800H,
RTX 3070; verified on DMI board `88D2`), as one small tool. It replaces
`nbfc_service` and the `power-profile*` scripts.

> **Hardware-specific, and it writes embedded-controller (EC) registers.**
> The EC offsets, the fan setpoints (`0x2C`/`0x2D`), the temperature bytes and
> the battery registers were measured on this model only. On another OMEN the
> same bytes can mean something else. `omenctl` refuses to run unless the DMI
> matches a verified board, and prints what it found. Do not bypass that check.
>
> The fan setpoints are a latch: a dead writer leaves them frozen. Every stop
> path therefore writes `255`, which hands the fans back to the EC's own
> (louder, steeper) curve. A failed EC read puts both fans at 100 %. There is
> no warranty; use at your own risk.

## Requirements

- root, systemd, Python 3
- `ec_sys` with `write_support=1` (the wrapper modprobes it)
- `ryzenadj` (CPU power limits)
- `ryzen_smu` (SMU drift watcher; optional for the fan daemon)
- `nvidia-smi` (dGPU clock lock; optional, skipped when the dGPU is on
  `vfio-pci`)

## Install

```sh
sudo ./install.sh
```

This installs:

- `/usr/local/bin/omenctl`
- `/usr/local/bin/omenctl-run.sh`
- `/etc/systemd/system/omenctl.service`

and enables/starts the fan daemon. The fans move from the EC's own curve to the
tuned curve in `omenctl.py`.

The power-profile units (`omenctl-profile.service`, `omenctl-profile.timer`,
`omenctl-watch.service`) are shipped by the accompanying dotfiles; this repo
provides the governor binary and the fan daemon unit they call.

## Usage

```sh
sudo omenctl status -w                                  # live: fans, power, battery
sudo omenctl profile auto|quiet|eco|perf                # power mode
sudo omenctl boost on|off                               # switch auto/quiet
sudo omenctl fan cpu|gpu|all 0-100|auto                 # manual fan speed
sudo omenctl tui                                        # live TUI with faders
sudo omenctl start|stop|restart|enable|disable          # the fan daemon
```

Modes (the chosen one is stored in `/var/lib/omenctl/mode` and survives
reboots and sleeps):

- `auto`: the AC or battery envelope, following the power source;
- `quiet`: auto with CPU boost off;
- `eco`: the battery envelope kept on AC;
- `perf`: everything unlocked.

## Safety contract

- `255` on every exit path: clean stop, crash (via the supervisor wrapper),
  watchdog kill;
- `WatchdogSec=10`, pinged every tick;
- three failed EC reads put both fans at 100 %;
- five failed EC writes exit, and the unit's stop path resets to `255`;
- manual speeds are dropped at 80 °C (65 °C below 12 %), and 100 °C forces
  100 % on both fans;
- `Restart=no`: a failed governor falls back to the EC's curve and stays there.

`omenctl stop` (or `systemctl stop omenctl`) hands the fans back to the EC.

## Tests

```sh
python3 test_failpaths.py
```

No hardware and no root: the failure paths the safety contract depends on are
simulated.

## License

MIT. See `LICENSE`.

The measurements behind the curve, the EC map and the thermal A/B live in the
author's research notes; the comments in `omenctl.py` reference them.
