"""批次归并端到端测试：覆盖预览/确认/解除/补录/诊断冻结/导出/幂等/并发。"""
import importlib
import threading

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from src.config.database import Base, engine, Session
from src.models.entities import (
    User, Batch, Inspection, BatchGroup, BatchGroupMember, IdempotencyOp, Review,
)
from src.utils.security import password_hash

# 用 fakeredis 替换真实队列（测试环境无 Redis 服务依赖）。
# 注意：不能 import src.worker，其模块级代码包含 while True 消费循环。
from src.services import business as business_svc
business_svc.queue = fakeredis.FakeRedis(decode_responses=True)
import src.main as main_mod  # noqa: E402
main_mod.queue = business_svc.queue

PW = "Demo123!"


@pytest.fixture(scope="session", autouse=True)
def _schema():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture()
def db_clean():
    with engine.begin() as conn:
        conn.execute(text(
            "TRUNCATE users,batches,inspections,reviews,audits,"
            "batch_groups,batch_group_members,idempotency_ops RESTART IDENTITY CASCADE"
        ))
    with Session() as db:
        for role in ["admin", "analyst", "viewer"]:
            db.add(User(email=f"{role}@demo.local",
                        password_hash=password_hash(PW), role=role))
        db.commit()
    yield


@pytest.fixture()
def client(db_clean):
    with TestClient(main_mod.app) as c:
        yield c


def token(client, role):
    r = client.post("/auth/login", json={"email": f"{role}@demo.local", "password": PW})
    assert r.status_code == 200
    return r.json()["access_token"]


def auth(client, role):
    return {"Authorization": f"Bearer {token(client, role)}"}


def make_batch(db, code, line="一线", supplier="丰垦粮业", tonnes=28.0):
    b = Batch(code=code, line=line, supplier=supplier, tonnes=tonnes,
              received_date="2026-11-15")
    db.add(b)
    db.commit()
    db.refresh(b)
    return b


def add_sample(db, batch, code, y=73.0):
    db.add(Inspection(sample_code=code, batch_id=batch.id, flour_yield=y,
                      moisture=12.5, inspector="林洁"))
    db.commit()


# ---------- 预览与确认 ----------

def test_preview_members_samples_tonnes(client):
    with Session() as db:
        b1 = make_batch(db, "T-A", tonnes=28.0)
        b2 = make_batch(db, "T-B", tonnes=27.5)
        add_sample(db, b1, "T-A-S1")
        add_sample(db, b2, "T-B-S1")
        add_sample(db, b2, "T-B-S2")
    r = client.post("/merges/preview", json={"batch_ids": [b1.id, b2.id]},
                    headers=auth(client, "analyst"))
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["member_count"] == 2
    assert p["total_samples"] == 3            # 样本各计一次
    assert p["sum_member_tonnes"] == 55.5
    assert p["verified_tonnes"] == 28.0       # 默认取主批号吨数
    assert p["ready"] is True


def test_confirm_merge_counts_tonnes_once_samples_each(client):
    with Session() as db:
        b1 = make_batch(db, "M-A", tonnes=30.0)
        b2 = make_batch(db, "M-B", tonnes=29.0)
        for i in range(3):
            add_sample(db, b1, f"M-A-S{i}", 70 + i)
            add_sample(db, b2, f"M-B-S{i}", 70 + i)
    key = "m-test-key-0001"
    r = client.post("/merges/confirm", json={
        "batch_ids": [b1.id, b2.id], "idempotency_key": key,
        "verified_tonnes": 30.0,
    }, headers=auth(client, "admin"))
    assert r.status_code == 201, r.text
    g = r.json()
    assert g["total_samples"] == 6
    assert g["verified_tonnes"] == 30.0
    assert g["status"] == "active"
    # 吨数口径：归并组只计一次
    r = client.get("/tonnage", headers=auth(client, "viewer"))
    t = r.json()
    assert t["gross_tonnes"] == 59.0
    assert t["deduped_tonnes"] == 30.0
    assert t["active_group_count"] == 1
    # 批次列表带归并信息
    rows = client.get("/batches", headers=auth(client, "viewer")).json()
    tagged = {x["code"]: x for x in rows}
    assert tagged["M-A"]["group_code"] == "M-A" and tagged["M-A"]["is_primary"] is True
    assert tagged["M-B"]["is_primary"] is False
    assert tagged["M-B"]["verified_tonnes"] == 30.0


