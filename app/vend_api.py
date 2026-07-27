# vend_api.py
#
# SellMate Vend API
#
# Current behavior:
# - /vend_mask_verified performs the normal vend without beam checking.
# - /vend_mask_verified_test contains the beam-verified test flow.
# - New beam sensors use HIGH / 1 for BROKEN and LOW / 0 for CLEAR.
# - Beam GPIO initialization failure does not crash the API.
# - LED/light-manager initialization failure remains non-fatal.
# - Startup I2C initialization failure does not prevent the API from starting.
# - Hardware availability and initialization errors are exposed through /health.
# - I2C failures are reported only when a vending endpoint actually needs I2C.
# - Output registers are cleared as safely as possible after vending.

import asyncio
import os
import re
import subprocess
import traceback
from typing import Optional, List, Dict, Any, Tuple

from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel, Field, model_validator


app = FastAPI(
    title="SellMate Vend API",
    version="1.2.0",
)


# =========================================================
# Configuration
# =========================================================

I2C_BUS = os.getenv("I2C_BUS", "1")
I2C_ADDR = os.getenv("I2C_ADDR", "0x27")

REG_PULSE_A = os.getenv("REG_PULSE_A", "0x14")
REG_PULSE_B = os.getenv("REG_PULSE_B", "0x15")
REG_ENABLE = os.getenv("REG_ENABLE", "0x00")

BEAM_GPIO = int(os.getenv("BEAM_GPIO", "17"))

# New infrared sensors:
#   HIGH / 1 = beam broken
#   LOW  / 0 = beam clear
#
# This can still be overridden through the environment later.
BEAM_BROKEN_STATE = int(os.getenv("BEAM_BROKEN_STATE", "1"))
BEAM_CLEAR_STATE = 0 if BEAM_BROKEN_STATE == 1 else 1

API_KEY = os.getenv("VEND_API_KEY", "CHANGE_ME")

SLOT_RE = re.compile(r"^S\d{2}$")

SLOT_TO_MASK: Dict[str, Dict[str, Any]] = {
    "S01": {"bank": "A", "mask": 1},
    "S02": {"bank": "A", "mask": 2},
    "S03": {"bank": "A", "mask": 4},
    "S04": {"bank": "A", "mask": 8},
    "S05": {"bank": "A", "mask": 16},
    "S06": {"bank": "A", "mask": 32},
}


# Only one vending operation may run at a time.
_vend_lock = asyncio.Lock()


# =========================================================
# Runtime hardware status
# =========================================================

GPIO = None
GPIO_IMPORTED = False
GPIO_AVAILABLE = False
GPIO_INITIALIZED = False
GPIO_ERROR: Optional[str] = None

I2C_INITIALIZED = False
I2C_ERROR: Optional[str] = None

LIGHTS_AVAILABLE = False
LIGHTS_ERROR: Optional[str] = None


# =========================================================
# GPIO import
# =========================================================

try:
    import RPi.GPIO as _GPIO

    GPIO = _GPIO
    GPIO_IMPORTED = True

except Exception as e:
    GPIO = None
    GPIO_IMPORTED = False
    GPIO_AVAILABLE = False
    GPIO_ERROR = f"RPi.GPIO import failed: {e}"


# =========================================================
# Light manager integration
# =========================================================

try:
    from light_manager import (
        lights,
        STATE_IDLE,
        STATE_TABLET_ACTIVE,
        STATE_PAYMENT_AUTHORIZED,
        STATE_VEND_SUCCESS,
    )

    LIGHTS_AVAILABLE = True

except Exception as e:
    lights = None

    STATE_IDLE = "idle"
    STATE_TABLET_ACTIVE = "tablet_active"
    STATE_PAYMENT_AUTHORIZED = "payment_authorized"
    STATE_VEND_SUCCESS = "vend_success"

    LIGHTS_AVAILABLE = False
    LIGHTS_ERROR = str(e)

    print(
        f"[LIGHTS] Light manager unavailable: {e}",
        flush=True,
    )


ALLOWED_LIGHT_STATES = {
    STATE_IDLE,
    STATE_TABLET_ACTIVE,
    STATE_PAYMENT_AUTHORIZED,
    STATE_VEND_SUCCESS,
}


