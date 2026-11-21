"""End-to-end domain tests for persistent ledger, quota orders and signing.

Run with GB5311_TEST_DATABASE_URL pointing at a PostgreSQL 16 database whose
role may create TEMP tables, e.g.

    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=backend \
      python -m pytest -q -p no:cacheprovider test-artifacts/gb-5311/tests/test_domain.py
"""

import os

import pytest
from sqlalchemy import create_engine, func, select

from src.constants.workflow import QuotaLineStatus, QuotaOrderStatus, Role
from src.models.tables import (audit, cases, cloth_ledger, evidence,
                               outbound_adjustments, outbound_batches,
                               quota_lines, quota_orders, runs, schema, users)
from src.services import cloth_ledger as ledger
from src.services import domain, workflow
from src.services.security import hash_password

DB_URL = os.getenv('GB5311_TEST_DATABASE_URL')
pytestmark = pytest.mark.skipif(not DB_URL, reason='GB5311_TEST_DATABASE_URL 未设置')

BOOK, CANCEL = domain.BOOK, domain.CANCEL

OPENING = [
    {'batch_id': 'BL-101', 'revision': 1, 'grade': 'A级', 'weight_g': 18000, 'pieces': 12},
    {'batch_id': 'BL-102', 'revision': 1, 'grade': 'A级', 'weight_g': 12000, 'pieces': 8},
    {'batch_id': 'BL-201', 'revision': 1, 'grade': 'B级', 'weight_g': 16000, 'pieces': 11},
    {'batch_id': 'BL-301', 'revision': 1, 'grade': 'C级', 'weight_g': 9000, 'pieces': 6},
    {'batch_id': 'BL-501', 'revision': 1, 'grade': 'D级', 'weight_g': 3000, 'pieces': 2},
]
QUOTAS = [
    {'grade': 'A级', 'capacity_g': 23000, 'capacity_pieces': 12},
    {'grade': 'B级', 'capacity_g': 17000, 'capacity_pieces': 15},
    {'grade': 'C级', 'capacity_g': 10000, 'capacity_pieces': 8},
    {'grade': 'D级', 'capacity_g': 5000, 'capacity_pieces': 4},
]


def base_payload(**overrides):
    payload = {'opening_batches': [dict(row) for row in OPENING],
               'revisions': [], 'quotas': [dict(row) for row in QUOTAS]}
    payload.update(overrides)
    return payload


@pytest.fixture(scope='session')
def engine():
    eng = create_engine(DB_URL)
    schema.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def clean(engine):
    with engine.begin() as conn:
        for table in (outbound_adjustments, outbound_batches, quota_lines,
                      quota_orders, cloth_ledger, evidence, runs, cases,
                      users, audit):
            conn.execute(table.delete())
    yield


@pytest.fixture()
def actors(engine):
    with engine.begin() as conn:
        for role in Role:
            conn.execute(users.insert().values(
                username=f'test-{role.value}', role=role.value,
                password_hash=hash_password('pw')))
    return {role.value: f'test-{role.value}' for role in Role}


def case_and_run(engine, payload):
    """Simulate case creation and one worker run, returning (case_id, run_id, report)."""
    with engine.begin() as conn:
        case_id = conn.execute(cases.insert().values(
            slug=f'case-{os.urandom(6).hex()}', title='测试档案',
            payload=payload, author='test-operator',
            created_at='2026-11-21T08:00:00Z')).inserted_primary_key[0]
        run_id = conn.execute(runs.insert().values(
            case_id=case_id, status='queued', owner='test-operator',
            created_at='2026-11-21T08:00:00Z')).inserted_primary_key[0]
    report = domain.solve(payload, engine, case_id=case_id, run_id=run_id)
    with engine.begin() as conn:
        conn.execute(evidence.insert().values(
            run_id=run_id, report=report, digest='d' * 64))
    return case_id, run_id, report


def cuts_of(report):
    return next(section['rows'] for section in report['sections']
                if section['title'] == '建议转裁批次')


def metric(report, name):
    return report['metrics'][name]