def test_merge_rejects_cross_line_and_supplier(client):
    with Session() as db:
        b1 = make_batch(db, "X-A", line="一线", supplier="丰垦粮业")
        b2 = make_batch(db, "X-B", line="二线", supplier="丰垦粮业")
        b3 = make_batch(db, "X-C", line="一线", supplier="河谷粮仓")
    r = client.post("/merges/confirm", json={
        "batch_ids": [b1.id, b2.id], "idempotency_key": "m-cross-line-01",
    }, headers=auth(client, "analyst"))
    assert r.status_code == 422
    r = client.post("/merges/confirm", json={
        "batch_ids": [b1.id, b3.id], "idempotency_key": "m-cross-sup-01",
    }, headers=auth(client, "analyst"))
    assert r.status_code == 422


def test_cannot_merge_batch_already_in_active_group(client):
    with Session() as db:
        b1 = make_batch(db, "D-A")
        b2 = make_batch(db, "D-B")
        b3 = make_batch(db, "D-C")
    h = auth(client, "analyst")
    client.post("/merges/confirm", json={"batch_ids": [b1.id, b2.id],
                                         "idempotency_key": "m-dup-01"}, headers=h)
    r = client.post("/merges/confirm", json={"batch_ids": [b2.id, b3.id],
                                             "idempotency_key": "m-dup-02"}, headers=h)
    assert r.status_code == 409


def test_viewer_cannot_write_merge(client):
    with Session() as db:
        b1 = make_batch(db, "V-A")
        b2 = make_batch(db, "V-B")
    r = client.post("/merges/confirm", json={"batch_ids": [b1.id, b2.id],
                                             "idempotency_key": "m-viewer-01"},
                    headers=auth(client, "viewer"))
    assert r.status_code == 403
    # 预览是只读，viewer 可以
    r = client.post("/merges/preview", json={"batch_ids": [b1.id, b2.id]},
                    headers=auth(client, "viewer"))
    assert r.status_code == 200


# ---------- 幂等 ----------

def test_same_idempotency_key_does_not_duplicate(client):
    with Session() as db:
        b1 = make_batch(db, "I-A")
        b2 = make_batch(db, "I-B")
    key = "m-idem-repeat-0001"
    payload = {"batch_ids": [b1.id, b2.id], "idempotency_key": key}
    h = auth(client, "analyst")
    r1 = client.post("/merges/confirm", json=payload, headers=h)
    r2 = client.post("/merges/confirm", json=payload, headers=h)
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["id"] == r2.json()["id"]
    assert r1.json()["idempotent_reused"] is False
    assert r2.json()["idempotent_reused"] is True
    with Session() as db:
        assert db.query(BatchGroup).count() == 1
        assert db.query(BatchGroupMember).count() == 2
        assert db.query(IdempotencyOp).count() == 1


# ---------- 旧批号补录与解除还原 ----------