# =========================================================
# Request models
# =========================================================

class VendRequest(BaseModel):
    slot_id: str
    pulse_seconds: float = Field(default=2.0, ge=0.05, le=10.0)
    pulses: int = Field(default=1, ge=1, le=20)
    gap_seconds: float = Field(default=0.25, ge=0.0, le=5.0)


class VendMaskRequest(BaseModel):
    bank: str = Field(default="A")
    mask: int = Field(..., ge=1, le=65535)
    pulse_seconds: float = Field(default=2.0, ge=0.05, le=10.0)
    pulses: int = Field(default=1, ge=1, le=20)
    gap_seconds: float = Field(default=0.25, ge=0.0, le=5.0)

    @model_validator(mode="after")
    def validate_mask_single_bit(self):
        _reg_for_bank(self.bank)

        if not _is_single_bit(self.mask):
            raise ValueError(
                "mask must be single-bit, meaning a power of two"
            )

        return self


class VendMaskVerifiedRequest(BaseModel):
    bank: str = Field(default="A")
    mask: int = Field(..., ge=1, le=65535)
    pulse_seconds: float = Field(default=2.5, ge=0.05, le=10.0)
    beam_wait_seconds: float = Field(default=2.0, ge=0.1, le=10.0)
    retry_attempts: int = Field(default=2, ge=0, le=5)
    retry_gap_seconds: float = Field(default=0.50, ge=0.0, le=5.0)
    post_pulse_settle_seconds: float = Field(
        default=0.15,
        ge=0.0,
        le=3.0,
    )
    require_clear_before_start: bool = Field(default=True)

    @model_validator(mode="after")
    def validate_mask_single_bit(self):
        _reg_for_bank(self.bank)

        if not _is_single_bit(self.mask):
            raise ValueError(
                "mask must be single-bit, meaning a power of two"
            )

        return self


class VendStep(BaseModel):
    bank: str = Field(default="A")
    mask: int = Field(..., ge=1, le=65535)
    pulses: int = Field(default=1, ge=1, le=20)
    pulse_seconds: float = Field(default=2.0, ge=0.05, le=10.0)
    gap_seconds: float = Field(default=0.25, ge=0.0, le=5.0)

    @model_validator(mode="after")
    def validate_step(self):
        _reg_for_bank(self.bank)

        if not _is_single_bit(self.mask):
            raise ValueError(
                "Each step.mask must be single-bit"
            )

        estimated_seconds = self.pulses * (
            self.pulse_seconds + self.gap_seconds
        )

        if estimated_seconds > 60:
            raise ValueError(
                "Step too long. Reduce pulses, pulse_seconds, "
                "or gap_seconds."
            )

        return self


class VendSequenceRequest(BaseModel):
    order_id: Optional[str] = None
    steps: List[VendStep] = Field(..., min_length=1)

    @model_validator(mode="after")
    def validate_total(self):
        total_estimated_seconds = sum(
            step.pulses
            * (step.pulse_seconds + step.gap_seconds)
            for step in self.steps
        )

        if total_estimated_seconds > 120:
            raise ValueError("Sequence too long. Maximum is 120 seconds.")

        return self


# =========================================================
# General helpers
# =========================================================

def _require_api_key(x_api_key: Optional[str]) -> None:
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
        )


def _is_single_bit(mask: int) -> bool:
    return mask != 0 and (mask & (mask - 1)) == 0


def normalize_slot_id(raw: str) -> str:
    slot_id = (raw or "").strip().upper()

    if not SLOT_RE.match(slot_id):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid slot_id '{raw}'. "
                "Expected a value such as S01."
            ),
        )

    slot_number = int(slot_id[1:])

    if slot_number < 1 or slot_number > 20:
        raise HTTPException(
            status_code=400,
            detail=(
                f"slot_id '{slot_id}' is out of range. "
                "Expected S01 through S20."
            ),
        )

    return slot_id


def _reg_for_bank(bank: str) -> str:
    normalized_bank = (bank or "A").strip().upper()

    if normalized_bank == "A":
        return REG_PULSE_A

    if normalized_bank == "B":
        return REG_PULSE_B

    raise ValueError("bank must be 'A' or 'B'")


