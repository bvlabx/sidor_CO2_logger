#!/usr/bin/env python3
"""
fake_sidor.py — simple SIDOR (AK protocol) instrument simulator, for testing
sidor_logger without real hardware.

Usage:
    1. Create a virtual serial port pair.
       - Linux/macOS, with socat:
           socat -d -d pty,raw,echo=0,link=/tmp/ttySIDOR_A pty,raw,echo=0,link=/tmp/ttySIDOR_B
       - Windows, with com0com:
           install PortName=COM10 PortName=COM11
    2. Run this script pointed at one end:
           python tools/fake_sidor.py /tmp/ttySIDOR_B --ak-id A
           python tools\\fake_sidor.py COM11 --ak-id A
    3. Point the SIDOR Logger app (or ak_protocol.py) at the other end,
       with the same AK-ID.

Implements just enough of the AK protocol (chapter 10 of the SIDOR manual)
to exercise the app: AKON / AKONx (measuring values, 3 fake components),
AKMP (component names + ranges), AFLT (status bits), AGNR (serial number),
AKEN (instrument identification), ASPR (menu language). Any other command
gets the documented "undefined command" reply (????).

Pass --wrong-id-rate or --no-value-rate to occasionally exercise the app's
error handling (AK-ID mismatch and "no measuring value" replies).
"""

import argparse
import random

import serial

STX = b"\x02"
ETX = b"\x03"

FAKE_COMPONENTS = {
    1: ("NO", "1000"),
    2: ("NO2", "500"),
    3: ("SO2", "2000"),
}


def build_reply(ak_id: str, command_echo: str, status: str, params: str = "") -> bytes:
    text = ak_id + command_echo + " " + status
    if params:
        text += " " + params
    return STX + text.encode("ascii") + ETX


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("port", nargs="?", default="/tmp/ttySIDOR_B")
    parser.add_argument("--ak-id", default="A", help="Single-character AK-ID this fake instrument answers to (default: A)")
    parser.add_argument("--no-value-rate", type=float, default=0.0,
                         help="Fraction of AKON replies (0-1) that report 'no measuring value' instead of a number")
    args = parser.parse_args()

    ak_id = args.ak_id
    if len(ak_id) != 1:
        parser.error("--ak-id must be exactly one character")

    ser = serial.Serial(args.port, baudrate=9600, timeout=1)
    print(f"Fake SIDOR listening on {args.port} (AK-ID={ak_id!r})")

    buf = bytearray()
    started = False
    while True:
        b = ser.read(1)
        if not b:
            continue
        if not started:
            if b == STX:
                started = True
                buf = bytearray(b)
            continue
        buf += b
        if b != ETX:
            continue

        started = False
        frame = bytes(buf)
        buf = bytearray()

        text = frame[1:-1].decode("ascii", errors="replace")
        if not text:
            continue
        recv_id = text[0]
        rest = text[1:]

        if recv_id != ak_id:
            # AK-ID mismatch -> "????" reply, status '0'
            reply = build_reply(recv_id, "????", "0")
            ser.write(reply)
            print(f"Ignored (wrong AK-ID {recv_id!r}, expected {ak_id!r})")
            continue

        # Split "command[params]" — command is 4 chars normally, but AKONx
        # appends the digit directly with no space (see ak_protocol.py).
        command = rest[:4]
        param_str = rest[4:].strip()

        print("Received:", repr(rest))

        if command == "AKMP":
            # AKMP or "AKMP Kx" — param_str is either empty or "Kx"
            if param_str.startswith("K") and len(param_str) > 1:
                try:
                    x = int(param_str[1:])
                except ValueError:
                    x = None
                if x in FAKE_COMPONENTS:
                    name, rng = FAKE_COMPONENTS[x]
                    ser.write(build_reply(ak_id, "AKMP", "0", f"{name} {rng}"))
                else:
                    ser.write(build_reply(ak_id, "AKMP", "0", "SE"))
            else:
                # all components, flattened name/range pairs
                parts = []
                for num in sorted(FAKE_COMPONENTS):
                    name, rng = FAKE_COMPONENTS[num]
                    parts += [name, rng]
                ser.write(build_reply(ak_id, "AKMP", "0", " ".join(parts)))

        elif command.startswith("AKON"):
            # AKON or AKONx (x appended directly onto "AKON", no space)
            suffix = rest[4:]  # e.g. "1", "2", or ""
            if random.random() < args.no_value_rate:
                ser.write(build_reply(ak_id, rest, "0", "#"))
                print("Sent: no measuring value (#)")
                continue

            if suffix:
                try:
                    x = int(suffix)
                except ValueError:
                    x = None
                if x in FAKE_COMPONENTS:
                    val = round(random.uniform(0, 100), 1)
                    ser.write(build_reply(ak_id, rest, "0", f"{x} {val}"))
                    print(f"Sent: component {x} = {val}")
                else:
                    ser.write(build_reply(ak_id, rest, "0", "SE"))
            else:
                parts = []
                for num in sorted(FAKE_COMPONENTS):
                    val = round(random.uniform(0, 100), 1)
                    parts += [str(num), str(val)]
                ser.write(build_reply(ak_id, "AKON", "0", " ".join(parts)))
                print("Sent: all components ->", " ".join(parts))

        elif command == "AFLT":
            ser.write(build_reply(ak_id, "AFLT", "0", "00000000 00000000 00000000 00000000 00000000 00000000 00000000 00000000"))

        elif command == "AGNR":
            ser.write(build_reply(ak_id, "AGNR", "0", "SN-12345"))

        elif command == "AKEN":
            ser.write(build_reply(ak_id, "AKEN", "0", "FAKE SIDOR SIMULATOR"))

        elif command == "ASPR":
            ser.write(build_reply(ak_id, "ASPR", "0", "E"))

        else:
            # undefined command
            ser.write(build_reply(ak_id, "????", "0"))
            print("Sent: undefined command reply")


if __name__ == "__main__":
    main()