def test_old_batch_still_accepts_inspections_and_dissolve_restores(client):
    with Session() as db:
        b1 = make_batch(db, "R-A", tonnes=20.0)
        b2 = make_batch(db, "R-B", tonnes=21.0)
        add_sample(db, b1, "R-A-S1")
        add_sample(db, b2, "R-B-S1")
    h = auth(client, "analyst")
    g = client.post("/merges/confirm", json={
        "batch_ids": [b1.id, b2.id], "idempotency_key": "m-diss-01",
    }, headers=h).json()
    # 归并期间向旧批号（成员批号）补录
    r = client.post("/inspections", json={
        "sample_code": "R-B-S2", "batch_id": b2.id,
        "flour_yield": 71.5, "moisture": 12.0, "inspector": "周麦",
    }, headers=h)
    assert r.status_code == 201
    insp = client.get("/inspections", headers=h).json()
    s2 = next(x for x in insp if x["sample_code"] == "R-B-S2")
    assert s2["entry_batch_code"] == "R-B"          # 入口批号不变
    assert s2["group_code"] == "R-A"                # 当前归属生效归并组
    # 解除归并
    r = client.post(f"/merges/{g['id']}/dissolve", headers=h)
    assert r.status_code == 200
    assert r.json()["status"] == "dissolved"
    # 再次解除：自然幂等，不重复生效
    r2 = client.post(f"/merges/{g['id']}/dissolve", headers=h)
    assert r2.status_code == 200 and r2.json()["idempotent_reused"] is True
    # 补录样本按原入口批号还原
    insp = client.get("/inspections", headers=h).json()
    s2 = next(x for x in insp if x["sample_code"] == "R-B-S2")
    assert s2["entry_batch_code"] == "R-B" and s2["group_code"] is None
    # 成员履历仍保留可追溯，吨数恢复为各批各计
    t = client.get("/tonnage", headers=h).json()
    assert t["deduped_tonnes"] == 41.0 and t["active_group_count"] == 0
    # 解除后批号可重新归并
    r = client.post("/merges/confirm", json={
        "batch_ids": [b1.id, b2.id], "idempotency_key": "m-remerge-01",
    }, headers=h)
    assert r.status_code == 201


def test_dissolve_blocked_by_dependent_active_group(client):
    with Session() as db:
        b1 = make_batch(db, "P-A")
        b2 = make_batch(db, "P-B")
        b3 = make_batch(db, "P-C")
    h = auth(client, "analyst")
    g1 = client.post("/merges/confirm", json={"batch_ids": [b1.id, b2.id],
                                               "idempotency_key": "m-dep-01"}, headers=h).json()
    # 先解除 g1，再建一个包含 b1 的新归并，然后重建 g1 形成“后续归并”依赖链
    client.post(f"/merges/{g1['id']}/dissolve", headers=h)
    g2 = client.post("/merges/confirm", json={"batch_ids": [b1.id, b3.id],
                                               "idempotency_key": "m-dep-02"}, headers=h).json()
    g1b = client.post("/merges/confirm", json={"batch_ids": [b1.id, b2.id],
                                                "idempotency_key": "m-dep-03"}, headers=h)
    # b1 已在 g2 中，重建 g1 必须先被阻断
    assert g1b.status_code == 409
    # 解除 g2（后续归并）后才能再建
    client.post(f"/merges/{g2['id']}/dissolve", headers=h)
    g1b = client.post("/merges/confirm", json={"batch_ids": [b1.id, b2.id],
                                                "idempotency_key": "m-dep-04"}, headers=h)
    assert g1b.status_code == 201


# ---------- 归属来历 ----------

def test_attribution_history(client):
    with Session() as db:
        b1 = make_batch(db, "H-A")
        b2 = make_batch(db, "H-B")
    h = auth(client, "viewer")
    ah = auth(client, "analyst")
    g = client.post("/merges/confirm", json={"batch_ids": [b1.id, b2.id],
                                             "idempotency_key": "m-hist-01"}, headers=ah).json()
    r = client.get(f"/batches/{b2.id}/attribution", headers=h)
    a = r.json()
    assert a["active_group"]["code"] == g["code"]
    assert a["history"][0]["member_codes"] == ["H-A", "H-B"]
    assert a["history"][0]["is_primary"] is False
    client.post(f"/merges/{g['id']}/dissolve", headers=ah)
    a = client.get(f"/batches/{b2.id}/attribution", headers=h).json()
    assert a["active_group"] is None
    assert a["history"][0]["status"] == "dissolved"
    assert a["history"][0]["dissolved_at"] is not None


