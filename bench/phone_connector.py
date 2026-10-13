# Connector: reads lux from phyphox on the phone over USB (adb forward tcp:8080 tcp:8080).
# Manual test (prints one reading):  python bench/phone_connector.py [--url http://...]
import argparse
import os

import requests

DEFAULT_URL = "http://localhost:8080"
BUFFER = "illum"  # check /config if your experiment uses another name


def resolve_url(url: str | None) -> str:
    """--url if given, else $PHYPHOX_URL, else the adb-forwarded default."""
    return (url or os.environ.get("PHYPHOX_URL") or DEFAULT_URL).rstrip("/")


def start(url: str = DEFAULT_URL):
    requests.get(f"{url}/control", params={"cmd": "start"}, timeout=2)


def read_lux(url: str = DEFAULT_URL):
    data = requests.get(f"{url}/get?{BUFFER}", timeout=2).json()
    values = data["buffer"][BUFFER]["buffer"]
    if not values or values[-1] is None:
        raise RuntimeError("No light reading. Is the phyphox measurement running?")
    return float(values[-1])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Read the light level once from phyphox on the phone.")
    parser.add_argument("--url", help=f"phyphox address (default: $PHYPHOX_URL, else {DEFAULT_URL})")
    args = parser.parse_args()
    url = resolve_url(args.url)
    start(url)
    print(f"{read_lux(url):.1f} lux (from {url})")
