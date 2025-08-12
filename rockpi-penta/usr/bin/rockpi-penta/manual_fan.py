#!/usr/bin/env python3
import os
import os.path
import sys
import time
import threading

try:
    import gpiod  # only needed for software PWM mode
except Exception:
    gpiod = None


class Pwm:
    def __init__(self, chip):
        # chip can be "0" / 0 or "pwmchip0"
        try:
            int(chip)
            chip = f"pwmchip{chip}"
        except ValueError:
            pass

        self.base = f"/sys/class/pwm/{chip}"
        self.path = f"{self.base}/pwm0"
        self.period_value = None

        # Export channel 0 if needed
        try:
            if not os.path.isdir(self.path):
                with open(f"{self.base}/export", "w") as f:
                    f.write("0")
        except OSError:
            # Already exported or lacks permission; continue
            pass

        # Wait briefly for sysfs to appear
        for _ in range(50):
            if os.path.isdir(self.path):
                break
            time.sleep(0.01)
        if not os.path.isdir(self.path):
            raise RuntimeError(f"PWM path not found: {self.path}")

    def period(self, ns: int):
        self.period_value = ns
        with open(os.path.join(self.path, "period"), "w") as f:
            f.write(str(ns))

    def period_us(self, us: int):
        self.period(us * 1000)

    def enable(self, enable: bool):
        with open(os.path.join(self.path, "enable"), "w") as f:
            f.write("1" if enable else "0")

    def write(self, duty: float):
        if self.period_value is None:
            raise RuntimeError("PWM period not set")
        # Sysfs requires duty_cycle < period; cap slightly below 100%
        duty = max(0.0, min(duty, 0.999))
        with open(os.path.join(self.path, "duty_cycle"), "w") as f:
            f.write(str(int(self.period_value * duty)))


class GpioPWM:
    def __init__(self, period_s: float):
        if gpiod is None:
            raise RuntimeError("python3-libgpiod is required for software PWM mode")

        fan_chip = os.environ.get("FAN_CHIP")
        fan_line = os.environ.get("FAN_LINE")
        if fan_chip is None or fan_line is None:
            raise RuntimeError("FAN_CHIP and FAN_LINE environment variables are required for software PWM mode")

        self.period_s = period_s
        self.high_time = period_s / 2
        self.low_time = period_s / 2
        self._stop = False

        self.line = gpiod.Chip(fan_chip).get_line(int(fan_line))
        self.line.request(consumer="fan", type=gpiod.LINE_REQ_DIR_OUT)

        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        # Simple square-wave PWM loop
        while not self._stop:
            self.line.set_value(1)
            time.sleep(self.high_time)
            self.line.set_value(0)
            time.sleep(self.low_time)

    def write(self, duty: float):
        duty = max(0.0, min(duty, 1.0))
        self.high_time = self.period_s * duty
        self.low_time = self.period_s - self.high_time

    def stop(self):
        self._stop = True
        try:
            self.line.set_value(0)
        except Exception:
            pass


def get_controller():
    hw = os.environ.get("HARDWARE_PWM", "0") == "1"
    if hw:
        chip = os.environ.get("PWMCHIP", "0")
        ctrl = Pwm(chip)
        # Match your existing program: 40 µs period => 25 kHz
        ctrl.period_us(40)
        ctrl.enable(True)
        print("Mode: Hardware PWM (/sys/class/pwm), 25 kHz")
        return ctrl, "hardware"
    else:
        # Match your existing program: 0.025 s period => 40 Hz
        ctrl = GpioPWM(period_s=0.025)
        print("Mode: Software PWM (gpiod), 40 Hz")
        return ctrl, "software"


def parse_duty(s: str) -> float:
    s = s.strip().lower().rstrip("%")
    if not s:
        raise ValueError("empty input")
    val = float(s)
    # If user types 0-100, treat as percent; else assume 0.0-1.0
    if val > 1.0:
        return val / 100.0
    return val


def main():
    try:
        ctrl, mode = get_controller()
    except Exception as e:
        print(f"Init error: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        while True:
            raw = input("Enter duty cycle (0-100 for %, or 0.0-1.0). 'q' to quit: ").strip()
            if raw.lower() in ("q", "quit", "exit"):
                break
            try:
                duty = parse_duty(raw)
            except Exception:
                print("Invalid number. Try e.g. 35 or 0.35 or 35%.")
                continue

            # Bounds and caps
            if mode == "hardware" and duty >= 1.0:
                print("Capped at 99.9% for hardware PWM.")
                duty = 0.999
            duty = max(0.0, min(duty, 1.0))

            try:
                ctrl.write(duty)
                pct = round(duty * 100.0, 1)
                print(f"Set duty: {pct}%")
            except Exception as e:
                print(f"Write error: {e}")

    except KeyboardInterrupt:
        pass
    finally:
        # Best-effort cleanup
        try:
            if isinstance(ctrl, Pwm):
                ctrl.write(0.0)
                ctrl.enable(False)
            else:
                ctrl.write(0.0)
                ctrl.stop()
        except Exception:
            pass
        print("\nExiting.")


if __name__ == "__main__":
    main()