# ---------- 诊断冻结完整归属，排队后归并变化不改依据 ----------

def test_review_freezes_attribution_and_survives_later_merge_changes(client):
    with Session() as db:
        b1 = make_batch(db, "F-A", tonnes=25.0)
        b2 = make_batch(db, "F-B", tonnes=26.0)
        for i in range(3):
            add_sample(db, b1, f"F-A-S{i}", 72 + i)
            add_sample(db, b2, f"F-B-S{i}", 72 + i)
    h = auth(client, "analyst")
    # 先提交诊断（此时未归并）
    r = client.post("/reviews", json={"title": "冻结前诊断", "line": "全部"}, headers=h)
    assert r.status_code == 202
    rid = r.json()["id"]
    # 排队期间发生归并
    g = client.post("/merges/confirm", json={
        "batch_ids": [b1.id, b2.id], "idempotency_key": "m-freeze-01",
        "verified_tonnes": 25.0,
    }, headers=h).json()
    # 模拟 worker 计算（基于冻结快照）
    with Session() as db:
        rv = db.get(Review, rid)
        from src.services.statistics import diagnose
        rv.result = diagnose(rv.snapshot)
        rv.status = "completed"
        db.commit()
        snap_codes = {(x["sample_code"]): x for x in rv.snapshot}
    # 快照保留完整归属：入口批号 + 提交当时（无）归并组
    assert snap_codes["F-B-S0"]["entry_batch_code"] == "F-B"
    assert snap_codes["F-B-S0"]["group_code"] is None
    # context 是提交时刻口径：独立批号两批吨数
    full = client.get(f"/reviews/{rid}", headers=h).json()
    assert full["context"]["sample_count"] == 6
    assert full["context"]["deduped_tonnes"] == 51.0
    assert full["context"]["groups"] == []
    # 再提交一次诊断，这次应带上归并口径
    r2 = client.post("/reviews", json={"title": "归并后诊断", "line": "全部"}, headers=h)
    full2 = client.get(f"/reviews/{r2.json()['id']}", headers=h).json()
    assert full2["context"]["groups"][0]["group_code"] == g["code"]
    assert full2["context"]["deduped_tonnes"] == 25.0
    snap2 = {x["sample_code"]: x for x in full2["snapshot"]}
    assert snap2["F-B-S0"]["group_code"] == g["code"]
    assert snap2["F-B-S0"]["entry_batch_code"] == "F-B"


def test_export_keeps_entry_batch_and_group_of_snapshot(client):
    with Session() as db:
        b1 = make_batch(db, "E-A", tonnes=22.0)
        b2 = make_batch(db, "E-B", tonnes=23.0)
        for i in range(3):
            add_sample(db, b1, f"E-A-S{i}", 71 + i)
            add_sample(db, b2, f"E-B-S{i}", 71 + i)
    h = auth(client, "analyst")
    client.post("/merges/confirm", json={"batch_ids": [b1.id, b2.id],
                                         "idempotency_key": "m-export-01"}, headers=h)
    r = client.post("/reviews", json={"title": "导出诊断", "line": "全部"}, headers=h)
    rid = r.json()["id"]
    with Session() as db:
        rv = db.get(Review, rid)
        from src.services.statistics import diagnose
        rv.result = diagnose(rv.snapshot)
        rv.status = "completed"
        db.commit()
    r = client.get(f"/reviews/{rid}/export", headers=auth(client, "viewer"))
    assert r.status_code == 200
    body = r.content.decode("utf-8-sig")
    assert "录入入口批号" in body and "当时归并组" in body
    assert "E-B" in body and "E-A" in body          # 原批号保留
    assert "核实吨数(只计一次)" in body
    # 表头样本行包含入口批号与归并组两列
    assert "E-B-S0,E-B,E-A," in body


# ---------- 并发 ----------