# ---------- 原始单档案演示流程保持不变 ----------

def test_standalone_run_reproduces_demo(engine, clean):
    report = domain.solve(domain.SPEC['sample'], engine)
    for key, value in domain.SPEC['expect'].items():
        assert report['metrics'][key] == value
    assert metric(report, '库存差异等级数') == 0
    assert [row['批次'] for row in cuts_of(report)] == ['BL-102', 'BL-101']


def test_opening_run_persists_ledger_and_stock(engine, clean, actors):
    case_id, _, report = case_and_run(engine, base_payload())
    assert metric(report, '库存差异等级数') == 0
    with engine.connect() as conn:
        stock = ledger.stock_by_grade(conn, case_id)
        assert stock['A级'] == {'grade': 'A级', 'weight_g': 30000, 'pieces': 20}
        assert stock['B级'] == {'grade': 'B级', 'weight_g': 16000, 'pieces': 11}
        assert len(ledger.current_versions(conn, case_id)) == 5


# ---------- 需求1：同一档案续收复秤、重算仓位配额 ----------

def test_same_case_accepts_follow_up_reweigh_and_recomputes(engine, clean, actors):
    # 期初即超额的配额，首跑就会产生转裁建议。
    payload = base_payload()
    case_id, _, first = case_and_run(engine, payload)
    assert {row['批次'] for row in cuts_of(first)} == {'BL-101'}

    reweigh = [{'batch_id': 'BL-301', 'revision': 2, 'action': BOOK,
                'grade': 'C级', 'weight_g': 7000, 'pieces': 6}]
    run_id = workflow.append_revisions(case_id, reweigh, actors['operator'])['id']
    report = domain.solve({**payload, 'revisions': reweigh}, engine,
                          case_id=case_id, run_id=run_id)
    assert metric(report, '生效修订数') == 1
    with engine.connect() as conn:
        stock = ledger.stock_by_grade(conn, case_id)
        assert stock['C级'] == {'grade': 'C级', 'weight_g': 7000, 'pieces': 6}
        versions = ledger.current_versions(conn, case_id)
        bl301 = next(row for row in versions if row['batch_id'] == 'BL-301')
        assert bl301['revision'] == 2 and bl301['weight_g'] == 7000
        # 旧版本仍保留在修订表（含过期版本）。
        revisions = conn.execute(select(cloth_ledger.c.revision).where(
            cloth_ledger.c.case_id == case_id,
            cloth_ledger.c.batch_id == 'BL-301')).scalars().all()
        assert revisions == [1, 2]


def test_stale_and_duplicate_revisions_kept_in_ledger(engine, clean, actors):
    case_id, _, _ = case_and_run(engine, base_payload())
    events = [
        {'batch_id': 'BL-101', 'revision': 3, 'action': BOOK,
         'grade': 'B级', 'weight_g': 20000, 'pieces': 13},
        # 迟到的旧版：过期但保留身份。
        {'batch_id': 'BL-101', 'revision': 2, 'action': BOOK,
         'grade': 'A级', 'weight_g': 17500, 'pieces': 11},
        # 同版原样重传：重复。
        {'batch_id': 'BL-101', 'revision': 3, 'action': BOOK,
         'grade': 'B级', 'weight_g': 20000, 'pieces': 13},
    ]
    run_id = workflow.append_revisions(case_id, events, actors['operator'])['id']
    report = domain.solve({**base_payload(), 'revisions': events}, engine,
                          case_id=case_id, run_id=run_id)
    assert metric(report, '生效修订数') == 1
    assert metric(report, '过期修订数') == 1
    assert metric(report, '重复修订数') == 1
    with engine.connect() as conn:
        stock = ledger.stock_by_grade(conn, case_id)
        # BL-101 从 A级 整批改到 B级（20000 克/13 件），BL-102 仍留在 A级。
        assert stock['A级'] == {'grade': 'A级', 'weight_g': 12000, 'pieces': 8}
        assert stock['B级'] == {'grade': 'B级', 'weight_g': 36000, 'pieces': 24}