def resolve_slot_to_hw(slot_id: str) -> Tuple[str, int]:
    normalized_slot = normalize_slot_id(slot_id)
    config = SLOT_TO_MASK.get(normalized_slot)

    if config is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown slot_id '{normalized_slot}'. "
                "No hardware mapping exists on this Pi."
            ),
        )

    if not isinstance(config, dict):
        raise HTTPException(
            status_code=500,
            detail=(
                f"Invalid mapping for {normalized_slot}: "
                f"expected dict, received {type(config)}"
            ),
        )

    bank = (config.get("bank") or "A").strip().upper()

    if bank not in ("A", "B"):
        raise HTTPException(
            status_code=500,
            detail=(
                f"Invalid mapping for {normalized_slot}: "
                "bank must be A or B."
            ),
        )

    mask = config.get("mask", config.get("bit_value"))

    if not isinstance(mask, int):
        raise HTTPException(
            status_code=500,
            detail=(
                f"Invalid mapping for {normalized_slot}: "
                "mask must be an integer."
            ),
        )

    if not _is_single_bit(mask):
        raise HTTPException(
            status_code=500,
            detail=(
                f"Invalid mapping for {normalized_slot}: "
                f"mask {mask} is not single-bit."
            ),
        )

    return bank, mask


# =========================================================
# Shell and I2C helpers
# =========================================================

def _run(command: List[str]) -> str:
    try:
        process = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )

    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"Command timed out: {' '.join(command)}"
        ) from e

    output = (
        (process.stdout or "")
        + (process.stderr or "")
    ).strip()

    if process.returncode != 0:
        raise RuntimeError(
            output or f"Command failed: {' '.join(command)}"
        )

    return output


def i2cset(register: str, value: int) -> None:
    hex_value = hex(int(value) & 0xFFFF)

    _run(
        [
            "i2cset",
            "-y",
            str(I2C_BUS),
            str(I2C_ADDR),
            str(register),
            hex_value,
        ]
    )


def init_mcp23017() -> bool:
    global I2C_INITIALIZED
    global I2C_ERROR

    try:
        i2cset("0x00", 0x00)
        i2cset("0x01", 0x00)

        i2cset(REG_PULSE_A, 0x00)
        i2cset(REG_PULSE_B, 0x00)

        i2cset(REG_ENABLE, 0x00)

        I2C_INITIALIZED = True
        I2C_ERROR = None

        print(
            "[I2C] MCP23017 initialized successfully",
            flush=True,
        )

        return True

    except Exception as e:
        I2C_INITIALIZED = False
        I2C_ERROR = str(e)

        print(
            f"[I2C] MCP23017 initialization failed: {e}",
            flush=True,
        )

        return False


def ensure_i2c_ready() -> None:
    if I2C_INITIALIZED:
        return

    if not init_mcp23017():
        raise HTTPException(
            status_code=503,
            detail={
                "message": "I2C vending hardware is unavailable.",
                "error": I2C_ERROR,
                "bus": I2C_BUS,
                "address": I2C_ADDR,
            },
        )


def clear_vend_outputs_safe() -> None:
    for register in (REG_PULSE_A, REG_PULSE_B):
        try:
            i2cset(register, 0x00)
        except Exception as e:
            print(
                f"[I2C] Failed to clear register {register}: {e}",
                flush=True,
            )


async def _pulse_mask_once(
    bank: str,
    mask: int,
    pulse_seconds: float,
) -> None:
    ensure_i2c_ready()

    pulse_register = _reg_for_bank(bank)

    try:
        i2cset(pulse_register, 0x00)
        i2cset(REG_ENABLE, 0x00)
        i2cset(pulse_register, mask)

        await asyncio.sleep(pulse_seconds)

    finally:
        try:
            i2cset(pulse_register, 0x00)
        except Exception as e:
            print(
                f"[I2C] Failed to stop pulse on {pulse_register}: {e}",
                flush=True,
            )


async def _pulse_mask_repeated(
    bank: str,
    mask: int,
    pulses: int,
    pulse_seconds: float,
    gap_seconds: float,
) -> None:
    for pulse_index in range(pulses):
        await _pulse_mask_once(
            bank,
            mask,
            pulse_seconds,
        )

        is_last_pulse = pulse_index == pulses - 1

        if not is_last_pulse and gap_seconds > 0:
            await asyncio.sleep(gap_seconds)


