from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException
from redis import Redis
from src.config.settings import REDIS_URL
from src.models.entities import Batch, Inspection, Review
from src.services.merges import active_membership
from src.utils.serialize import record

queue = Redis.from_url(REDIS_URL, decode_responses=True)


def batches(db):
    rows = db.execute(
        select(Batch, func.count(Inspection.id))
        .outerjoin(Inspection, Inspection.batch_id == Batch.id)
        .group_by(Batch.id).order_by(Batch.id.desc())
    ).all()
    membership = active_membership(db)
    out = []
    for b, n in rows:
        g = membership.get(b.id)
        out.append({
            **record(b), 'samples': n,
            'group_id': g.id if g else None,
            'group_code': g.code if g else None,
            'group_status': g.status if g else None,
            'is_primary': g.primary_batch_id == b.id if g else False,
            'verified_tonnes': g.verified_tonnes if g else None,
        })
    return out


def create(db, model, data):
    obj = model(**data)
    db.add(obj)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, '编号已存在，请使用独立编号')
    return record(obj)


def inspections(db, batch_id=None):
    """检验记录（追加留痕）。batch_code/batch_id 始终为录入入口批号；
    group_* 为当前生效归并口径，历史快照由 Review.snapshot 冻结，不受后续归并影响。"""
    q = (
        select(Inspection, Batch.code, Batch.line, Batch.supplier)
        .join(Batch, Batch.id == Inspection.batch_id)
        .order_by(Inspection.id.desc())
    )
    if batch_id:
        q = q.where(Inspection.batch_id == batch_id)
    membership = active_membership(db)
    out = []
    for i, code, line, supplier in db.execute(q):
        g = membership.get(i.batch_id)
        out.append({
            **record(i),
            'entry_batch_id': i.batch_id,
            'entry_batch_code': code,
            'batch_code': code,  # 兼容既有字段：录入入口批号
            'line': line,
            'supplier': supplier,
            'group_id': g.id if g else None,
            'group_code': g.code if g else None,
        })
    return out


def add_inspection(db, data):
    batch = db.get(Batch, data['batch_id'])
    if not batch:
        raise HTTPException(404, '原粮批次不存在')
    # 行锁串行化与并发归并：若批号正被归并事务锁定，本事务等待后按最新状态写入，
    # 检验行始终挂在录入入口批号上，归并/解除都不会移动或丢失该记录。
    db.execute(select(Batch.id).where(Batch.id == batch.id).with_for_update())
    return create(db, Inspection, data)


def _line_group_context(db, rows, line):
    """提交时刻的归并口径快照：归并组吨数只计一次，样本各计一次。"""
    membership = active_membership(db)
    groups, standalone_ids = {}, set()
    group_batch_ids = {}
    for r in rows:
        g = membership.get(r['batch_id'])
        if g:
            groups.setdefault(g.id, g)
            group_batch_ids.setdefault(g.id, set()).add(r['batch_id'])
        else:
            standalone_ids.add(r['batch_id'])
    standalone_tonnes = db.scalar(
        select(func.coalesce(func.sum(Batch.tonnes), 0.0)).where(Batch.id.in_(standalone_ids))
    ) if standalone_ids else 0.0
    items = [
        {
            'group_id': g.id, 'group_code': g.code,
            'primary_batch_id': g.primary_batch_id,
            'member_batch_ids': sorted(group_batch_ids.get(g.id, set())),
            'verified_tonnes': g.verified_tonnes,
            'sample_count': sum(
                1 for r in rows if r['batch_id'] in group_batch_ids.get(g.id, set())
            ),
        }
        for g in groups.values()
    ]
    return {
        'scope_line': line,
        'groups': items,
        'standalone_batch_ids': sorted(standalone_ids),
        'standalone_tonnes': round(float(standalone_tonnes), 4),
        'deduped_tonnes': round(
            sum(g.verified_tonnes for g in groups.values()) + float(standalone_tonnes), 4
        ),
        'sample_count': len(rows),
    }


def submit(db, data, user):
    rows = inspections(db)
    if data['line'] != '全部':
        rows = [x for x in rows if x['line'] == data['line']]
    if len(rows) < 5:
        raise HTTPException(422, '该范围至少需要 5 条检验记录，请先录入')
    context = _line_group_context(db, rows, data['line'])
    obj = Review(
        **data, created_by=user.email, snapshot=rows, context=context,
    )
    db.add(obj)
    db.commit()
    try:
        queue.rpush('mill:jobs', obj.id)
    except Exception:
        obj.status = 'failed'
        obj.note = '队列不可用，请恢复后重新提交'
        db.commit()
        raise HTTPException(503, obj.note)
    return record(obj)


def review(db, id):
    obj = db.get(Review, id)
    if not obj:
        raise HTTPException(404, '复核单不存在')
    return obj


def approve(db, id, note, user):
    obj = review(db, id)
    if obj.status != 'completed':
        raise HTTPException(409, '只有已计算且未签核的复核单可以签核')
    obj.status = 'approved'
    obj.note = note
    obj.approved_by = user.email
    db.commit()
    return record(obj)