# ---------- 需求2：配额单签收整批布，从下次在库量扣除 ----------

def _open_order_for_cuts(engine, case_id, evidence_id, report, actor):
    with engine.begin() as conn:
        order_id = ledger.create_order(conn, case_id, evidence_id, cuts_of(report), actor)
    return order_id


def test_signed_whole_batch_is_deducted_from_next_on_hand(engine, clean, actors):
    # 用演示样例制造两条转裁建议：BL-102（A级超件）与 BL-101 v3（B级超重）。
    case_id, run_id, report = case_and_run(engine, domain.SPEC['sample'])
    assert [row['批次'] for row in cuts_of(report)] == ['BL-102', 'BL-101']
    with engine.begin() as conn:
        evidence_id = conn.execute(select(evidence.c.id).where(
            evidence.c.run_id == run_id)).scalar_one()
    order_id = _open_order_for_cuts(engine, case_id, evidence_id, report, actors['reviewer'])

    detail = workflow.quota_order_detail(order_id)
    lines = detail['lines']
    bl102 = next(line for line in lines if line['batch_id'] == 'BL-102')
    workflow.sign_quota_line(order_id, bl102['id'], '实物核对一致', actors['viewer'])

    # 重算：BL-102 整批已出库，A 级在库量应扣除 12000 克 / 8 件。
    # 演示样例中 BL-101 v3 已转到 B级，A 级原本只有 BL-102 与 BL-401(8000/5)。
    rerun = domain.solve(domain.SPEC['sample'], engine, case_id=case_id)
    with engine.connect() as conn:
        stock = ledger.stock_by_grade(conn, case_id)
        assert stock['A级'] == {'grade': 'A级', 'weight_g': 8000, 'pieces': 5}
        shipped, _ = ledger.outbound_reconciliation(conn, case_id)
        assert [(row['batch_id'], row['revision']) for row in shipped] == [('BL-102', 1)]
    assert any(section['title'] == '已出库批次（在库量已扣除）'
               for section in rerun['sections'])


# ---------- 需求3：同批两张单，先签生效，另一张可签未变动批次 ----------

def test_two_orders_same_batch_first_sign_wins(engine, clean, actors):
    case_id, run_id, report = case_and_run(engine, domain.SPEC['sample'])
    with engine.begin() as conn:
        evidence_id = conn.execute(select(evidence.c.id).where(
            evidence.c.run_id == run_id)).scalar_one()
    with engine.begin() as conn:
        order_a = ledger.create_order(conn, case_id, evidence_id, cuts_of(report), actors['reviewer'])
        order_b = ledger.create_order(conn, case_id, evidence_id, cuts_of(report), actors['reviewer'])

    line_a = next(line for line in workflow.quota_order_detail(order_a)['lines']
                  if line['batch_id'] == 'BL-102')
    line_b = next(line for line in workflow.quota_order_detail(order_b)['lines']
                  if line['batch_id'] == 'BL-102')

    workflow.sign_quota_line(order_a, line_a['id'], '第一单签收', actors['viewer'])
    # 第二单再签同一批次版本：不重复扣减，本行标记为重复签收（接口正常返回，页面可查）。
    duplicate = workflow.sign_quota_line(order_b, line_b['id'], '第二单签收', actors['viewer'])
    assert duplicate['status'] == '重复签收'
    assert duplicate['signed_order_id'] == order_a
    with engine.connect() as conn:
        assert conn.execute(select(func.count())
                            .select_from(outbound_batches)).scalar_one() == 1
        refreshed = next(line for line in ledger.get_order(conn, order_b)['lines']
                         if line['id'] == line_b['id'])
        assert refreshed['status'] == QuotaLineStatus.DUPLICATE.value

    # 第二张单仍可签收未变动的另一条批次 BL-101 v3。
    other_b = next(line for line in workflow.quota_order_detail(order_b)['lines']
                   if line['batch_id'] == 'BL-101')
    outcome = workflow.sign_quota_line(order_b, other_b['id'], '签未变动批次', actors['viewer'])
    assert outcome['status'] == '已签收'
    with engine.connect() as conn:
        assert conn.execute(select(func.count())
                            .select_from(outbound_batches)).scalar_one() == 2
        order_b_row = ledger.get_order(conn, order_b)['order']
        assert order_b_row['status'] == QuotaOrderStatus.CLOSED.value


