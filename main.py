import json
import os
from datetime import datetime, timezone

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

from models import GameRecordCreate, GameRecordOut

RECORDS_FILE = os.path.join(os.path.dirname(__file__), "game_records.json")

app = FastAPI()


def configure_cors(app: FastAPI) -> None:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )


def load_game_records() -> list[GameRecordOut]:
    if not os.path.exists(RECORDS_FILE):
        return []
    with open(RECORDS_FILE, "r") as f:
        data = json.load(f)
    return [GameRecordOut(**item) for item in data]


def save_game_records(records: list[GameRecordOut]) -> None:
    data = [record.model_dump(mode="json") for record in records]
    with open(RECORDS_FILE, "w") as f:
        json.dump(data, f)


def next_game_record_id(records: list[GameRecordOut]) -> int:
    if not records:
        return 1
    return max(r.id for r in records) + 1


configure_cors(app)


@app.get("/multiply")
def multiply(a: int = Query(...), b: int = Query(...)):
    return {"result": a * b}


@app.post("/game-records")
async def create_game_record(record: GameRecordCreate) -> GameRecordOut:
    records = load_game_records()
    new_id = next_game_record_id(records)
    now = datetime.now(timezone.utc)
    new_record = GameRecordOut(
        id=new_id,
        score=record.score,
        ended_at=record.ended_at,
        created_at=now,
    )
    records.append(new_record)
    save_game_records(records)
    return new_record
