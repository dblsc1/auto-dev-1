from datetime import datetime
from pydantic import BaseModel


class GameRecordCreate(BaseModel):
    score: int
    ended_at: datetime


class GameRecordOut(BaseModel):
    id: int
    score: int
    ended_at: datetime
    created_at: datetime
