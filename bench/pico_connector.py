# Connector: sends commands to the Pico over USB serial.
# Manual test (sweeps the LED up and down):  python bench/pico_connector.py [--port /dev/...]
import argparse
import os
import time

import serial
from serial.tools import list_ports

RASPBERRY_PI_VID = 0x2E8A  # USB vendor id of the Pico (MicroPython reports it too)


def find_pico_port() -> str:
    """The serial port of the one connected Pico; raises if there is none, or more than one."""
    picos = [port.device for port in list_ports.comports() if port.vid == RASPBERRY_PI_VID]
    if len(picos) == 1:
        return picos[0]
    found = ", ".join(port.device for port in list_ports.comports()) or "none"
    if not picos:
        raise RuntimeError(f"No Pico found on USB. Plug it in, or pass its port with --port. Serial ports: {found}")
    raise RuntimeError(f"Several Picos found ({', '.join(picos)}): pass one with --port.")


def resolve_port(port: str | None) -> str:
    """--port if given, else $PICO_PORT, else the auto-detected Pico."""
    return port or os.environ.get("PICO_PORT") or find_pico_port()


class Pico:
    def __init__(self, port: str, baud: int = 115200):
        self.port = port
        self.ser = serial.Serial(port, baud, timeout=2)
        time.sleep(0.5)
        self.ser.reset_input_buffer()

    def _send(self, command):
        self.ser.write((command + "\n").encode())
        reply = self.ser.readline().decode().strip()
        if not reply:
            raise RuntimeError("No reply from Pico")
        return reply

    def set_led(self, percent):
        return self._send(f"LED {int(percent)}")

    def off(self):
        return self._send("OFF")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sweep the bench LED up and down to test the Pico.")
    parser.add_argument("--port", help="serial port of the Pico (default: $PICO_PORT, else auto-detect)")
    args = parser.parse_args()
    pico = Pico(resolve_port(args.port))
    print(f"Pico on {pico.port}. Ctrl+C stops.")
    try:
        while True:
            vals = (0, 10, 40, 50, 70, 80, 100)
            for p in vals + vals[::-1]:
                print(p, "->", pico.set_led(p))
                time.sleep(0.1)
    except KeyboardInterrupt:
        print("off ->", pico.off())
