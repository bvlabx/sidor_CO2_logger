"""
ak_protocol.py — Serial communication layer implementing the SICK SIDOR
"limited AK protocol" (SIDOR Operating Instructions, 8010939 V2.3, chapter 10).

Protocol summary (from the manual):
  - Interface #1, 9600 baud, 8 data bits, no parity, 1 stop bit (fixed).
  - Every command frame:
        STX (0x02) + [AK-ID] + [4-char command] + [" "+parameter, if any] + ETX (0x03)
    [AK-ID] is a single character identifying the instrument (set on the
    instrument itself), used so several SIDOR units can share one line.
  - Every reply frame:
        STX + [AK-ID] + [echoed command] + " " + [status char] + [" "+params, if any] + ETX
    The status character is normally '0'; it increases by 1 for certain
    internal faults (gas flow / chopper / step motor / temperature).
  - Error replies (still framed the same way) signal themselves via the
    echoed command and/or a trailing keyword instead of normal parameters:
      - echoed command "????"      -> AK-ID mismatch, or command not defined
      - trailing "SMAN"            -> command needs remote control active,
                                       but SREM was never sent (still in
                                       manual/local mode)
      - trailing "BS"              -> instrument busy, can't run this now
      - trailing "SE"              -> command didn't meet the syntax rules
  - Three command families: 'A' commands (read data, always available),
    'E' commands (change settings, need SREM/remote-control-activated
    first), 'S' commands (start a procedure, also need SREM first).

This app only needs to *read* data, so only 'A' commands are used — no
SREM/remote-control activation is required for anything implemented here.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import serial
from serial.tools import list_ports


STX = b"\x02"
ETX = b"\x03"

SUPPORTED_BAUD_RATES = [9600]  # the manual documents this as the fixed/standard rate


class SidorProtocolError(Exception):
    """Base class for all AK-protocol-level errors."""


class SidorTimeoutError(SidorProtocolError):
    """No complete STX...ETX reply arrived within the timeout."""


class SidorRemoteNotActivated(SidorProtocolError):
    """Reply carried the 'SMAN' suffix: command needs SREM sent first."""


class SidorBusy(SidorProtocolError):
    """Reply carried the 'BS' suffix: instrument can't run this command now."""


class SidorSyntaxError(SidorProtocolError):
    """Reply carried the 'SE' suffix: command didn't meet the syntax rules."""


class SidorUnknownCommand(SidorProtocolError):
    """Reply echoed '????': AK-ID mismatch, or the command isn't defined."""


class SidorNoMeasuringValue(SidorProtocolError):
    """AKON replied '#': no measuring value currently available."""


def _validate_ak_id(ak_id: str) -> str:
    if not isinstance(ak_id, str) or len(ak_id) != 1:
        raise ValueError(f"AK-ID must be exactly one character, got {ak_id!r}.")
    if not (0x20 <= ord(ak_id) <= 0x7E):
        raise ValueError(f"AK-ID must be a printable ASCII character, got {ak_id!r}.")
    return ak_id


def build_frame(ak_id: str, command_text: str) -> bytes:
    """Build a complete STX...ETX command frame. `command_text` is the
    4-character command optionally followed by " "+parameter(s) — except
    for AKONx, where the manual shows the parameter digit concatenated
    directly onto the command with no space (e.g. "AKON1"); callers build
    that string themselves and pass it straight through here."""
    ak_id = _validate_ak_id(ak_id)
    return STX + (ak_id + command_text).encode("ascii") + ETX


@dataclass
class AKReply:
    ak_id: str
    command_echo: str
    status: str
    params: str
    raw: str


def parse_frame(frame: bytes) -> AKReply:
    """Parse a raw STX...ETX reply frame into an AKReply, raising the
    appropriate SidorProtocolError subclass for the documented error
    variants (see module docstring)."""
    if not (frame.startswith(STX) and frame.endswith(ETX)):
        raise SidorProtocolError(f"Malformed frame (missing STX/ETX): {frame!r}")

    text = frame[1:-1].decode("ascii", errors="replace")
    if len(text) < 1:
        raise SidorProtocolError(f"Empty reply frame: {frame!r}")

    ak_id = text[0]
    rest = text[1:]

    # The echoed command is everything up to the first space; this stays
    # correct both for fixed 4-character commands (SREM, AKMP, AFLT, ...)
    # and for AKON's parameter-appended form (AKON1, AKON2, ...).
    space_idx = rest.find(" ")
    if space_idx == -1:
        raise SidorProtocolError(f"Malformed reply (no status field): {frame!r}")
    command_echo = rest[:space_idx]
    after = rest[space_idx + 1:]

    if not after:
        raise SidorProtocolError(f"Malformed reply (missing status character): {frame!r}")
    status = after[0]
    tail = after[1:]
    if tail.startswith(" "):
        tail = tail[1:]

    reply = AKReply(ak_id=ak_id, command_echo=command_echo, status=status, params=tail, raw=text)

    if command_echo == "????":
        raise SidorUnknownCommand(
            f"AK-ID mismatch or undefined command (status {status}): {frame!r}"
        )
    if tail == "SMAN":
        raise SidorRemoteNotActivated(
            f"Command {command_echo!r} needs remote control active (send SREM first)."
        )
    if tail == "BS":
        raise SidorBusy(f"Instrument busy, cannot run {command_echo!r} right now.")
    if tail == "SE":
        raise SidorSyntaxError(f"Command {command_echo!r} did not meet the syntax rules.")

    return reply


