"""FastAPI service exposing the Force Multiplier status contract."""

from __future__ import annotations

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from eta_engine.deploy.fm_status_payload import build_status_payload  # noqa: E402

app = FastAPI(title="ETA Force Multiplier Status")


@app.get("/api/fm/status")
async def fm_status() -> JSONResponse:
    return JSONResponse(
        content=build_status_payload(),
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@app.get("/health")
@app.get("/healthz")
async def health() -> dict[str, str]:
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8422)