# =========================================================
# Beam GPIO helpers
# =========================================================

def init_beam_gpio() -> bool:
    global GPIO_AVAILABLE
    global GPIO_INITIALIZED
    global GPIO_ERROR

    if not GPIO_IMPORTED or GPIO is None:
        GPIO_AVAILABLE = False
        GPIO_INITIALIZED = False

        if GPIO_ERROR is None:
            GPIO_ERROR = "RPi.GPIO is not installed or could not be imported."

        print(
            f"[GPIO] Beam sensor unavailable: {GPIO_ERROR}",
            flush=True,
        )

        return False

    try:
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(
            BEAM_GPIO,
            GPIO.IN,
            pull_up_down=GPIO.PUD_UP,
        )

        GPIO_AVAILABLE = True
        GPIO_INITIALIZED = True
        GPIO_ERROR = None

        print(
            f"[GPIO] Beam sensor initialized on BCM GPIO {BEAM_GPIO}",
            flush=True,
        )

        return True

    except Exception as e:
        GPIO_AVAILABLE = False
        GPIO_INITIALIZED = False
        GPIO_ERROR = str(e)

        print(
            f"[GPIO] Beam GPIO initialization failed: {e}",
            flush=True,
        )

        return False


def ensure_beam_gpio_ready() -> None:
    if GPIO_AVAILABLE and GPIO_INITIALIZED:
        return

    if not init_beam_gpio():
        raise HTTPException(
            status_code=503,
            detail={
                "message": "Beam sensor GPIO is unavailable.",
                "error": GPIO_ERROR,
                "beam_gpio": BEAM_GPIO,
                "gpio_library_imported": GPIO_IMPORTED,
            },
        )


def beam_raw_state() -> int:
    ensure_beam_gpio_ready()

    try:
        return int(GPIO.input(BEAM_GPIO))

    except Exception as e:
        global GPIO_AVAILABLE
        global GPIO_INITIALIZED
        global GPIO_ERROR

        GPIO_AVAILABLE = False
        GPIO_INITIALIZED = False
        GPIO_ERROR = str(e)

        raise HTTPException(
            status_code=503,
            detail={
                "message": "Failed to read beam sensor GPIO.",
                "error": GPIO_ERROR,
                "beam_gpio": BEAM_GPIO,
            },
        ) from e


def beam_is_clear() -> bool:
    return beam_raw_state() == BEAM_CLEAR_STATE


def beam_is_broken() -> bool:
    return beam_raw_state() == BEAM_BROKEN_STATE


async def wait_for_beam_break(
    timeout_seconds: float,
    poll_interval: float = 0.01,
) -> bool:
    deadline = (
        asyncio.get_running_loop().time()
        + timeout_seconds
    )

    while asyncio.get_running_loop().time() < deadline:
        if beam_is_broken():
            return True

        await asyncio.sleep(poll_interval)

    return False


# =========================================================
# Light helpers
# =========================================================

def set_light_state_safe(state: str) -> bool:
    if not LIGHTS_AVAILABLE or lights is None:
        print(
            f"[LIGHTS] Skipping '{state}' because lights are unavailable",
            flush=True,
        )

        return False

    try:
        lights.set_state(state)
        return True

    except Exception as e:
        print(
            f"[LIGHTS] Failed to set state '{state}': {e}",
            flush=True,
        )

        return False


async def flash_light_state_then_idle(
    state: str,
    seconds: float = 3.0,
) -> None:
    set_light_state_safe(state)
    await asyncio.sleep(seconds)
    set_light_state_safe(STATE_IDLE)


# =========================================================
# Application lifecycle
# =========================================================

@app.on_event("startup")
def startup() -> None:
    print("[STARTUP] Starting SellMate Vend API", flush=True)

    init_mcp23017()
    init_beam_gpio()
    set_light_state_safe(STATE_IDLE)

    print(
        "[STARTUP] SellMate Vend API startup completed",
        flush=True,
    )


