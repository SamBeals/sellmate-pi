
import asyncio
import os
import subprocess
import traceback
import re
from typing import Optional, List, Dict, Any, Tuple

from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel, Field, model_validator

app = FastAPI()

# ---- Config you own ----
I2C_BUS = os.getenv("I2C_BUS", "1")
I2C_ADDR = os.getenv("I2C_ADDR", "0x27")

REG_PULSE_A = os.getenv("REG_PULSE_A", "0x14")
REG_PULSE_B = os.getenv("REG_PULSE_B", "0x15")
REG_ENABLE = os.getenv("REG_ENABLE", "0x00")

# Beam sensor config
BEAM_GPIO = int(os.getenv("BEAM_GPIO", "17"))
BEAM_ACTIVE_STATE = int(os.getenv("BEAM_ACTIVE_STATE", "0"))  # 0 means beam break reads LOW
BEAM_CLEAR_STATE = 1 if BEAM_ACTIVE_STATE == 0 else 0

SLOT_TO_MASK: Dict[str, Dict[str, Any]] = {
    "S01": {"bank": "A", "mask": 1},
    "S02": {"bank": "A", "mask": 2},
    "S03": {"bank": "A", "mask": 4},
    "S04": {"bank": "A", "mask": 8},
    "S05": {"bank": "A", "mask": 16},
    "S06": {"bank": "A", "mask": 32},
}

_vend_lock = asyncio.Lock()
API_KEY = os.getenv("VEND_API_KEY", "CHANGE_ME")
SLOT_RE = re.compile(r"^S\d{2}$")

GPIO = None
GPIO_AVAILABLE = False

try:
    import RPi.GPIO as _GPIO
    GPIO = _GPIO
    GPIO_AVAILABLE = True
except Exception:
    GPIO = None
    GPIO_AVAILABLE = False


# -------------------------
# Light manager integration
# -------------------------
LIGHTS_AVAILABLE = False

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
    print(f"[LIGHTS] Light manager unavailable: {e}")

    lights = None

    STATE_IDLE = "idle"
    STATE_TABLET_ACTIVE = "tablet_active"
    STATE_PAYMENT_AUTHORIZED = "payment_authorized"
    STATE_VEND_SUCCESS = "vend_success"


ALLOWED_LIGHT_STATES = {
    STATE_IDLE,
    STATE_TABLET_ACTIVE,
    STATE_PAYMENT_AUTHORIZED,
    STATE_VEND_SUCCESS,
}


def set_light_state_safe(state: str) -> bool:
    if not LIGHTS_AVAILABLE or lights is None:
        print(f"[LIGHTS] Skipping state '{state}' because lights are unavailable")
        return False

    try:
        lights.set_state(state)
        return True
    except Exception as e:
        print(f"[LIGHTS] Failed to set state '{state}': {e}")
        return False


async def flash_light_state_then_idle(state: str, seconds: float = 3.0) -> None:
    set_light_state_safe(state)
    await asyncio.sleep(seconds)
    set_light_state_safe(STATE_IDLE)


# -------------------------
# Low-level helpers
# -------------------------
def _run(cmd: List[str]) -> str:
    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Command timed out: {cmd}")

    out = (p.stdout or "") + (p.stderr or "")

    if p.returncode != 0:
        raise RuntimeError(out.strip() or f"Command failed: {cmd}")

    return out.strip()


def i2cset(reg: str, val: int) -> None:
    hex_val = hex(int(val) & 0xFFFF)
    _run(["i2cset", "-y", str(I2C_BUS), str(I2C_ADDR), str(reg), hex_val])


def _is_single_bit(mask: int) -> bool:
    return mask != 0 and (mask & (mask - 1)) == 0


def normalize_slot_id(raw: str) -> str:
    s = (raw or "").strip().upper()

    if not SLOT_RE.match(s):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid slot_id '{raw}'. Expected S01..S20."
        )

    n = int(s[1:])

    if n < 1 or n > 20:
        raise HTTPException(
            status_code=400,
            detail=f"slot_id '{s}' out of range. Expected S01..S20."
        )

    return s


def _reg_for_bank(bank: str) -> str:
    b = (bank or "A").strip().upper()

    if b == "A":
        return REG_PULSE_A

    if b == "B":
        return REG_PULSE_B

    raise ValueError("bank must be 'A' or 'B'")


