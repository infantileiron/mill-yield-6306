from pydantic import BaseModel, Field, field_validator
from typing import Literal


class Login(BaseModel):
    email: str
    password: str


class BatchInput(BaseModel):
    code: str = Field(min_length=3, max_length=50, pattern=r"^[A-Za-z0-9_-]+$")
    line: Literal["一线", "二线", "三线"]
    supplier: str = Field(min_length=1, max_length=80)
    tonnes: float = Field(gt=0, le=10000, allow_inf_nan=False)
    received_date: str

    @field_validator('received_date')
    @classmethod
    def valid_date(cls, v):
        from datetime import date
        date.fromisoformat(v)
        return v


class InspectionInput(BaseModel):
    sample_code: str = Field(min_length=3, max_length=70, pattern=r"^[A-Za-z0-9_-]+$")
    batch_id: int = Field(gt=0)
    flour_yield: float = Field(gt=0, le=100, allow_inf_nan=False)
    moisture: float = Field(ge=0, le=30, allow_inf_nan=False)
    inspector: str = Field(min_length=1, max_length=50)


class ReviewInput(BaseModel):
    title: str = Field(min_length=2, max_length=120)
    line: Literal["全部", "一线", "二线", "三线"] = "全部"


class ApprovalInput(BaseModel):
    note: str = Field(min_length=5, max_length=1500)


class MergePreviewInput(BaseModel):
    batch_ids: list[int] = Field(min_length=2, max_length=50)

    @field_validator('batch_ids')
    @classmethod
    def positive(cls, v):
        if any(x <= 0 for x in v):
            raise ValueError('批号标识必须为正整数')
        return v


class MergeConfirmInput(BaseModel):
    batch_ids: list[int] = Field(min_length=2, max_length=50)
    idempotency_key: str = Field(min_length=8, max_length=80,
                                 pattern=r"^[A-Za-z0-9_-]+$")
    primary_batch_id: int | None = Field(default=None, gt=0)
    code: str | None = Field(default=None, max_length=50)
    verified_tonnes: float | None = Field(default=None, gt=0, le=10000,
                                          allow_inf_nan=False)
    note: str | None = Field(default=None, max_length=500)

    @field_validator('batch_ids')
    @classmethod
    def positive(cls, v):
        if any(x <= 0 for x in v):
            raise ValueError('批号标识必须为正整数')
        return v

    @field_validator('code')
    @classmethod
    def code_pattern(cls, v):
        import re
        if v is not None and v.strip() and not re.fullmatch(r"[A-Za-z0-9_-]+", v.strip()):
            raise ValueError('归并组编号仅支持字母、数字、下划线与连字符')
        return v
