"""批次归并领域服务。

并发约定（PostgreSQL 行锁 + 事务）：
- 归并确认先按 id 顺序锁定全部候选批次行，并发归并/解除/补录在同一批行上串行化，
  既不会串批，也不会丢失已经提交的检验记录。
- 解除归并锁定归并组行，重复解除读到 dissolved 后原样返回，不重复生效。
- 客户端幂等键落 idempotency_ops 唯一主键；同号重试返回首次响应。
"""
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException

from src.models.entities import (
    Batch, Inspection, BatchGroup, BatchGroupMember, IdempotencyOp,
)
from src.utils.serialize import record


def active_membership(db):
    """返回 {batch_id: BatchGroup}，仅含生效中的归并组。语句级一致性读。"""
    rows = db.execute(
        select(BatchGroupMember.batch_id, BatchGroup)
        .join(BatchGroup, BatchGroup.id == BatchGroupMember.group_id)
        .where(BatchGroup.status == "active")
    ).all()
    return {batch_id: group for batch_id, group in rows}


def group_members(db, group_id):
    return list(db.scalars(
        select(Batch)
        .join(BatchGroupMember, BatchGroupMember.batch_id == Batch.id)
        .where(BatchGroupMember.group_id == group_id)
        .order_by(Batch.id)
    ))


def _sample_counts(db, batch_ids):
    if not batch_ids:
        return {}
    rows = db.execute(
        select(Inspection.batch_id, func.count(Inspection.id))
        .where(Inspection.batch_id.in_(batch_ids))
        .group_by(Inspection.batch_id)
    ).all()
    return dict(rows)


def _member_payload(db, batches, counts):
    return [
        {
            "batch_id": b.id, "code": b.code, "line": b.line,
            "supplier": b.supplier, "tonnes": b.tonnes,
            "received_date": b.received_date, "samples": counts.get(b.id, 0),
        }
        for b in batches
    ]


def group_detail(db, group):
    members = group_members(db, group.id)
    counts = _sample_counts(db, [b.id for b in members])
    payload = _member_payload(db, members, counts)
    primary = next((b for b in members if b.id == group.primary_batch_id), None)
    return {
        **record(group),
        "primary_code": primary.code if primary else "",
        "members": payload,
        "member_count": len(payload),
        "total_samples": sum(m["samples"] for m in payload),
        "sum_member_tonnes": round(sum(m["tonnes"] for m in payload), 4),
    }


def list_groups(db):
    groups = list(db.scalars(select(BatchGroup).order_by(BatchGroup.id.desc())))
    return [group_detail(db, g) for g in groups]


def _validate_candidates(db, batch_ids, primary_batch_id):
    ids = list(dict.fromkeys(batch_ids))  # 去重保序
    if len(ids) < 2:
        raise HTTPException(422, "归并至少选择两个不同批号")
    batches = list(db.scalars(
        select(Batch).where(Batch.id.in_(ids)).order_by(Batch.id).with_for_update()
    ))
    if len(batches) != len(ids):
        raise HTTPException(404, "部分原粮批次不存在，请刷新后重试")
    lines = {b.line for b in batches}
    if len(lines) > 1:
        raise HTTPException(422, "仅同一生产线的批号可以归并")
    suppliers = {b.supplier for b in batches}
    if len(suppliers) > 1:
        raise HTTPException(422, "仅同一供应商的批号可以归并，不同供应商请分别核对")
    primary_id = primary_batch_id or batches[0].id
    if primary_id not in ids:
        raise HTTPException(422, "主批号必须是归并成员之一")
    # 成员是否已处在生效归并组中（行锁已挡住并发归并提交）
    membership = active_membership(db)
    conflicts = sorted({b.code for b in batches if b.id in membership})
    if conflicts:
        raise HTTPException(409, f"批号已在生效归并组中，请先解除：{'、'.join(conflicts)}")
    return batches, primary_id


def preview(db, data):
    ids = list(dict.fromkeys(data.batch_ids))
    batches = list(db.scalars(
        select(Batch).where(Batch.id.in_(ids)).order_by(Batch.id)
    ))
    if len(batches) != len(set(ids)):
        raise HTTPException(404, "部分原粮批次不存在，请刷新后重试")
    if len(batches) < 2:
        raise HTTPException(422, "归并至少选择两个不同批号")
    membership = active_membership(db)
    counts = _sample_counts(db, [b.id for b in batches])
    primary = batches[0]
    same_line = len({b.line for b in batches}) == 1
    same_supplier = len({b.supplier for b in batches}) == 1
    blocked = sorted({b.code for b in batches if b.id in membership})
    return {
        "members": _member_payload(db, batches, counts),
        "member_count": len(batches),
        "total_samples": sum(counts.values()),
        "sum_member_tonnes": round(sum(b.tonnes for b in batches), 4),
        "primary_batch_id": primary.id,
        "proposed_group_code": primary.code,
        "verified_tonnes": primary.tonnes,
        "same_line": same_line,
        "same_supplier": same_supplier,
        "blocked_by_active_group": blocked,
        "ready": same_line and same_supplier and not blocked,
    }


