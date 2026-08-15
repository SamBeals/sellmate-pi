"""INA226 register parsing tests. No hardware required.

Live bench samples captured with `sudo i2cget -y 1 0x40 0x01 w` are used
only as endianness fixtures. They are SMBus word-swapped and must be
converted before they represent INA226 register values. They are not
production thresholds.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parents[1] / "app"
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

from ina226 import (
    MANUFACTURER_ID_TI,
    REG_MANUFACTURER_ID,
    REG_SHUNT_VOLTAGE,
    Ina226,
    MappingBus,
    SequenceBus,
    i16_from_be_bytes,
    parse_i2ctransfer_bytes,
    register_i16_from_i2cget_word,
    shunt_voltage_volts,
    signed_i16,
    swap_u16,
    u16_from_be_bytes,
)

# Raw `i2cget ... w` values from the powered-motor bench test.
I2CGET_IDLE = (0xFBFF, 0xFCFF, 0xFDFF, 0xFEFF)
I2CGET_MOTOR = (
    0xF81B,
    0x3B1A,
    0x561F,
    0xE41B,
    0xB01F,
    0x0A1E,
    0xDE1E,
    0x411F,
    0x2E1F,
    0x201F,
    0x1C1E,
    0x861E,
    0x571F,
)
I2CGET_AFTER = 0xFDFF
I2CGET_PSU_OFF = (0xFFFF, 0x0000, 0xFFFF, 0xFEFF)


class TestRegisterParsing(unittest.TestCase):
    def test_big_endian_unsigned(self):
        self.assertEqual(u16_from_be_bytes(0x54, 0x49), MANUFACTURER_ID_TI)
        self.assertEqual(u16_from_be_bytes(0x1B, 0xF8), 0x1BF8)

    def test_signed_two_complement(self):
        self.assertEqual(i16_from_be_bytes(0x00, 0x00), 0)
        self.assertEqual(i16_from_be_bytes(0xFF, 0xFB), -5)
        self.assertEqual(signed_i16(0xFFFB), -5)
        self.assertEqual(signed_i16(0x1BF8), 0x1BF8)

    def test_i2cget_word_is_byte_swapped(self):
        # 0xfbff displayed by i2cget -w is native 0xfffb == -5 LSB, not idle hex.
        self.assertEqual(register_i16_from_i2cget_word(0xFBFF), -5)
        self.assertEqual(register_i16_from_i2cget_word(0xFDFF), -3)
        self.assertEqual(register_i16_from_i2cget_word(0x0000), 0)
        self.assertEqual(register_i16_from_i2cget_word(0xFFFF), -1)
        native_motor = register_i16_from_i2cget_word(0xF81B)
        self.assertEqual(native_motor, 0x1BF8)
        self.assertNotEqual(native_motor, 0xF81B)

    def test_must_not_treat_i2cget_hex_as_native(self):
        displayed = 0xFDFF
        native = register_i16_from_i2cget_word(displayed)
        self.assertNotEqual(native, displayed)
        self.assertEqual(swap_u16(displayed), 0xFFFD)

    def test_shunt_lsb_is_datasheet_2_5_uv(self):
        self.assertAlmostEqual(shunt_voltage_volts(1), 2.5e-6)
        self.assertAlmostEqual(shunt_voltage_volts(7160), 7160 * 2.5e-6)
        self.assertAlmostEqual(shunt_voltage_volts(-5), -5 * 2.5e-6)

    def test_i2ctransfer_parser(self):
        self.assertEqual(parse_i2ctransfer_bytes("0x54 0x49"), [0x54, 0x49])
        self.assertEqual(parse_i2ctransfer_bytes("0x1b 0xf8"), [0x1B, 0xF8])


class TestLiveFixtureSeparation(unittest.TestCase):
    def test_idle_and_motor_do_not_overlap_after_swap(self):
        idle = [register_i16_from_i2cget_word(v) for v in I2CGET_IDLE]
        motor = [register_i16_from_i2cget_word(v) for v in I2CGET_MOTOR]
        after = register_i16_from_i2cget_word(I2CGET_AFTER)
        off = [register_i16_from_i2cget_word(v) for v in I2CGET_PSU_OFF]
        self.assertTrue(all(abs(v) < 20 for v in idle))
        self.assertTrue(all(abs(v) > 1000 for v in motor))
        self.assertLess(abs(after), 20)
        self.assertTrue(all(abs(v) < 20 for v in off))


class TestIna226Device(unittest.TestCase):
    def test_identify_ti_manufacturer(self):
        bus = MappingBus(
            {(0x40, REG_MANUFACTURER_ID): (0x54, 0x49)}
        )
        sensor = Ina226(bus=bus, address=0x40)
        self.assertTrue(sensor.identify())
        self.assertIsNone(sensor.error)

    def test_identify_rejects_wrong_id(self):
        bus = MappingBus({(0x40, REG_MANUFACTURER_ID): (0x00, 0x00)})
        sensor = Ina226(bus=bus, address=0x40)
        self.assertFalse(sensor.identify())
        self.assertIn("unexpected manufacturer", sensor.error or "")

    def test_read_shunt_uses_big_endian_not_i2cget_word(self):
        # Native motor sample 0x1BF8 as MSB,LSB — not the i2cget 0xf81b word.
        bus = SequenceBus([(0x1B, 0xF8)])
        sensor = Ina226(bus=bus, address=0x40)
        volts = sensor.read_shunt_voltage_v()
        self.assertAlmostEqual(volts, shunt_voltage_volts(0x1BF8))
        swapped_wrong = shunt_voltage_volts(signed_i16(0xF81B))
        self.assertNotAlmostEqual(volts, swapped_wrong)


if __name__ == "__main__":
    unittest.main()