# ---------- 需求4：签收与复秤并发，换修订号的批次要重新开单 ----------

def test_concurrent_reweigh_marks_line_superseded_and_requires_new_order(engine, clean, actors):
    case_id, run_id, report = case_and_run(engine, domain.SPEC['sample'])
    with engine.begin() as conn:
        evidence_id = conn.execute(select(evidence.c.id).where(
            evidence.c.run_id == run_id)).scalar_one()
        order_id = ledger.create_order(conn, case_id, evidence_id, cuts_of(report), actors['reviewer'])
    line = next(line for line in workflow.quota_order_detail(order_id)['lines']
                if line['batch_id'] == 'BL-102')

    # 复秤在签收前后到达：BL-102 升到第 2 版，且复秤后仍然超重，需要重新开单。
    reweigh = [{'batch_id': 'BL-102', 'revision': 2, 'action': BOOK,
                'grade': 'A级', 'weight_g': 20000, 'pieces': 9}]
    new_run = workflow.append_revisions(case_id, reweigh, actors['operator'])['id']
    domain.solve({**domain.SPEC['sample'], 'revisions': reweigh}, engine,
                 case_id=case_id, run_id=new_run)

    # 冻结在第 1 版的配额行直接判失效，不能签收。
    with pytest.raises(Exception) as exc:
        workflow.sign_quota_line(order_id, line['id'], '尝试签收旧版', actors['viewer'])
    assert getattr(exc.value, 'status_code', None) == 409
    with engine.connect() as conn:
        refreshed = next(row for row in ledger.get_order(conn, order_id)['lines']
                         if row['id'] == line['id'])
        assert refreshed['status'] == QuotaLineStatus.SUPERSEDED.value
        # 旧版未出库，在库按新版本计算：BL-102 v2(20000) + BL-401(8000)。
        stock = ledger.stock_by_grade(conn, case_id)
        assert stock['A级']['weight_g'] == 28000
        assert stock['A级']['pieces'] == 14

    # 重算后新证据里出现针对第 2 版的转裁建议，可重新开单并成功签收。
    rerun = domain.solve({**domain.SPEC['sample'], 'revisions': reweigh}, engine,
                         case_id=case_id)
    with engine.begin() as conn:
        new_evidence = conn.execute(evidence.insert().values(
            run_id=new_run, report=rerun, digest='e' * 64)).inserted_primary_key[0]
        new_order = ledger.create_order(conn, case_id, new_evidence,
                                        cuts_of(rerun), actors['reviewer'])
    detail = workflow.quota_order_detail(new_order)
    new_line = next(row for row in detail['lines'] if row['batch_id'] == 'BL-102')
    assert new_line['revision'] == 2
    outcome = workflow.sign_quota_line(new_order, new_line['id'], '按新版签收', actors['viewer'])
    assert outcome['status'] == '已签收'


# ---------- 需求5：已出库批次补传复秤，只修正出库对账 ----------