def _idem_take(db, key, operation):
    existing = db.get(IdempotencyOp, key)
    if existing is not None:
        if existing.operation != operation:
            raise HTTPException(409, "幂等键已用于其他操作，请更换后重试")
        return existing.response
    return None


def _idem_store(db, key, operation, response):
    db.add(IdempotencyOp(key=key, operation=operation, response=response))


def confirm_merge(db, data, user):
    cached = _idem_take(db, data.idempotency_key, "merge.confirm")
    if cached is not None:
        return cached, True
    batches, primary_id = _validate_candidates(db, data.batch_ids, data.primary_batch_id)
    primary = next(b for b in batches if b.id == primary_id)
    code = (data.code or primary.code).strip()
    if db.scalar(select(BatchGroup.id).where(BatchGroup.code == code,
                                             BatchGroup.status == "active")):
        raise HTTPException(409, "归并组编号已存在，请更换")
    verified = data.verified_tonnes if data.verified_tonnes is not None else primary.tonnes
    group = BatchGroup(
        code=code, primary_batch_id=primary_id, verified_tonnes=verified,
        note=(data.note or "").strip(), created_by=user.email,
    )
    db.add(group)
    db.flush()
    for b in batches:
        db.add(BatchGroupMember(group_id=group.id, batch_id=b.id))
    db.flush()
    response = group_detail(db, group)
    _idem_store(db, data.idempotency_key, "merge.confirm", response)
    try:
        db.commit()
    except IntegrityError:
        # 并发下唯一键（归并组编号或幂等键）冲突：整体回滚，保持原状。
        db.rollback()
        cached = _idem_take(db, data.idempotency_key, "merge.confirm")
        if cached is not None:
            return cached, True
        raise HTTPException(409, "归并组编号已存在，请更换")
    return response, False


def dissolve(db, group_id, user):
    group = db.scalar(
        select(BatchGroup).where(BatchGroup.id == group_id).with_for_update()
    )
    if not group:
        raise HTTPException(404, "归并组不存在")
    if group.status == "dissolved":
        # 自然幂等：重复解除不重复生效，返回当前原状。
        return group_detail(db, group), True
    members = group_members(db, group.id)
    member_ids = [b.id for b in members]
    # 依赖保护：成员批号若被另一个生效归并组引用，必须先解除依赖。
    others = db.execute(
        select(BatchGroup.code)
        .join(BatchGroupMember, BatchGroupMember.group_id == BatchGroup.id)
        .where(
            BatchGroup.status == "active",
            BatchGroup.id != group.id,
            BatchGroupMember.batch_id.in_(member_ids),
        )
    ).scalars().all()
    if others:
        raise HTTPException(409, f"存在后续归并依赖，请先解除：{'、'.join(others)}")
    group.status = "dissolved"
    group.dissolved_by = user.email
    from datetime import datetime, timezone
    group.dissolved_at = datetime.now(timezone.utc)
    db.flush()
    response = group_detail(db, group)
    db.commit()
    return response, False


def attribution(db, batch_id):
    batch = db.get(Batch, batch_id)
    if not batch:
        raise HTTPException(404, "原粮批次不存在")
    membership = active_membership(db)
    links = list(db.scalars(
        select(BatchGroupMember).where(BatchGroupMember.batch_id == batch_id)
        .order_by(BatchGroupMember.id)
    ))
    history = []
    for link in links:
        group = db.get(BatchGroup, link.group_id)
        members = group_members(db, group.id)
        primary = next((b for b in members if b.id == group.primary_batch_id), None)
        history.append({
            "group_id": group.id,
            "group_code": group.code,
            "status": group.status,
            "is_primary": group.primary_batch_id == batch.id,
            "primary_code": primary.code if primary else "",
            "verified_tonnes": group.verified_tonnes,
            "member_codes": [b.code for b in members],
            "joined_at": link.joined_at.isoformat(),
            "created_at": group.created_at.isoformat(),
            "dissolved_at": group.dissolved_at.isoformat() if group.dissolved_at else None,
            "created_by": group.created_by,
            "dissolved_by": group.dissolved_by,
            "note": group.note,
        })
    active = membership.get(batch_id)
    return {
        "batch": record(batch),
        "active_group": record(active) if active else None,
        "history": history,
    }


def tonnage_summary(db, line=None):
    """归并口径吨数：生效归并组按核实吨数只计一次，其余批号各计一次。"""
    batches = list(db.scalars(select(Batch).order_by(Batch.id)))
    if line:
        batches = [b for b in batches if b.line == line]
    membership = active_membership(db)
    groups, standalone = {}, []
    for b in batches:
        g = membership.get(b.id)
        if g:
            groups.setdefault(g.id, g)
        else:
            standalone.append(b)
    gross = round(sum(b.tonnes for b in batches), 4)
    deduped = round(
        sum(g.verified_tonnes for g in groups.values())
        + sum(b.tonnes for b in standalone), 4
    )
    return {
        "gross_tonnes": gross,
        "deduped_tonnes": deduped,
        "duplicated_tonnes": round(gross - deduped, 4),
        "active_group_count": len(groups),
        "standalone_batch_count": len(standalone),
    }
