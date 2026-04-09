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

# Your known-working registers (DEFAULT = bank A)
# NOTE: For MCP23017, OLATA=0x14 and OLATB=0x15.
REG_PULSE_A = os.getenv("REG_PULSE_A", "0x14")
REG_PULSE_B = os.getenv("REG_PULSE_B", "0x15")

REG_ENABLE = os.getenv("REG_ENABLE", "0x00")

# Slot -> hardware mapping (MVP).
# Canonical Slot IDs match Firestore: S01..S20
# TODO: Move this into a JSON config file or Firestore later.
SLOT_TO_MASK: Dict[str, Dict[str, Any]] = {
    "S01": {"bank": "A", "mask": 1},   # PA0
    "S02": {"bank": "A", "mask": 2},   # PA1
    "S03": {"bank": "A", "mask": 4},   # PA2
    "S04": {"bank": "A", "mask": 8},   # PA3
    "S05": {"bank": "A", "mask": 16},  # PA4
    "S06": {"bank": "A", "mask": 32},  # PA5
    # Add the rest: S07..S20
}

# Simple concurrency control so you don't pulse two motors at once.
_vend_lock = asyncio.Lock()

# Shared secret in header
API_KEY = os.getenv("VEND_API_KEY", "CHANGE_ME")

SLOT_RE = re.compile(r"^S\d{2}$")

# -------------------------
# Low-level helpers
# -------------------------
def _run(cmd: List[str]) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True)
    out = (p.stdout or "") + (p.stderr or "")
    if p.returncode != 0:
        raise RuntimeError(out.strip() or f"Command failed: {cmd}")
    return out.strip()


def i2cset(reg: str, val: int) -> None:
    """
    Write a single byte/word value with i2cset.
    We always format values as hex (e.g., 0x80, 0x00).
    """
    hex_val = hex(int(val) & 0xFFFF)  # keep compatible with your previous 0..65535 bounds
    _run(["i2cset", "-y", str(I2C_BUS), str(I2C_ADDR), str(reg), hex_val])


def _is_single_bit(mask: int) -> bool:
    return mask != 0 and (mask & (mask - 1)) == 0


def normalize_slot_id(raw: str) -> str:
    """
    Enforce canonical DB slot IDs: S01..S20 (zero-padded).
    """
    s = (raw or "").strip().upper()
    if not SLOT_RE.match(s):
        raise HTTPException(status_code=400, detail=f"Invalid slot_id '{raw}'. Expected S01..S20.")
    n = int(s[1:])
    if n < 1 or n > 20:
        raise HTTPException(status_code=400, detail=f"slot_id '{s}' out of range. Expected S01..S20.")
    return s


def _reg_for_bank(bank: str) -> str:
    """
    Map requested bank to the correct output register.
    Defaults are set up for MCP23017: A=0x14, B=0x15.
    """
    b = (bank or "A").strip().upper()
    if b == "A":
        return REG_PULSE_A
    if b == "B":
        return REG_PULSE_B
    raise ValueError("bank must be 'A' or 'B'")


def resolve_slot_to_hw(slot_id: str) -> Tuple[str, int]:
    """
    Resolve canonical SlotID (S01..S20) to (bank, mask_int).
    Today uses local dict; later swap to Firestore/JSON without changing endpoints.
    """
    sid = normalize_slot_id(slot_id)
    cfg = SLOT_TO_MASK.get(sid)
    if cfg is None:
        raise HTTPException(status_code=400, detail=f"Unknown slot_id '{sid}' (no mapping on Pi).")

    if not isinstance(cfg, dict):
        raise HTTPException(status_code=500, detail=f"Bad mapping for {sid}: expected dict, got {type(cfg)}")

    bank = (cfg.get("bank") or "A").strip().upper()
    if bank not in ("A", "B"):
        raise HTTPException(status_code=500, detail=f"Bad mapping for {sid}: bank must be 'A' or 'B'")

    mask = cfg.get("mask", cfg.get("bit_value"))
    if not isinstance(mask, int):
        raise HTTPException(status_code=500, detail=f"Bad mapping for {sid}: mask must be int, got {type(mask)}")

    if not _is_single_bit(mask):
        raise HTTPException(status_code=500, detail=f"Bad mapping for {sid}: mask is not single-bit: {mask}")

    return bank, mask