def resolve_slot_to_hw(slot_id: str) -> Tuple[str, int]:
    sid = normalize_slot_id(slot_id)
    cfg = SLOT_TO_MASK.get(sid)

    if cfg is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown slot_id '{sid}' (no mapping on Pi)."
        )

    if not isinstance(cfg, dict):
        raise HTTPException(
            status_code=500,
            detail=f"Bad mapping for {sid}: expected dict, got {type(cfg)}"
        )

    bank = (cfg.get("bank") or "A").strip().upper()

    if bank not in ("A", "B"):
        raise HTTPException(
            status_code=500,
            detail=f"Bad mapping for {sid}: bank must be 'A' or 'B'"
        )

    mask = cfg.get("mask", cfg.get("bit_value"))

    if not isinstance(mask, int):
        raise HTTPException(
            status_code=500,
            detail=f"Bad mapping for {sid}: mask must be int, got {type(mask)}"
        )

    if not _is_single_bit(mask):
        raise HTTPException(
            status_code=500,
            detail=f"Bad mapping for {sid}: mask is not single-bit: {mask}"
        )

    return bank, mask


async def _pulse_mask_once(bank: str, mask: int, pulse_seconds: float) -> None:
    init_mcp23017()

    reg_pulse = _reg_for_bank(bank)

    i2cset(reg_pulse, 0x00)
    i2cset(REG_ENABLE, 0x00)

    i2cset(reg_pulse, mask)

    await asyncio.sleep(pulse_seconds)

    i2cset(reg_pulse, 0x00)


async def _pulse_mask_repeated(
    bank: str,
    mask: int,
    pulses: int,
    pulse_seconds: float,
    gap_seconds: float,
) -> None:
    for _ in range(pulses):
        await _pulse_mask_once(bank, mask, pulse_seconds)

        if gap_seconds > 0:
            await asyncio.sleep(gap_seconds)


def _require_api_key(x_api_key: Optional[str]) -> None:
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")


def init_mcp23017():
    i2cset("0x00", 0x00)
    i2cset("0x01", 0x00)

    i2cset("0x14", 0x00)
    i2cset("0x15", 0x00)


def init_beam_gpio():
    if not GPIO_AVAILABLE:
        return

    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(BEAM_GPIO, GPIO.IN, pull_up_down=GPIO.PUD_UP)


def beam_raw_state() -> int:
    if not GPIO_AVAILABLE:
        raise RuntimeError("RPi.GPIO is not available on this Pi/environment.")

    return int(GPIO.input(BEAM_GPIO))


def beam_is_clear() -> bool:
    return beam_raw_state() == BEAM_CLEAR_STATE


def beam_is_broken() -> bool:
    return beam_raw_state() == BEAM_ACTIVE_STATE


async def wait_for_beam_break(
    timeout_seconds: float,
    poll_interval: float = 0.01,
) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout_seconds

    while asyncio.get_running_loop().time() < deadline:
        if beam_is_broken():
            return True

        await asyncio.sleep(poll_interval)

    return False


def read_beam_raw() -> int:
    if not GPIO_AVAILABLE:
        return -1

    return GPIO.input(BEAM_GPIO)


def log_beam(label: str):
    raw = read_beam_raw()
    print(f"[BEAM] {label}: raw={raw}", flush=True)


@app.on_event("startup")
def _startup():
    init_mcp23017()
    init_beam_gpio()
    set_light_state_safe(STATE_IDLE)


# -------------------------
# Request models
# -------------------------
class VendRequest(BaseModel):
    slot_id: str
    pulse_seconds: float = Field(default=2.0, ge=0.05, le=10.0)
    pulses: int = Field(default=1, ge=1, le=20)
    gap_seconds: float = Field(default=0.25, ge=0.0, le=5.0)


class VendMaskRequest(BaseModel):
    bank: str = Field(default="A")
    mask: int = Field(..., ge=0, le=65535)
    pulse_seconds: float = Field(default=2.0, ge=0.05, le=10.0)
    pulses: int = Field(default=1, ge=1, le=20)
    gap_seconds: float = Field(default=0.25, ge=0.0, le=5.0)

    @model_validator(mode="after")
    def validate_mask_single_bit(self):
        _reg_for_bank(self.bank)

        if self.mask != 0 and not _is_single_bit(self.mask):
            raise ValueError("mask must be single-bit (power of two)")

        return self


