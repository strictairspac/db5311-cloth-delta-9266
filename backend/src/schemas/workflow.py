from typing import Any

from pydantic import BaseModel, Field


class LoginForm(BaseModel):
    username: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=128)


class CaseForm(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    payload: dict[str, Any]


class ReviewForm(BaseModel):
    note: str = Field(min_length=1, max_length=500)


class RevisionForm(BaseModel):
    batch_id: str = Field(min_length=1, max_length=64)
    revision: int = Field(ge=1, le=1_000_000_000)
    action: str = Field(pattern='^(记账|撤销)$')
    grade: str = Field(max_length=64, default='')
    weight_g: int = Field(ge=0, le=1_000_000_000)
    pieces: int = Field(ge=0, le=1_000_000_000)


class AppendRevisionsForm(BaseModel):
    revisions: list[RevisionForm] = Field(min_length=1, max_length=500)


class SignLineForm(BaseModel):
    note: str = Field(default='', max_length=500)
