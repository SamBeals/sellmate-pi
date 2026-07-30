"""
Live VL53L1X distance feed for hardware bring-up.

Usage (on the Pi, from the venv):
  python tof_sensor_test.py

Confirms Adafruit VL53L1X on I2C bus 1 (default address 0x29).
Motor MCP23017 typically appears at 0x27 on the same bus.
"""

import time

import adafruit_vl53l1x
from adafruit_extended_bus import ExtendedI2C


# Raspberry Pi I2C bus 1
i2c = ExtendedI2C(1)

# Initialize VL53L1X ToF sensor
sensor = adafruit_vl53l1x.VL53L1X(i2c)

print("Starting VL53L1X distance test...")
print("Press Ctrl+C to stop.")

sensor.start_ranging()

try:
    while True:
        if sensor.data_ready:
            distance_cm = sensor.distance

            if distance_cm is not None:
                print(f"Distance: {distance_cm:.1f} cm")
            else:
                print("Distance unavailable")

            sensor.clear_interrupt()

        time.sleep(0.1)

except KeyboardInterrupt:
    print("\nStopping sensor test...")

finally:
    sensor.stop_ranging()