def test_late_reweigh_after_outbound_only_touches_reconciliation(engine, clean, actors):
    case_id, run_id, report = case_and_run(engine, domain.SPEC['sample'])
    with engine.begin() as conn:
        evidence_id = conn.execute(select(evidence.c.id).where(
            evidence.c.run_id == run_id)).scalar_one()
        order_id = ledger.create_order(conn, case_id, evidence_id, cuts_of(report), actors['reviewer'])
    bl102 = next(line for line in workflow.quota_order_detail(order_id)['lines']
                 if line['batch_id'] == 'BL-102')
    workflow.sign_quota_line(order_id, bl102['id'], '签收', actors['viewer'])

    before = domain.solve(domain.SPEC['sample'], engine, case_id=case_id)
    with engine.connect() as conn:
        stock_before = ledger.stock_by_grade(conn, case_id)

    late = [{'batch_id': 'BL-102', 'revision': 2, 'action': BOOK,
             'grade': 'A级', 'weight_g': 10500, 'pieces': 7}]
    late_run = workflow.append_revisions(case_id, late, actors['operator'])['id']
    after = domain.solve({**domain.SPEC['sample'], 'revisions': late}, engine,
                         case_id=case_id, run_id=late_run)

    with engine.connect() as conn:
        # 在库量完全不变。
        assert ledger.stock_by_grade(conn, case_id) == stock_before
        _, adjustments = ledger.outbound_reconciliation(conn, case_id)
        assert len(adjustments) == 1
        row = adjustments[0]
        assert row['outbound_revision'] == 1 and row['revision'] == 2
        assert row['weight_g'] == 10500
        # 补传事件不进在库账本。
        assert conn.execute(select(func.count()).select_from(
            cloth_ledger).where(cloth_ledger.c.batch_id == 'BL-102',
                                cloth_ledger.c.revision == 2)).scalar_one() == 0
    assert metric(after, '出库后补传数') == 1
    assert any(section['title'] == '出库后补传复秤（仅修正出库对账）'
               for section in after['sections'])
    # 重复补传同一调整：记为重复，不产生第二条。
    again = domain.solve({**domain.SPEC['sample'], 'revisions': late}, engine,
                         case_id=case_id)
    with engine.connect() as conn:
        _, adjustments = ledger.outbound_reconciliation(conn, case_id)
        assert len(adjustments) == 1
    assert metric(again, '重复修订数') == 0 and metric(again, '出库后补传数') == 1


# ---------- 需求4b：签收与复秤真正并发（多事务） ----------