class VendMaskVerifiedRequest(BaseModel):
    bank: str = Field(default="A")
    mask: int = Field(..., ge=1, le=65535)
    pulse_seconds: float = Field(default=2.5, ge=0.05, le=10.0)
    beam_wait_seconds: float = Field(default=2.0, ge=0.1, le=10.0)
    retry_attempts: int = Field(default=2, ge=0, le=5)
    retry_gap_seconds: float = Field(default=0.50, ge=0.0, le=5.0)
    post_pulse_settle_seconds: float = Field(default=0.15, ge=0.0, le=3.0)
    require_clear_before_start: bool = Field(default=True)

    @model_validator(mode="after")
    def validate_mask_single_bit(self):
        _reg_for_bank(self.bank)

        if not _is_single_bit(self.mask):
            raise ValueError("mask must be single-bit (power of two)")

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
            raise ValueError("Each step.mask must be single-bit (power of two)")

        est = self.pulses * (self.pulse_seconds + self.gap_seconds)

        if est > 60:
            raise ValueError(
                "Step too long (>60s). Reduce pulses/pulse_seconds/gap_seconds."
            )

        return self


class VendSequenceRequest(BaseModel):
    order_id: Optional[str] = None
    steps: List[VendStep] = Field(..., min_length=1)

    @model_validator(mode="after")
    def validate_total(self):
        total_est = sum(
            s.pulses * (s.pulse_seconds + s.gap_seconds)
            for s in self.steps
        )

        if total_est > 120:
            raise ValueError("Sequence too long (>120s).")

        return self


# -------------------------
# Endpoints
# -------------------------
@app.get("/health")
def health():
    return {
        "ok": True,
        "gpio_available": GPIO_AVAILABLE,
        "beam_gpio": BEAM_GPIO,
        "lights_available": LIGHTS_AVAILABLE,
    }


@app.post("/lights/{state}")
def set_lights(state: str, x_api_key: Optional[str] = Header(default=None)):
    _require_api_key(x_api_key)

    state = state.strip().lower()

    if state not in ALLOWED_LIGHT_STATES:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Invalid light state",
                "allowed": sorted(ALLOWED_LIGHT_STATES),
            },
        )

    changed = set_light_state_safe(state)

    return {
        "ok": changed,
        "state": state,
        "lights_available": LIGHTS_AVAILABLE,
    }


