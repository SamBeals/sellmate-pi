import asyncio
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel, Field
import subprocess
from typing import Optional
import os
import traceback

app = FastAPI()

# ---- Config you own ----
I2C_BUS = "1"
I2C_ADDR = "0x27"
REG_PULSE = "0x14"   # your GPIO register
REG_ENABLE = "0x00"  # your enable register

# Slot -> bitmask mapping (MVP).
# TODO: Move this into a JSON config file or Firestore later.
SLOT_TO_MASK = {
    "shelf1_lane3": "0x80",
    # "shelf1_lane1": "0x01",
    # ...
}

# Simple concurrency control so you don't pulse two motors at once.
_vend_lock = asyncio.Lock()
API_KEY = os.getenv("VEND_API_KEY", "CHANGE_ME")

class VendRequest(BaseModel):
    slot_id: str
    pulse_seconds: float = Field(default=2.0, ge=0.05, le=10.0)

class VendMaskRequest(BaseModel):
    mask: int = Field(..., ge=0, le=65535)
    pulse_seconds: float = Field(..., gt=0.0, le=5.0)

def _run(cmd: list[str]) -> str:
    # Runs a command and returns stdout+stderr; raises if failed
    p = subprocess.run(cmd, capture_output=True, text=True)
    out = (p.stdout or "") + (p.stderr or "")
    if p.returncode != 0:
        raise RuntimeError(out.strip() or f"Command failed: {cmd}")
    return out.strip()

def i2cset(reg, val):
    _run([
        "i2cset",
        "-y",
        str(I2C_BUS),
        str(I2C_ADDR),
        str(reg),
        str(val),
    ])

async def vend_sequence(mask: str, pulse_seconds: float) -> None:
    # 1) Safe OFF
    i2cset(REG_PULSE, "0x00")

    # 2) Enable outputs
    i2cset(REG_ENABLE, "0x00")

    # 3) Pulse
    i2cset(REG_PULSE, mask)
    await asyncio.sleep(pulse_seconds)
    i2cset(REG_PULSE, "0x00")

@app.post("/vend_mask")
async def vend_mask(req: VendMaskRequest, x_api_key: Optional[str] = Header(default=None)):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

    if req.mask != 0 and (req.mask & (req.mask - 1)) != 0:
        raise HTTPException(status_code=400, detail="mask must be single-bit (power of two)")

    async with _vend_lock:
        try:
            await vend_sequence(req.mask, req.pulse_seconds)
        except Exception as e:
            traceback.print_exc()  # <-- prints full stack trace to Pi console
            raise HTTPException(status_code=500, detail=str(e))  # <-- returns actual error to curl

    return {"ok": True, "mask": req.mask, "pulse_seconds": req.pulse_seconds}

@app.post("/vend")
async def vend(req: VendRequest, x_api_key: Optional[str] = Header(default=None)):
    # TODO: Replace with real auth. For MVP: shared secret in header.
    EXPECTED = "CHANGE_ME"
    if EXPECTED and x_api_key != EXPECTED:
        raise HTTPException(status_code=401, detail="Unauthorized")

    mask = SLOT_TO_MASK.get(req.slot_id)
    if not mask:
        raise HTTPException(status_code=400, detail=f"Unknown slot_id: {req.slot_id}")

    async with _vend_lock:
        try:
            await vend_sequence(mask, req.pulse_seconds)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    return {"ok": True, "slot_id": req.slot_id, "mask": mask, "pulse_seconds": req.pulse_seconds}