@app.on_event("shutdown")
def shutdown() -> None:
    clear_vend_outputs_safe()

    if GPIO_IMPORTED and GPIO is not None:
        try:
            GPIO.cleanup(BEAM_GPIO)
        except Exception:
            pass


# =========================================================
# Endpoints
# =========================================================

@app.get("/")
def root():
    return {
        "service": "SellMate Vend API",
        "ok": True,
        "health_endpoint": "/health",
    }


@app.get("/health")
def health():
    return {
        "ok": True,
        "service": "SellMate Vend API",
        "i2c": {
            "initialized": I2C_INITIALIZED,
            "bus": I2C_BUS,
            "address": I2C_ADDR,
            "error": I2C_ERROR,
        },
        "gpio": {
            "library_imported": GPIO_IMPORTED,
            "available": GPIO_AVAILABLE,
            "initialized": GPIO_INITIALIZED,
            "beam_gpio": BEAM_GPIO,
            "beam_broken_state": BEAM_BROKEN_STATE,
            "beam_clear_state": BEAM_CLEAR_STATE,
            "error": GPIO_ERROR,
        },
        "lights": {
            "available": LIGHTS_AVAILABLE,
            "error": LIGHTS_ERROR,
        },
    }


@app.post("/hardware/reinitialize")
def reinitialize_hardware(
    x_api_key: Optional[str] = Header(default=None),
):
    _require_api_key(x_api_key)

    i2c_ok = init_mcp23017()
    gpio_ok = init_beam_gpio()

    return {
        "ok": i2c_ok or gpio_ok,
        "i2c_initialized": i2c_ok,
        "i2c_error": I2C_ERROR,
        "gpio_initialized": gpio_ok,
        "gpio_error": GPIO_ERROR,
    }


@app.post("/lights/{state}")
def set_lights(
    state: str,
    x_api_key: Optional[str] = Header(default=None),
):
    _require_api_key(x_api_key)

    normalized_state = state.strip().lower()

    if normalized_state not in ALLOWED_LIGHT_STATES:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Invalid light state",
                "allowed": sorted(ALLOWED_LIGHT_STATES),
            },
        )

    changed = set_light_state_safe(normalized_state)

    if not changed:
        raise HTTPException(
            status_code=503,
            detail={
                "message": "Lighting hardware is unavailable.",
                "state": normalized_state,
                "error": LIGHTS_ERROR,
            },
        )

    return {
        "ok": True,
        "state": normalized_state,
        "lights_available": LIGHTS_AVAILABLE,
    }


@app.get("/beam_status")
def beam_status(
    x_api_key: Optional[str] = Header(default=None),
):
    _require_api_key(x_api_key)

    raw_state = beam_raw_state()

    return {
        "ok": True,
        "beam_gpio": BEAM_GPIO,
        "raw_state": raw_state,
        "beam_broken": raw_state == BEAM_BROKEN_STATE,
        "beam_clear": raw_state == BEAM_CLEAR_STATE,
        "beam_broken_state": BEAM_BROKEN_STATE,
        "beam_clear_state": BEAM_CLEAR_STATE,
    }


@app.post("/vend_mask")
async def vend_mask(
    req: VendMaskRequest,
    x_api_key: Optional[str] = Header(default=None),
):
    _require_api_key(x_api_key)

    async with _vend_lock:
        try:
            await _pulse_mask_repeated(
                req.bank,
                req.mask,
                req.pulses,
                req.pulse_seconds,
                req.gap_seconds,
            )

        except HTTPException:
            raise

        except Exception as e:
            traceback.print_exc()

            raise HTTPException(
                status_code=500,
                detail=str(e),
            ) from e

        finally:
            clear_vend_outputs_safe()

    return {
        "ok": True,
        "mode": "vend_mask",
        "bank": req.bank.upper(),
        "mask": hex(req.mask),
        "pulses": req.pulses,
        "pulse_seconds": req.pulse_seconds,
        "gap_seconds": req.gap_seconds,
    }


