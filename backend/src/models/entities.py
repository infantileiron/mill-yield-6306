from sqlalchemy import String, Float, Integer, ForeignKey, JSON, Text, DateTime, Index, text
from sqlalchemy.orm import Mapped, mapped_column
from datetime import datetime, timezone
from src.config.database import Base

def _now():
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(String(300))
    role: Mapped[str] = mapped_column(String(20))


class Batch(Base):
    __tablename__ = "batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True)
    line: Mapped[str] = mapped_column(String(20))
    supplier: Mapped[str] = mapped_column(String(80))
    tonnes: Mapped[float] = mapped_column(Float)
    received_date: Mapped[str] = mapped_column(String(10))


class Inspection(Base):
    __tablename__ = "inspections"
    id: Mapped[int] = mapped_column(primary_key=True)
    sample_code: Mapped[str] = mapped_column(String(70), unique=True)
    # 永不变更的录入入口批号（追加留痕）；归并只建立批次间关系，不改写样本归属行。
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"))
    flour_yield: Mapped[float] = mapped_column(Float)
    moisture: Mapped[float] = mapped_column(Float)
    inspector: Mapped[str] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class BatchGroup(Base):
    """批次归并组：同一车原粮的多个误建批号合并为一个统计口径。"""
    __tablename__ = "batch_groups"
    id: Mapped[int] = mapped_column(primary_key=True)
    # 仅 active 组编号唯一（部分唯一索引）；解除后的历史组保留同名履历，允许重新归并。
    code: Mapped[str] = mapped_column(String(50))
    primary_batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"))
    verified_tonnes: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active / dissolved
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    dissolved_by: Mapped[str] = mapped_column(String(100), default="")
    dissolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("uq_batch_group_active_code", "code", unique=True,
              postgresql_where=text("status = 'active'")),
    )


class BatchGroupMember(Base):
    """归并组成员履历：解除归并不删除行，便于还原与追溯。"""
    __tablename__ = "batch_group_members"
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("batch_groups.id"))
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"))
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        Index("ix_bg_members_batch", "batch_id"),
        Index("ix_bg_members_group", "group_id"),
    )


class IdempotencyOp(Base):
    """写操作幂等记录：同号重试直接返回首次结果，不重复生效。"""
    __tablename__ = "idempotency_ops"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    operation: Mapped[str] = mapped_column(String(40))
    response: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Review(Base):
    __tablename__ = "reviews"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(120))
    line: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="queued")
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    snapshot: Mapped[list] = mapped_column(JSON, default=list)
    # 提交时刻的归并口径（归并组吨数只计一次等），后续归并变化不影响此依据。
    context: Mapped[dict] = mapped_column(JSON, default=dict)
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(100))
    approved_by: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Audit(Base):
    __tablename__ = "audits"
    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(200))
    status: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
