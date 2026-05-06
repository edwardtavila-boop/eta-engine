"""Minimal FastAPI server exposing Force Multiplier health status."""
import sys

sys.path.insert(0, r"C:\EvolutionaryTradingAlgo")
sys.path.insert(0, r"C:\EvolutionaryTradingAlgo\firm\eta_engine")

from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI(title="ETA Force Multiplier Status")


@app.get("/api/fm/status")
async def fm_status():
    try:
        from eta_engine.brain.multi_model import force_multiplier_status
        return force_multiplier_status()
    except Exception as e:
        return JSONResponse(
            status_code=503,
            content={"error": "fm_unavailable", "detail": str(e)[:200]},
        )


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8422)