@app.post("/vend")
async def vend(
    req: VendRequest,
    x_api_key: Optional[str] = Header(default=None),
):
    _require_api_key(x_api_key)

    bank, mask = resolve_slot_to_hw(req.slot_id)

    async with _vend_lock:
        try:
            await _pulse_mask_repeated(
                bank,
                mask,
                req.pulses,
                req.pulse_seconds,
                req.gap_seconds,
            )

        except HTTPException:
            raise

        except Exception as e:
            traceback.print_exc()

            raise HTTPException(
                status_code=500,
                detail=str(e),
            ) from e

        finally:
            clear_vend_outputs_safe()

    return {
        "ok": True,
        "mode": "vend",
        "slot_id": normalize_slot_id(req.slot_id),
        "bank": bank,
        "mask": hex(mask),
        "pulses": req.pulses,
        "pulse_seconds": req.pulse_seconds,
        "gap_seconds": req.gap_seconds,
    }


@app.post("/vend_mask_verified")
async def vend_mask_verified(
    req: VendMaskVerifiedRequest,
    x_api_key: Optional[str] = Header(default=None),
):
    """
    Production vending endpoint.

    Beam checking is intentionally disabled for now. The motor is pulsed
    once and the request succeeds when the pulse completes.
    """
    _require_api_key(x_api_key)

    async with _vend_lock:
        try:
            ensure_i2c_ready()
            set_light_state_safe(STATE_PAYMENT_AUTHORIZED)

            await _pulse_mask_once(
                req.bank,
                req.mask,
                req.pulse_seconds,
            )

            if req.post_pulse_settle_seconds > 0:
                await asyncio.sleep(req.post_pulse_settle_seconds)

            asyncio.create_task(
                flash_light_state_then_idle(
                    STATE_VEND_SUCCESS,
                    seconds=3.0,
                )
            )

        except HTTPException:
            set_light_state_safe(STATE_IDLE)
            raise

        except Exception as e:
            set_light_state_safe(STATE_IDLE)
            traceback.print_exc()

            raise HTTPException(
                status_code=500,
                detail=str(e),
            ) from e

        finally:
            clear_vend_outputs_safe()

    return {
        "ok": True,
        "mode": "vend_mask_verified",
        "bank": req.bank.upper(),
        "mask": hex(req.mask),
        "beam_check_enabled": False,
        "beam_check_skipped": True,
        "beam_broken": None,
        "verified": False,
        "vend_completed": True,
        "attempt_count": 1,
        "max_attempts": 1,
        "message": "Vend pulse completed. Beam verification is disabled.",
        "attempts": [
            {
                "attempt": 1,
                "beam_check_skipped": True,
                "pulse_completed": True,
            }
        ],
        "pulse_seconds": req.pulse_seconds,
        "beam_wait_seconds": 0,
    }