def list_available_ports() -> list[str]:
    """Return device names of currently available serial ports."""
    return [p.device for p in list_ports.comports()]


@dataclass
class MeasuringComponent:
    number: int
    name: str
    range_max: str


class SidorAnalyzer:
    """Thin wrapper around a serial connection to a SIDOR instrument using
    the limited AK protocol."""

    def __init__(self, port: str, ak_id: str = "A", timeout: float = 2.0):
        self.port = port
        self.ak_id = _validate_ak_id(ak_id)
        self.timeout = timeout
        self._ser: Optional[serial.Serial] = None

    def connect(self) -> None:
        self._ser = serial.Serial(
            port=self.port,
            baudrate=9600,          # fixed by the instrument's interface spec
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.2,             # short per-read slice; overall deadline enforced below
        )
        time.sleep(0.2)
        self._ser.reset_input_buffer()

    def disconnect(self) -> None:
        if self._ser is not None and self._ser.is_open:
            self._ser.close()
        self._ser = None

    @property
    def is_connected(self) -> bool:
        return self._ser is not None and self._ser.is_open

    def _read_frame(self) -> bytes:
        """Read one STX...ETX frame, ignoring any stray bytes before STX,
        enforcing self.timeout as an overall deadline (not per-byte)."""
        deadline = time.monotonic() + self.timeout
        buf = bytearray()
        started = False
        while time.monotonic() < deadline:
            b = self._ser.read(1)
            if not b:
                continue
            if not started:
                if b == STX:
                    started = True
                    buf += b
                continue
            buf += b
            if b == ETX:
                return bytes(buf)
        raise SidorTimeoutError(
            f"No complete reply from instrument on {self.port} within {self.timeout}s."
        )

    def _send_command(self, command_text: str) -> AKReply:
        if not self.is_connected:
            raise SidorProtocolError("Not connected to instrument.")
        frame = build_frame(self.ak_id, command_text)
        self._ser.reset_input_buffer()
        self._ser.write(frame)
        self._ser.flush()
        raw = self._read_frame()
        return parse_frame(raw)

    # ------------------------------------------------------------------
    # 'A' commands — reading data, always available (no SREM needed)
    # ------------------------------------------------------------------
    def read_measuring_values(self, component: int = 0) -> dict[int, Optional[float]]:
        """AKON / AKONx — current measuring value(s). component=0 (or
        omitted) reads all components. Returns {component_number: value},
        value is None if the instrument reports no measuring value ('#')
        for that slot."""
        command = "AKON" if component == 0 else f"AKON{component}"
        reply = self._send_command(command)

        if reply.params.strip() == "#":
            raise SidorNoMeasuringValue("Instrument reports no measuring value available.")

        tokens = reply.params.split()
        values: dict[int, Optional[float]] = {}
        it = iter(tokens)
        for tok_x in it:
            try:
                x = int(tok_x)
            except ValueError:
                continue  # unexpected token; skip rather than crash
            try:
                tok_mv = next(it)
            except StopIteration:
                break
            try:
                values[x] = float(tok_mv)
            except ValueError:
                values[x] = None  # e.g. "-.-" or other non-numeric placeholder
        if not values:
            raise SidorProtocolError(f"Could not parse AKON reply: {reply.raw!r}")
        return values

    def read_measuring_components(self, component: int = 0) -> dict[int, MeasuringComponent]:
        """AKMP / AKMP Kx — component name(s) and physical measuring
        range(s). component=0 (or omitted) reads all components."""
        command = "AKMP" if component == 0 else f"AKMP K{component}"
        reply = self._send_command(command)
        tokens = reply.params.split()
        result: dict[int, MeasuringComponent] = {}
        # Reply shape for a single component: [x] [y] (name, range end value).
        # For "all components" the manual doesn't give a multi-component
        # example the way AKON does, so we handle both a flat pair and,
        # defensively, a repeating (name, range) sequence.
        if component != 0:
            if len(tokens) >= 2:
                result[component] = MeasuringComponent(number=component, name=tokens[0], range_max=tokens[1])
        else:
            it = iter(tokens)
            idx = 1
            for name in it:
                try:
                    range_max = next(it)
                except StopIteration:
                    break
                result[idx] = MeasuringComponent(number=idx, name=name, range_max=range_max)
                idx += 1
        return result

    def read_instrument_status(self) -> str:
        """AFLT — coded status message: 8 space-separated 8-bit blocks."""
        reply = self._send_command("AFLT")
        return reply.params

    def read_serial_number(self) -> str:
        """AGNR — instrument serial number."""
        reply = self._send_command("AGNR")
        return reply.params

    def read_instrument_identification(self) -> str:
        """AKEN — the free-text instrument identification string (up to
        40 ASCII characters) programmed into the instrument."""
        reply = self._send_command("AKEN")
        return reply.params

    def read_menu_language(self) -> str:
        """ASPR — single character identifying the selected menu language."""
        reply = self._send_command("ASPR")
        return reply.params
