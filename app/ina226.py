"""
INA226 shunt-current sensor (Texas Instruments).

Hardware-specific I²C/register logic lives here so commissioning and vend
business logic never parse raw bus values.

Register format is big-endian. Do not treat `i2cget ... w` SMBus word output
as a native INA226 register value — that tooling byte-swaps 16-bit reads.
Use `register_i16_from_i2cget_word()` only when interpreting those terminal
fixtures.

Datasheet (INA226):
  - Shunt-voltage register 0x01 is signed 16-bit, LSB = 2.50 µV
  - Manufacturer ID register 0xFE = 0x5449 ("TI")
  - VBUS and ALARM are unused by this driver
"""

from __future__ import annotations

import subprocess
from typing import List, Optional, Protocol, Sequence, Tuple

# Register map (pointer addresses)
REG_CONFIG = 0x00
REG_SHUNT_VOLTAGE = 0x01
REG_BUS_VOLTAGE = 0x02
REG_POWER = 0x03
REG_CURRENT = 0x04
REG_CALIBRATION = 0x05
REG_MASK_ENABLE = 0x06
REG_ALERT_LIMIT = 0x07
REG_MANUFACTURER_ID = 0xFE
REG_DIE_ID = 0xFF

MANUFACTURER_ID_TI = 0x5449
SHUNT_LSB_VOLTS = 2.5e-6


class Ina226Error(RuntimeError):
    """Raised when the INA226 cannot be identified or read."""


class RegisterBus(Protocol):
    """Reads a 16-bit big-endian device register as two bytes (MSB, LSB)."""

    def read_register_bytes(self, address: int, register: int) -> Tuple[int, int]:
        ...


def u16_from_be_bytes(msb: int, lsb: int) -> int:
    return ((int(msb) & 0xFF) << 8) | (int(lsb) & 0xFF)


def i16_from_be_bytes(msb: int, lsb: int) -> int:
    raw = u16_from_be_bytes(msb, lsb)
    if raw & 0x8000:
        return raw - 0x10000
    return raw


def signed_i16(raw_u16: int) -> int:
    value = int(raw_u16) & 0xFFFF
    if value & 0x8000:
        return value - 0x10000
    return value


def swap_u16(value: int) -> int:
    raw = int(value) & 0xFFFF
    return ((raw & 0xFF) << 8) | ((raw >> 8) & 0xFF)


def register_i16_from_i2cget_word(displayed: int) -> int:
    """
    Convert `sudo i2cget -y <bus> <addr> <reg> w` output to the signed
    INA226 register value.

    SMBus word reads present the 16-bit register bytes swapped relative to
    the INA226 big-endian wire format.
    """
    return signed_i16(swap_u16(displayed))


def shunt_voltage_volts(raw_i16: int) -> float:
    """Convert signed shunt-voltage register counts to volts."""
    return float(raw_i16) * SHUNT_LSB_VOLTS


def current_amps(shunt_voltage_v: float, shunt_ohms: float) -> float:
    """
    Ohm's law conversion. Calibration is explicit: callers must supply the
    physical shunt resistance. Do not infer ohms from a single bench test.
    """
    ohms = float(shunt_ohms)
    if ohms <= 0:
        raise ValueError("shunt_ohms must be > 0")
    return float(shunt_voltage_v) / ohms


def parse_i2ctransfer_bytes(output: str) -> List[int]:
    tokens: List[int] = []
    for tok in (output or "").replace(",", " ").split():
        cleaned = tok.strip().rstrip(",")
        if cleaned.lower().startswith("0x"):
            tokens.append(int(cleaned, 16) & 0xFF)
    return tokens