@app.post("/vend_mask_verified_test")
async def vend_mask_verified_test(
    req: VendMaskVerifiedRequest,
    x_api_key: Optional[str] = Header(default=None),
):
    """
    Test-only copy of the old beam-verified vending endpoint.

    New sensor polarity:
      HIGH / 1 = beam broken
      LOW  / 0 = beam clear
    """
    _require_api_key(x_api_key)

    ensure_beam_gpio_ready()

    max_attempts = 1 + req.retry_attempts
    attempts: List[Dict[str, Any]] = []

    async with _vend_lock:
        try:
            ensure_i2c_ready()

            if req.require_clear_before_start:
                initial_precheck_state = beam_raw_state()

                if initial_precheck_state != BEAM_CLEAR_STATE:
                    set_light_state_safe(STATE_IDLE)

                    return {
                        "ok": False,
                        "mode": "vend_mask_verified_test",
                        "bank": req.bank.upper(),
                        "mask": hex(req.mask),
                        "beam_check_enabled": True,
                        "beam_broken": True,
                        "verified": False,
                        "attempt_count": 0,
                        "max_attempts": max_attempts,
                        "message": (
                            "Beam is already blocked before vend. "
                            "The pickup bin may be full, an item may be "
                            "stuck, or the sensor may be misaligned."
                        ),
                        "attempts": [
                            {
                                "attempt": 0,
                                "beam_broken": True,
                                "initial_state": initial_precheck_state,
                                "saw_clear_after_start": False,
                                "skipped_pulse": True,
                                "reason": "beam_not_clear_before_start",
                            }
                        ],
                        "pulse_seconds": req.pulse_seconds,
                        "beam_wait_seconds": req.beam_wait_seconds,
                    }

            set_light_state_safe(STATE_PAYMENT_AUTHORIZED)

            for attempt_number in range(1, max_attempts + 1):
                initial_state = beam_raw_state()

                await _pulse_mask_once(
                    req.bank,
                    req.mask,
                    req.pulse_seconds,
                )

                if req.post_pulse_settle_seconds > 0:
                    await asyncio.sleep(
                        req.post_pulse_settle_seconds
                    )

                detected = False
                saw_clear_after_start = (
                    initial_state == BEAM_CLEAR_STATE
                )

                deadline = (
                    asyncio.get_running_loop().time()
                    + req.beam_wait_seconds
                )

                while asyncio.get_running_loop().time() < deadline:
                    current_state = beam_raw_state()

                    if current_state == BEAM_CLEAR_STATE:
                        saw_clear_after_start = True

                    if (
                        saw_clear_after_start
                        and current_state == BEAM_BROKEN_STATE
                    ):
                        detected = True
                        break

                    await asyncio.sleep(0.01)

                attempts.append(
                    {
                        "attempt": attempt_number,
                        "beam_broken": detected,
                        "initial_state": initial_state,
                        "saw_clear_after_start": saw_clear_after_start,
                    }
                )

                if detected:
                    asyncio.create_task(
                        flash_light_state_then_idle(
                            STATE_VEND_SUCCESS,
                            seconds=3.0,
                        )
                    )

                    return {
                        "ok": True,
                        "mode": "vend_mask_verified_test",
                        "bank": req.bank.upper(),
                        "mask": hex(req.mask),
                        "beam_check_enabled": True,
                        "beam_broken": True,
                        "verified": True,
                        "attempt_count": attempt_number,
                        "max_attempts": max_attempts,
                        "message": (
                            f"Vend verified on attempt "
                            f"{attempt_number}."
                        ),
                        "attempts": attempts,
                        "pulse_seconds": req.pulse_seconds,
                        "beam_wait_seconds": req.beam_wait_seconds,
                    }

                if (
                    attempt_number < max_attempts
                    and req.retry_gap_seconds > 0
                ):
                    await asyncio.sleep(req.retry_gap_seconds)

            set_light_state_safe(STATE_IDLE)

        except HTTPException:
            set_light_state_safe(STATE_IDLE)
            raise

        except Exception as e:
            set_light_state_safe(STATE_IDLE)
            traceback.print_exc()

            raise HTTPException(
                status_code=500,
                detail=str(e),
            ) from e

        finally:
            clear_vend_outputs_safe()

    return {
        "ok": False,
        "mode": "vend_mask_verified_test",
        "bank": req.bank.upper(),
        "mask": hex(req.mask),
        "beam_check_enabled": True,
        "beam_broken": False,
        "verified": False,
        "attempt_count": max_attempts,
        "max_attempts": max_attempts,
        "message": (
            f"No beam break detected after {max_attempts} attempts."
        ),
        "attempts": attempts,
        "pulse_seconds": req.pulse_seconds,
        "beam_wait_seconds": req.beam_wait_seconds,
    }


@app.post("/vend_sequence")
async def vend_sequence(
    req: VendSequenceRequest,
    x_api_key: Optional[str] = Header(default=None),
):
    _require_api_key(x_api_key)

    async with _vend_lock:
        try:
            ensure_i2c_ready()
            clear_vend_outputs_safe()
            i2cset(REG_ENABLE, 0x00)

            for step in req.steps:
                await _pulse_mask_repeated(
                    step.bank,
                    step.mask,
                    step.pulses,
                    step.pulse_seconds,
                    step.gap_seconds,
                )

        except HTTPException:
            raise

        except Exception as e:
            traceback.print_exc()

            raise HTTPException(
                status_code=500,
                detail=str(e),
            ) from e

        finally:
            clear_vend_outputs_safe()

    return {
        "ok": True,
        "mode": "vend_sequence",
        "order_id": req.order_id,
        "steps": [
            {
                "bank": step.bank.upper(),
                "mask": hex(step.mask),
                "pulses": step.pulses,
                "pulse_seconds": step.pulse_seconds,
                "gap_seconds": step.gap_seconds,
            }
            for step in req.steps
        ],
    }
