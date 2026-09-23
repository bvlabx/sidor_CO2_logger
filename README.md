# SIDOR Logger

A small desktop application for logging and plotting live measuring values
from a SICK SIDOR gas analyzer over its "limited AK protocol" RS-232
interface (SIDOR Operating Instructions 8010939 V2.3, chapter 10).

## Features

- Select the serial port. Baud rate is fixed at 9600/8N1, as specified by
  the instrument's interface.
- Enter the instrument's **AK-ID** — the single character that identifies
  it on the line (set on the instrument itself; several SIDOR units can
  share one RS-232 line, each with its own AK-ID).
- On connect, the app queries the instrument's measuring components
  (AKMP) and lets you pick which one to monitor. This read also doubles
  as a connection sanity check — a wrong AK-ID or bad wiring is caught
  immediately and reported, rather than silently connecting.
- As soon as you connect, the app starts polling automatically: the most
  recent reading and the live historical chart both start updating right
  away — no extra button needed just to see live data.
- The historical chart's time window is independently selectable (last
  1/5/15/30 min, 1/4/24 hours, or all data), and its x-axis shows each
  sample's timestamp as **DDMMYY HH:MM**.
- Logging to CSV is a separate, optional toggle layered on top of the live
  view: pick a file and click **Start logging** whenever you want the
  readings being displayed to also be saved.
- An **Instrument info...** button reads the serial number (AGNR), the
  programmed instrument identification (AKEN), and the coded status bits
  (AFLT) on demand.

This app only *reads* data from the instrument ('A' commands, always
available per the manual) — it never sends the SREM/E/S commands that
activate remote control or change instrument settings.

## Requirements

- Python 3.9+
- Tkinter (bundled with most Python installers; on Debian/Ubuntu:
  `sudo apt install python3-tk`)
- The packages in `requirements.txt`

## Installation

```bash
python -m venv venv
source venv/bin/activate        # on Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Running

```bash
python run.py
```

Or, after `pip install -e .`:

```bash
sidor-logger
```

## Usage

1. Pick the serial port the instrument is connected to, and enter its
   **AK-ID** (a single character — check the instrument's own settings
   menu for the configured value; the manual's example uses `A`).
2. Click **Connect**. The app reads the measuring components from the
   instrument and populates the **Measuring component** dropdown, then
   starts polling automatically — the current-value display and the
   historical chart both start updating without any further action. A
   wrong AK-ID or wiring problem is reported here, before anything else
   happens.
3. Pick which measuring component to monitor from the dropdown at any
   time; switching components takes effect on the next poll.
4. Adjust the **sampling interval** (seconds) at any time.
5. Use the **Show** dropdown above the chart to change how much history is
   plotted.
6. To also save readings to disk: click **Choose...** to pick a CSV file,
   then **Start logging**. This can be turned on/off at any time without
   interrupting the live view. **Stop logging** / **Disconnect** at any
   point; anything already logged stays in the CSV file.
7. Click **Instrument info...** at any time while connected to see the
   instrument's serial number, programmed identification string, and
   coded status bits.

## CSV format

```
timestamp,component,value
2026-09-23T11:16:31.050448,1,93.2
2026-09-23T11:16:32.050842,1,93.0
```

## Project layout

```
sidor-logger/
├── run.py                     # `python run.py` launches the GUI
├── requirements.txt
├── setup.py
├── build_windows.bat          # builds a standalone Windows .exe (run on Windows)
├── sidor_logger/
│   ├── __init__.py
│   ├── ak_protocol.py         # AK protocol framing/parsing (no GUI dependencies)
│   ├── app.py                 # Tkinter GUI, plotting, CSV logging
│   └── assets/
│       └── logo.png           # placeholder logo — replace with your own
└── tools/
    └── fake_sidor.py          # instrument simulator, for testing without hardware
```

## Testing without hardware

You can exercise the whole app without a real instrument using a virtual
serial port pair and the included simulator (`tools/fake_sidor.py`).

### On Windows

Use [com0com](https://sourceforge.net/projects/com0com/), a free virtual
null-modem driver.

1. Install com0com, then open its "Setup Command Prompt" and create a
   linked pair:
   ```
   install PortName=COM10 PortName=COM11
   ```
2. In one terminal, run the simulator on one end:
   ```
   python tools\fake_sidor.py COM11 --ak-id A
   ```
3. In another terminal, run the app and connect to the other end:
   ```
   python run.py
   ```
   Select `COM10` as the serial port, enter `A` as the AK-ID, and click
   **Connect**.

### On Linux/macOS

Use `socat` to create the virtual pair:

```bash
# terminal 1: create a virtual null-modem pair
socat -d -d pty,raw,echo=0,link=/tmp/ttySIDOR_A pty,raw,echo=0,link=/tmp/ttySIDOR_B

# terminal 2: run the simulator on one end
python tools/fake_sidor.py /tmp/ttySIDOR_B --ak-id A

# terminal 3: run the app and connect to the other end (/tmp/ttySIDOR_A)
python run.py
```

The simulator answers AKMP, AKON/AKONx (3 fake components: NO, NO2, SO2),
AFLT, AGNR, AKEN and ASPR, and correctly rejects wrong AK-IDs and
undefined commands the way the real instrument does. Pass
`--no-value-rate 0.3` to have it occasionally reply "no measuring value"
(the `#` case), to exercise that error path in the app.

## Building a standalone Windows .exe

A build script is included: `build_windows.bat`. Run it **on Windows**
(PyInstaller builds for whatever OS it runs on, so this can't be done from
Linux/macOS):

```
python -m venv venv
venv\Scripts\activate
build_windows.bat
```

This installs the dependencies plus PyInstaller, then builds a single-file,
windowless executable at:

```
dist\sidor-logger.exe
```

That one file can be copied to and run on any Windows machine — no Python
installation required there.

## Protocol notes

Implemented from chapter 10 of the SIDOR manual ("Remote control with AK
protocol"):

- Interface #1, 9600 baud, 8 data bits, no parity, 1 stop bit (fixed).
- Every frame: `STX (0x02)` + `[AK-ID]` + `[4-char command]` +
  `[" "+parameter, if any]` + `ETX (0x03)`. The one documented exception is
  `AKONx`, where the component number digit is concatenated directly onto
  the command with no space (`AKON1`, not `AKON 1`).
- Every reply: `STX` + `[AK-ID]` + `[echoed command]` + `" "` +
  `[status character]` + `[" "+params, if any]` + `ETX`. The status
  character is `'0'` normally and increases for certain internal faults
  (gas flow / chopper / step motor / temperature).
- Error replies use the same framing but signal themselves via the echoed
  command and/or a trailing keyword: `"????"` (AK-ID mismatch or undefined
  command), trailing `SMAN` (needs remote control activated first),
  trailing `BS` (instrument busy), trailing `SE` (syntax error). These map
  to `SidorUnknownCommand`, `SidorRemoteNotActivated`, `SidorBusy`, and
  `SidorSyntaxError` in `ak_protocol.py`.

Only the read-only 'A' commands are implemented (`AKON`/`AKONx`, `AKMP`,
`AFLT`, `AGNR`, `AKEN`, `ASPR`) — these are always available per the
manual, with no need to send `SREM` first. The protocol also documents
'E' and 'S' commands for changing settings and running calibration
procedures (need `SREM` first) — `ak_protocol.py` can be extended to
support those if needed, following the same `_send_command` pattern.