def test_concurrent_sign_and_reweigh_under_real_transactions(engine, clean, actors):
    """两个并发事务：一方签收旧版、另一方登记新版修订，结果必须一致且不重复扣减。"""
    import threading

    case_id, run_id, report = case_and_run(engine, domain.SPEC['sample'])
    with engine.begin() as conn:
        evidence_id = conn.execute(select(evidence.c.id).where(
            evidence.c.run_id == run_id)).scalar_one()
        order_id = ledger.create_order(conn, case_id, evidence_id, cuts_of(report), actors['reviewer'])
    line_id = next(row for row in workflow.quota_order_detail(order_id)['lines']
                   if row['batch_id'] == 'BL-102')['id']
    reweigh = {'batch_id': 'BL-102', 'revision': 2, 'action': BOOK,
               'grade': 'A级', 'weight_g': 20000, 'pieces': 9}

    sign_outcome, reweigh_outcome, barrier = {}, {}, threading.Barrier(2)

    def sign_worker():
        barrier.wait()
        try:
            with engine.begin() as conn:
                sign_outcome['result'] = ledger.sign_line(
                    conn, order_id, line_id, actors['viewer'], '并发签收')
        except Exception as exc:  # noqa: BLE001 - 锁等待超时等意外才会进这里
            sign_outcome['error'] = exc

    def reweigh_worker():
        barrier.wait()
        try:
            with engine.begin() as conn:
                reweigh_outcome['result'] = ledger.register_revision(
                    conn, case_id, reweigh)
        except Exception as exc:  # noqa: BLE001
            reweigh_outcome['error'] = exc

    threads = [threading.Thread(target=sign_worker), threading.Thread(target=reweigh_worker)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert 'error' not in reweigh_outcome
    with engine.connect() as conn:
        outbound = conn.execute(select(func.count()).select_from(outbound_batches)
                                .where(outbound_batches.c.batch_id == 'BL-102')).scalar_one()
        line = next(row for row in ledger.get_order(conn, order_id)['lines']
                    if row['id'] == line_id)
    # 出库记录最多一条：要么旧版签走（已签收），要么新版抢先（版本失效，行状态已落库）。
    assert outbound in (0, 1)
    assert line['status'] in (QuotaLineStatus.SIGNED.value,
                              QuotaLineStatus.SUPERSEDED.value)
    if line['status'] == QuotaLineStatus.SIGNED.value:
        assert outbound == 1
        assert sign_outcome['result']['status'] == '已签收'
    else:
        assert outbound == 0
        assert sign_outcome['result']['status'] == '版本失效'


# ---------- 需求6：签收失败重试不重复扣减 ----------

def test_sign_retry_is_idempotent(engine, clean, actors):
    case_id, run_id, report = case_and_run(engine, domain.SPEC['sample'])
    with engine.begin() as conn:
        evidence_id = conn.execute(select(evidence.c.id).where(
            evidence.c.run_id == run_id)).scalar_one()
        order_id = ledger.create_order(conn, case_id, evidence_id, cuts_of(report), actors['reviewer'])
    line = next(row for row in workflow.quota_order_detail(order_id)['lines']
                if row['batch_id'] == 'BL-102')

    first = workflow.sign_quota_line(order_id, line['id'], '首次签收', actors['viewer'])
    assert first['status'] == '已签收'
    # 模拟客户端超时后原样重试。
    retry = workflow.sign_quota_line(order_id, line['id'], '首次签收', actors['viewer'])
    assert retry['status'] == '重复签收'
    with engine.connect() as conn:
        outbound_count = conn.execute(select(func.count())
                                      .select_from(outbound_batches)).scalar_one()
        assert outbound_count == 1
        signed = next(row for row in ledger.get_order(conn, order_id)['lines']
                      if row['id'] == line['id'])
        assert signed['status'] == QuotaLineStatus.SIGNED.value
        assert signed['signed_by'] == actors['viewer']


def test_ledger_identity_conflict_pauses_run(engine, clean, actors):
    case_id, _, _ = case_and_run(engine, base_payload())
    good = [{'batch_id': 'BL-101', 'revision': 2, 'action': BOOK,
             'grade': 'A级', 'weight_g': 17000, 'pieces': 11}]
    workflow.append_revisions(case_id, good, actors['operator'])
    first = domain.solve({**base_payload(), 'revisions': good}, engine, case_id=case_id)
    assert metric(first, '生效修订数') == 1
    # 同一 (批次, 修订号) 身份但内容不同：冲突，暂停发单。
    bad = [{'batch_id': 'BL-101', 'revision': 2, 'action': BOOK,
            'grade': 'A级', 'weight_g': 9999, 'pieces': 11}]
    report = domain.solve({**base_payload(), 'revisions': bad}, engine, case_id=case_id)
    assert '出现不同内容' in report['summary']
    assert report['metrics']['版本冲突数'] == 1


# ---------- 需求7：配额单页面可查实际签走的是哪一版 ----------

def test_quota_order_detail_exposes_signed_versions(engine, clean, actors):
    case_id, run_id, report = case_and_run(engine, domain.SPEC['sample'])
    with engine.begin() as conn:
        evidence_id = conn.execute(select(evidence.c.id).where(
            evidence.c.run_id == run_id)).scalar_one()
        order_id = ledger.create_order(conn, case_id, evidence_id, cuts_of(report), actors['reviewer'])
    line = next(row for row in workflow.quota_order_detail(order_id)['lines']
                if row['batch_id'] == 'BL-101')
    workflow.sign_quota_line(order_id, line['id'], '按修订号核对', actors['viewer'])

    detail = workflow.quota_order_detail(order_id)
    assert detail['case_title'] == '测试档案'
    signed = next(row for row in detail['lines'] if row['batch_id'] == 'BL-101')
    assert signed['revision'] == 3
    assert signed['status'] == QuotaLineStatus.SIGNED.value
    assert signed['signed_by'] == actors['viewer']
    assert signed['signed_at']
    pending = next(row for row in detail['lines'] if row['batch_id'] == 'BL-102')
    assert pending['status'] == QuotaLineStatus.PENDING.value
    assert detail['order']['status'] == QuotaOrderStatus.PARTIAL.value
    # 列表接口同样可查。
    listed = workflow.list_quota_orders(case_id)
    assert len(listed) == 1 and listed[0]['order']['id'] == order_id