class I2cTransferBus:
    """
    Production I²C reader using `i2ctransfer` so bytes stay in INA226
    big-endian order (write pointer, read 2 bytes).
    """

    def __init__(self, bus: int = 1, timeout: float = 2.0) -> None:
        self.bus = int(bus)
        self.timeout = timeout

    def read_register_bytes(self, address: int, register: int) -> Tuple[int, int]:
        addr = int(address) & 0x7F
        reg = int(register) & 0xFF
        command = [
            "i2ctransfer",
            "-y",
            str(self.bus),
            f"w1@{addr:#x}",
            f"{reg:#x}",
            "r2",
        ]
        try:
            process = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise Ina226Error("i2ctransfer is not installed") from exc
        except subprocess.TimeoutExpired as exc:
            raise Ina226Error("i2ctransfer timed out") from exc

        output = ((process.stdout or "") + (process.stderr or "")).strip()
        if process.returncode != 0:
            raise Ina226Error(output or "i2ctransfer failed")

        data = parse_i2ctransfer_bytes(output)
        if len(data) < 2:
            raise Ina226Error(f"expected 2 register bytes, got: {output!r}")
        return data[0], data[1]


class SmbusBlockBus:
    """Optional smbus2 backend; still treats the two bytes as big-endian."""

    def __init__(self, bus: int = 1) -> None:
        self.bus = int(bus)
        self._smbus = None

    def read_register_bytes(self, address: int, register: int) -> Tuple[int, int]:
        try:
            import smbus2
        except Exception as exc:  # noqa: BLE001
            raise Ina226Error("smbus2 is not available") from exc

        if self._smbus is None:
            self._smbus = smbus2.SMBus(self.bus)
        data = self._smbus.read_i2c_block_data(int(address), int(register), 2)
        if not data or len(data) < 2:
            raise Ina226Error("smbus2 returned fewer than 2 bytes")
        return int(data[0]) & 0xFF, int(data[1]) & 0xFF


class SequenceBus:
    """Test double: returns queued (msb, lsb) pairs, ignoring address/register."""

    def __init__(self, frames: Sequence[Tuple[int, int]]) -> None:
        self._frames = list(frames)
        self.reads: List[Tuple[int, int]] = []

    def read_register_bytes(self, address: int, register: int) -> Tuple[int, int]:
        self.reads.append((int(address), int(register)))
        if not self._frames:
            raise Ina226Error("no queued INA226 frames")
        return self._frames.pop(0)


class MappingBus:
    """Test double: map (address, register) -> (msb, lsb)."""

    def __init__(self, mapping: dict) -> None:
        self.mapping = dict(mapping)

    def read_register_bytes(self, address: int, register: int) -> Tuple[int, int]:
        key = (int(address), int(register))
        if key not in self.mapping:
            raise Ina226Error(
                f"no mapping for address={address:#x} register={register:#x}"
            )
        msb, lsb = self.mapping[key]
        return int(msb) & 0xFF, int(lsb) & 0xFF


def open_default_bus(bus: int = 1) -> RegisterBus:
    """Prefer i2ctransfer (explicit byte order); fall back to smbus2."""
    try:
        probe = I2cTransferBus(bus=bus)
        # Construction is cheap; actual availability is proven on first read.
        return probe
    except Exception:
        return SmbusBlockBus(bus=bus)


class Ina226:
    """Thin INA226 accessor. Does not configure ALARM or VBUS monitoring."""

    def __init__(
        self,
        *,
        bus: RegisterBus,
        address: int = 0x40,
        shunt_ohms: Optional[float] = None,
    ) -> None:
        self.bus = bus
        self.address = int(address) & 0x7F
        self.shunt_ohms = float(shunt_ohms) if shunt_ohms else None
        self.error: Optional[str] = None

    def read_u16(self, register: int) -> int:
        msb, lsb = self.bus.read_register_bytes(self.address, register)
        return u16_from_be_bytes(msb, lsb)

    def read_i16(self, register: int) -> int:
        msb, lsb = self.bus.read_register_bytes(self.address, register)
        return i16_from_be_bytes(msb, lsb)

    def manufacturer_id(self) -> int:
        return self.read_u16(REG_MANUFACTURER_ID)

    def identify(self) -> bool:
        try:
            mid = self.manufacturer_id()
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
            return False
        if mid != MANUFACTURER_ID_TI:
            self.error = f"unexpected manufacturer id {mid:#06x}"
            return False
        self.error = None
        return True

    def read_shunt_voltage_v(self) -> float:
        return shunt_voltage_volts(self.read_i16(REG_SHUNT_VOLTAGE))

    def read_current_a(self) -> Optional[float]:
        if self.shunt_ohms is None:
            return None
        return current_amps(self.read_shunt_voltage_v(), self.shunt_ohms)