@app.get("/beam_status")
def beam_status(x_api_key: Optional[str] = Header(default=None)):
    _require_api_key(x_api_key)

    if not GPIO_AVAILABLE:
        raise HTTPException(
            status_code=500,
            detail="RPi.GPIO is not available."
        )

    raw = beam_raw_state()

    return {
        "ok": True,
        "beam_gpio": BEAM_GPIO,
        "raw_state": raw,
        "beam_broken": raw == BEAM_ACTIVE_STATE,
        "beam_clear": raw == BEAM_CLEAR_STATE,
        "beam_active_state": BEAM_ACTIVE_STATE,
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

        except Exception as e:
            traceback.print_exc()
            raise HTTPException(status_code=500, detail=str(e))

    return {
        "ok": True,
        "mode": "vend_mask",
        "bank": req.bank.upper(),
        "mask": hex(req.mask),
        "pulses": req.pulses,
        "pulse_seconds": req.pulse_seconds,
        "gap_seconds": req.gap_seconds,
    }


@app.post("/vend_mask_verified")
async def vend_mask_verified(
    req: VendMaskVerifiedRequest,
    x_api_key: Optional[str] = Header(default=None),
):
    _require_api_key(x_api_key)

    if not GPIO_AVAILABLE:
        raise HTTPException(
            status_code=500,
            detail="RPi.GPIO is not available. Cannot use beam verification."
        )

    max_attempts = 1 + req.retry_attempts
    attempts: List[Dict[str, Any]] = []

    async with _vend_lock:
        try:
            if req.require_clear_before_start:
                initial_precheck_state = beam_raw_state()

                if initial_precheck_state != BEAM_CLEAR_STATE:
                    set_light_state_safe(STATE_IDLE)

                    return {
                        "ok": False,
                        "mode": "vend_mask_verified",
                        "bank": req.bank.upper(),
                        "mask": hex(req.mask),
                        "beam_broken": True,
                        "verified": False,
                        "attempt_count": 0,
                        "max_attempts": max_attempts,
                        "message": (
                            "Beam is already blocked before vend. "
                            "Pickup bin may be full, item may be stuck, "
                            "or sensor may be misaligned/disconnected."
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

            for attempt_num in range(1, max_attempts + 1):
                initial_state = beam_raw_state()

                await _pulse_mask_once(
                    req.bank,
                    req.mask,
                    req.pulse_seconds,
                )

                if req.post_pulse_settle_seconds > 0:
                    await asyncio.sleep(req.post_pulse_settle_seconds)

                detected = False
                saw_clear_after_start = initial_state == BEAM_CLEAR_STATE

                deadline = (
                    asyncio.get_running_loop().time()
                    + req.beam_wait_seconds
                )

                while asyncio.get_running_loop().time() < deadline:
                    current = beam_raw_state()

                    raw = read_beam_raw()
                    print(f"[BEAM] waiting: raw={raw}", flush=True)

                    if current == BEAM_CLEAR_STATE:
                        saw_clear_after_start = True

                    if (
                        saw_clear_after_start
                        and current == BEAM_ACTIVE_STATE
                    ):
                        detected = True
                        break

                    await asyncio.sleep(0.01)

                attempts.append({
                    "attempt": attempt_num,
                    "beam_broken": detected,
                    "initial_state": initial_state,
                    "saw_clear_after_start": saw_clear_after_start,
                })

                if detected:
                    asyncio.create_task(
                        flash_light_state_then_idle(
                            STATE_VEND_SUCCESS,
                            seconds=3.0,
                        )
                    )

                    return {
                        "ok": True,
                        "mode": "vend_mask_verified",
                        "bank": req.bank.upper(),
                        "mask": hex(req.mask),
                        "beam_broken": True,
                        "verified": True,
                        "attempt_count": attempt_num,
                        "max_attempts": max_attempts,
                        "message": f"Vend verified on attempt {attempt_num}.",
                        "attempts": attempts,
                        "pulse_seconds": req.pulse_seconds,
                        "beam_wait_seconds": req.beam_wait_seconds,
                    }

                if (
                    attempt_num < max_attempts
                    and req.retry_gap_seconds > 0
                ):
                    await asyncio.sleep(req.retry_gap_seconds)

            set_light_state_safe(STATE_IDLE)

        except Exception as e:
            set_light_state_safe(STATE_IDLE)
            traceback.print_exc()
            raise HTTPException(status_code=500, detail=str(e))

        finally:
            try:
                i2cset(REG_PULSE_A, 0x00)
                i2cset(REG_PULSE_B, 0x00)
            except Exception:
                pass

    return {
        "ok": False,
        "mode": "vend_mask_verified",
        "bank": req.bank.upper(),
        "mask": hex(req.mask),
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

        except Exception as e:
            traceback.print_exc()
            raise HTTPException(status_code=500, detail=str(e))

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


@app.post("/vend_sequence")
async def vend_sequence(
    req: VendSequenceRequest,
    x_api_key: Optional[str] = Header(default=None),
):
    _require_api_key(x_api_key)

    async with _vend_lock:
        try:
            i2cset(REG_PULSE_A, 0x00)
            i2cset(REG_PULSE_B, 0x00)
            i2cset(REG_ENABLE, 0x00)

            for step in req.steps:
                await _pulse_mask_repeated(
                    step.bank,
                    step.mask,
                    step.pulses,
                    step.pulse_seconds,
                    step.gap_seconds,
                )

            i2cset(REG_PULSE_A, 0x00)
            i2cset(REG_PULSE_B, 0x00)

        except Exception as e:
            traceback.print_exc()
            raise HTTPException(status_code=500, detail=str(e))

    return {
        "ok": True,
        "mode": "vend_sequence",
        "order_id": req.order_id,
        "steps": [
            {
                "bank": s.bank.upper(),
                "mask": hex(s.mask),
                "pulses": s.pulses,
                "pulse_seconds": s.pulse_seconds,
                "gap_seconds": s.gap_seconds,
            }
            for s in req.steps
        ],
    }