async def _pulse_mask_once(bank: str, mask: int, pulse_seconds: float) -> None:
    """
    One pulse on a specific bank: mask ON for pulse_seconds, then OFF.
    """
    init_mcp23017()

    reg_pulse = _reg_for_bank(bank)

    i2cset(reg_pulse, 0x00)   # Safe OFF for this bank
    i2cset(REG_ENABLE, 0x00)  # Enable outputs (per your existing behavior)

    i2cset(reg_pulse, mask)   # ON
    await asyncio.sleep(pulse_seconds)
    i2cset(reg_pulse, 0x00)   # OFF


async def _pulse_mask_repeated(bank: str, mask: int, pulses: int, pulse_seconds: float, gap_seconds: float) -> None:
    """
    Repeat pulses with a gap between them (bank-aware).
    """
    for _ in range(pulses):
        await _pulse_mask_once(bank, mask, pulse_seconds)
        if gap_seconds > 0:
            await asyncio.sleep(gap_seconds)


def _require_api_key(x_api_key: Optional[str]) -> None:
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")


def init_mcp23017():
    # Set both banks to outputs
    i2cset("0x00", 0x00)  # IODIRA
    i2cset("0x01", 0x00)  # IODIRB

    # Optional: clear outputs
    i2cset("0x14", 0x00)  # OLATA
    i2cset("0x15", 0x00)  # OLATB


@app.on_event("startup")
def _startup():
    init_mcp23017()

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
    # Keep as int like your current file (you can pass 128 or 0x80 in JSON)
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


class VendStep(BaseModel):
    """
    A vend step = one motor line (single-bit mask) pulsed N times.
    You can send multiple steps to vend multiple different slots/motors in one request.
    """
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
        # Keep any one step from taking forever
        est = self.pulses * (self.pulse_seconds + self.gap_seconds)
        if est > 60:
            raise ValueError("Step too long (>60s). Reduce pulses/pulse_seconds/gap_seconds.")
        return self


class VendSequenceRequest(BaseModel):
    order_id: Optional[str] = None
    steps: List[VendStep] = Field(..., min_length=1)

    @model_validator(mode="after")
    def validate_total(self):
        total_est = sum(s.pulses * (s.pulse_seconds + s.gap_seconds) for s in self.steps)
        if total_est > 120:
            raise ValueError("Sequence too long (>120s).")
        return self


# -------------------------
# Endpoints
# -------------------------
@app.get("/health")
def health():
    return {"ok": True}


@app.post("/vend_mask")
async def vend_mask(req: VendMaskRequest, x_api_key: Optional[str] = Header(default=None)):
    _require_api_key(x_api_key)

    async with _vend_lock:
        try:
            await _pulse_mask_repeated(req.bank, req.mask, req.pulses, req.pulse_seconds, req.gap_seconds)
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


@app.post("/vend")
async def vend(req: VendRequest, x_api_key: Optional[str] = Header(default=None)):
    _require_api_key(x_api_key)

    bank, mask = resolve_slot_to_hw(req.slot_id)

    async with _vend_lock:
        try:
            await _pulse_mask_repeated(bank, mask, req.pulses, req.pulse_seconds, req.gap_seconds)
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
async def vend_sequence(req: VendSequenceRequest, x_api_key: Optional[str] = Header(default=None)):
    """
    Execute multiple steps, each with multiple pulses.
    Example: vend 2 items from motor A (2 pulses), then 1 item from motor B (1 pulse).
    """
    _require_api_key(x_api_key)

    async with _vend_lock:
        try:
            # Safe OFF before starting (both banks)
            i2cset(REG_PULSE_A, 0x00)
            i2cset(REG_PULSE_B, 0x00)
            i2cset(REG_ENABLE, 0x00)

            for step in req.steps:
                await _pulse_mask_repeated(step.bank, step.mask, step.pulses, step.pulse_seconds, step.gap_seconds)

            # Safe OFF after (both banks)
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
