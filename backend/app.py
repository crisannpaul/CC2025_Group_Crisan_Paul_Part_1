from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator
from typing import List
import json
from pathlib import Path
from datetime import datetime

app = FastAPI(title="Sensor API", version="0.1.0")

# Allow local dev frontends
origins = [
    "http://localhost:8080",
    "http://127.0.0.1:8080",
    "http://localhost:5500",
    "http://127.0.0.1:5500",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DATA_PATH = Path(__file__).parent / "data.json"

class SensorRow(BaseModel):
    timestamp: str
    temperature: float
    humidity: float
    voltage: float

    @field_validator("timestamp")
    @classmethod
    def validate_ts(cls, v: str) -> str:
        # ensures ISO-ish format; throws if bad
        datetime.fromisoformat(v.replace("Z", "+00:00"))
        return v

def load_data() -> List[SensorRow]:
    if not DATA_PATH.exists():
        return []
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return [SensorRow(**row) for row in raw]

@app.get("/api/health")
def health():
    return {"status": "ok"}

@app.get("/api/data", response_model=List[SensorRow])
def get_data():
    return load_data()

@app.get("/api/data/latest", response_model=SensorRow)
def get_latest():
    data = load_data()
    if not data:
        raise HTTPException(status_code=404, detail="No data")
    # max by timestamp (ISO 8601 sortable)
    latest = max(data, key=lambda r: r.timestamp)
    return latest