def test_concurrent_merges_serialize_no_cross_batch_or_loss(client):
    with Session() as db:
        b1 = make_batch(db, "C-A")
        b2 = make_batch(db, "C-B")
        b3 = make_batch(db, "C-C")
        for b in (b1, b2, b3):
            for i in range(2):
                add_sample(db, b, f"{b.code}-S{i}")
    h = auth(client, "analyst")
    results = []

    def merge(ids, key):
        local = TestClient(main_mod.app)
        local.headers.update(h)
        rr = local.post("/merges/confirm",
                        json={"batch_ids": ids, "idempotency_key": key})
        results.append((rr.status_code, rr.json()))

    t1 = threading.Thread(target=merge, args=([b1.id, b2.id], "m-conc-01"))
    t2 = threading.Thread(target=merge, args=([b2.id, b3.id], "m-conc-02"))
    t1.start(); t2.start(); t1.join(); t2.join()
    statuses = sorted(s for s, _ in results)
    assert statuses == [201, 409]                   # 一个成功，一个被冲突阻断，无串批
    with Session() as db:
        assert db.query(BatchGroup).filter(BatchGroup.status == "active").count() == 1
        assert db.query(BatchGroupMember).count() == 2
        assert db.query(Inspection).count() == 6   # 检验记录无丢失


def test_concurrent_inspection_during_merge_never_lost(client):
    with Session() as db:
        b1 = make_batch(db, "N-A")
        b2 = make_batch(db, "N-B")
        add_sample(db, b1, "N-A-S0")
    h = auth(client, "analyst")
    barrier = threading.Barrier(2)
    errors = []

    def do_merge():
        try:
            barrier.wait()
            c = TestClient(main_mod.app)
            c.post("/merges/confirm",
                   json={"batch_ids": [b1.id, b2.id], "idempotency_key": "m-race-ins-01"},
                   headers=h)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    def do_inspection():
        try:
            barrier.wait()
            c = TestClient(main_mod.app)
            c.post("/inspections", json={
                "sample_code": "N-A-S1", "batch_id": b1.id,
                "flour_yield": 70.0, "moisture": 12.0, "inspector": "林洁",
            }, headers=h)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    t1 = threading.Thread(target=do_merge)
    t2 = threading.Thread(target=do_inspection)
    t1.start(); t2.start(); t1.join(); t2.join()
    assert not errors
    with Session() as db:
        assert db.query(Inspection).filter(Inspection.sample_code == "N-A-S1").count() == 1
        # 补录样本落在入口批号且能在归并组中查到
        row = db.query(Inspection).filter_by(sample_code="N-A-S1").one()
        assert row.batch_id == b1.id
    insp = client.get("/inspections", headers=h).json()
    s1 = next(x for x in insp if x["sample_code"] == "N-A-S1")
    assert s1["entry_batch_code"] == "N-A" and s1["group_code"] == "N-A"


def test_failed_merge_rolls_back_unchanged(client):
    with Session() as db:
        b1 = make_batch(db, "Z-A")
        b2 = make_batch(db, "Z-B", line="二线")  # 跨线，必失败
    before = client.get("/tonnage", headers=auth(client, "viewer")).json()
    r = client.post("/merges/confirm", json={
        "batch_ids": [b1.id, b2.id], "idempotency_key": "m-fail-01",
    }, headers=auth(client, "analyst"))
    assert r.status_code == 422
    with Session() as db:
        assert db.query(BatchGroup).count() == 0
        assert db.query(IdempotencyOp).count() == 0   # 失败不落幂等记录，可修正后重试
    after = client.get("/tonnage", headers=auth(client, "viewer")).json()
    assert before == after
    # 修正后同键可成功（失败保持原状，不占用幂等键）
    with Session() as db:
        db.query(Batch).filter_by(id=b2.id).update({"line": "一线"})
        db.commit()
    db.close()
    r = client.post("/merges/confirm", json={
        "batch_ids": [b1.id, b2.id], "idempotency_key": "m-fail-01",
    }, headers=auth(client, "analyst"))
    assert r.status_code == 201
