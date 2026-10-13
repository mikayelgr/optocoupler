# MicroPython firmware for the Raspberry Pi Pico.
# LED on GP15, brightness set by PWM.
# Copy it to the board as main.py so it runs on boot (see docs/bench-setup.md).
# Commands over USB serial (one per line):
#   LED <0-100>   set brightness in percent
#   OFF           turn the LED off
# Replies: "OK <percent>" or "ERR <reason>"
import sys
from machine import Pin, PWM

LED_PIN = 15  # GP15 (physical pin 20)
PWM_FREQ = 1000  # 1 kHz, no visible flicker

pwm = PWM(Pin(LED_PIN))
pwm.freq(PWM_FREQ)
pwm.duty_u16(0)


def set_percent(p):
    pwm.duty_u16(p * 65535 // 100)


while True:
    line = sys.stdin.readline()
    parts = line.strip().split()
    if not parts:
        continue
    cmd = parts[0].upper()

    if cmd == "LED" and len(parts) == 2:
        try:
            p = int(parts[1])
        except ValueError:
            print("ERR value must be an integer")
            continue
        if 0 <= p <= 100:
            set_percent(p)
            print("OK", p)
        else:
            print("ERR value must be 0-100")
    elif cmd == "OFF":
        set_percent(0)
        print("OK 0")
    else:
        print("ERR unknown command")
