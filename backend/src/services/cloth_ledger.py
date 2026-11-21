"""Persistent per-case cloth ledger, outbound records and quota orders.

Unlike the per-run temporary tables in ``cloth_stock`` (which still produce the
run evidence), the ledger survives across runs so the same case can keep
accepting re-weigh revisions, recompute stock and issue quota orders.
"""

from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError

from src.constants.workflow import QuotaLineStatus, QuotaOrderStatus
from src.models.tables import (cloth_ledger, outbound_adjustments,
                               outbound_batches, quota_lines, quota_orders)
from src.utils.time import utc_time

BOOK = '记账'
CANCEL = '撤销'

LEDGER_KEYS = ('action', 'grade', 'weight_g', 'pieces')


class LedgerConflict(Exception):
    """Same (batch, revision) identity registered with different content."""


class OrderClosed(Exception):
    """Quota order is fully settled and no longer accepts signatures."""


def next_sequence(conn, case_id):
    value = conn.execute(select(func.coalesce(func.max(cloth_ledger.c.sequence), 0))
                         .where(cloth_ledger.c.case_id == case_id)).scalar_one()
    return value + 1


def _identity_collision(conn, table, case_id, event, identity_fields):
    conditions = [table.c.case_id == case_id]
    for field in identity_fields:
        conditions.append(getattr(table.c, field) == event[field])
    return conn.execute(select(table).where(*conditions)).mappings().one()


def register_revision(conn, case_id, event, source_run_id=None):
    """Append one revision event to the persistent ledger.

    Returns ``生效`` / ``重复`` / ``过期``. A same-identity/different-content
    conflict raises :class:`LedgerConflict`. All identities, including stale
    ones, are kept in the ledger.
    """
    sequence = next_sequence(conn, case_id)
    savepoint = conn.begin_nested()
    try:
        conn.execute(cloth_ledger.insert().values(
            case_id=case_id, sequence=sequence, source_run_id=source_run_id,
            registered_at=utc_time(), **event))
    except IntegrityError:
        savepoint.rollback()
        stored = _identity_collision(conn, cloth_ledger, case_id, event,
                                     ('batch_id', 'revision'))
        if all(stored[key] == event[key] for key in LEDGER_KEYS):
            return '重复'
        raise LedgerConflict(dict(event), dict(stored))
    previous = conn.execute(select(cloth_ledger).where(
        cloth_ledger.c.case_id == case_id,
        cloth_ledger.c.batch_id == event['batch_id'],
        cloth_ledger.c.revision != event['revision'])
        .order_by(cloth_ledger.c.revision.desc()).limit(1)).mappings().first()
    if previous is not None and previous['revision'] > event['revision']:
        return '过期'
    return '生效'


def current_versions(conn, case_id):
    """Latest registered revision per batch, regardless of run boundaries."""
    latest = select(
        cloth_ledger.c.batch_id,
        func.max(cloth_ledger.c.revision).label('revision')
    ).where(cloth_ledger.c.case_id == case_id).group_by(cloth_ledger.c.batch_id).subquery()
    rows = conn.execute(select(cloth_ledger).join(
        latest, and_(cloth_ledger.c.batch_id == latest.c.batch_id,
                     cloth_ledger.c.revision == latest.c.revision))
        .where(cloth_ledger.c.case_id == case_id)
        .order_by(cloth_ledger.c.batch_id)).mappings().all()
    return [dict(row) for row in rows]


def shipped_batch_map(conn, case_id):
    return {row['batch_id']: dict(row) for row in conn.execute(
        select(outbound_batches).where(outbound_batches.c.case_id == case_id)).mappings().all()}


def active_batches(conn, case_id):
    """Current 记账 versions that have not been shipped out."""
    shipped = shipped_batch_map(conn, case_id)
    return [row for row in current_versions(conn, case_id)
            if row['action'] == BOOK and row['batch_id'] not in shipped]


def stock_by_grade(conn, case_id):
    """On-hand stock: sum of active (non-shipped) current 记账 versions."""
    stock = {}
    for row in active_batches(conn, case_id):
        bucket = stock.setdefault(row['grade'],
                                  {'grade': row['grade'], 'weight_g': 0, 'pieces': 0})
        bucket['weight_g'] += row['weight_g']
        bucket['pieces'] += row['pieces']
    return stock


def ledger_seq(conn, case_id):
    return conn.execute(select(func.coalesce(func.max(cloth_ledger.c.sequence), 0))
                        .where(cloth_ledger.c.case_id == case_id)).scalar_one()


def register_outbound_adjustment(conn, case_id, event):
    """Late re-weigh of a shipped batch: stored for reconciliation only."""
    savepoint = conn.begin_nested()
    try:
        conn.execute(outbound_adjustments.insert().values(
            case_id=case_id, registered_at=utc_time(), **event))
    except IntegrityError:
        savepoint.rollback()
        stored = _identity_collision(conn, outbound_adjustments, case_id, event,
                                     ('batch_id', 'revision'))
        if all(stored[key] == event[key] for key in LEDGER_KEYS):
            return '重复'
        raise LedgerConflict(dict(event), dict(stored))
    return '生效'


def create_order(conn, case_id, evidence_id, cuts, actor):
    """Freeze suggested cut batches into a quota order at the current ledger site."""
    seq = ledger_seq(conn, case_id)
    order_id = conn.execute(quota_orders.insert().values(
        case_id=case_id, evidence_id=evidence_id, ledger_seq=seq,
        status=QuotaOrderStatus.PENDING.value, created_by=actor,
        created_at=utc_time())).inserted_primary_key[0]
    for cut in cuts:
        conn.execute(quota_lines.insert().values(
            order_id=order_id, case_id=case_id, batch_id=cut['批次'],
            revision=cut['修订号'], grade=cut['等级'],
            weight_g=cut['转裁重量（克）'], pieces=cut['转裁件数'],
            status=QuotaLineStatus.PENDING.value))
    return order_id


def list_orders(conn, case_id=None):
    statement = select(quota_orders).order_by(quota_orders.c.id.desc())
    if case_id is not None:
        statement = statement.where(quota_orders.c.case_id == case_id)
    return [dict(row) for row in conn.execute(statement).mappings().all()]


def get_order(conn, order_id):
    order = conn.execute(select(quota_orders).where(
        quota_orders.c.id == order_id)).mappings().first()
    if order is None:
        return None
    lines = [dict(row) for row in conn.execute(select(quota_lines).where(
        quota_lines.c.order_id == order_id).order_by(quota_lines.c.id)).mappings().all()]
    return {'order': dict(order), 'lines': lines}


def _refresh_order_status(conn, order_id):
    rows = conn.execute(select(quota_lines.c.status).where(
        quota_lines.c.order_id == order_id)).mappings().all()
    statuses = {row['status'] for row in rows}
    settled = {QuotaLineStatus.SIGNED.value, QuotaLineStatus.DUPLICATE.value}
    if statuses <= settled:
        new_status = QuotaOrderStatus.CLOSED.value
        closed = utc_time()
    elif statuses & settled:
        new_status, closed = QuotaOrderStatus.PARTIAL.value, None
    else:
        new_status, closed = QuotaOrderStatus.PENDING.value, None
    conn.execute(quota_orders.update().where(quota_orders.c.id == order_id).values(
        status=new_status, closed_at=closed))


def sign_line(conn, order_id, line_id, actor, note):
    """Sign one frozen batch version. Idempotent and concurrency-safe."""
    # 锁定配额单，串行化同一单上的并发签收。
    order_row = conn.execute(select(quota_orders).where(
        quota_orders.c.id == order_id).with_for_update()).mappings().first()
    if order_row is None:
        raise LookupError(order_id, line_id)
    line = conn.execute(select(quota_lines).where(
        quota_lines.c.id == line_id, quota_lines.c.order_id == order_id)
        .with_for_update()).mappings().first()
    if line is None:
        raise LookupError(order_id, line_id)
    if line['status'] in (QuotaLineStatus.SIGNED.value, QuotaLineStatus.DUPLICATE.value):
        # 签收失败重试：同一批次版本只扣一次，直接返回既有签收结果。
        return {'status': '重复签收', 'line': dict(line)}
    if order_row['status'] == QuotaOrderStatus.CLOSED.value:
        raise OrderClosed(order_id)

    # 与并发复秤对账：冻结修订号必须仍是该批次当前版本，否则本行作废需重新开单。
    latest = conn.execute(select(func.max(cloth_ledger.c.revision)).where(
        cloth_ledger.c.case_id == line['case_id'],
        cloth_ledger.c.batch_id == line['batch_id'])).scalar_one_or_none()
    if latest is not None and latest != line['revision']:
        conn.execute(quota_lines.update().where(quota_lines.c.id == line_id).values(
            status=QuotaLineStatus.SUPERSEDED.value, sign_note=note or None,
            signed_by=actor, signed_at=utc_time()))
        _refresh_order_status(conn, order_id)
        stale_line = dict(conn.execute(select(quota_lines).where(
            quota_lines.c.id == line_id)).mappings().one())
        return {'status': '版本失效', 'line': stale_line,
                'latest_revision': latest}

    now = utc_time()
    # 整批布出库：唯一约束保证并发/跨单签收只有一方能写入，即只有一方真正扣减在库。
    savepoint = conn.begin_nested()
    try:
        conn.execute(outbound_batches.insert().values(
            case_id=line['case_id'], batch_id=line['batch_id'],
            revision=line['revision'], grade=line['grade'],
            weight_g=line['weight_g'], pieces=line['pieces'],
            line_id=line_id, order_id=order_id, shipped_by=actor, shipped_at=now))
    except IntegrityError:
        savepoint.rollback()
        # 同一批次版本已在另一张配额单签走：本单不再扣减，标记为重复签收，其余行仍可签。
        conn.execute(quota_lines.update().where(quota_lines.c.id == line_id).values(
            status=QuotaLineStatus.DUPLICATE.value, signed_by=actor,
            signed_at=now, sign_note=note or None))
        _refresh_order_status(conn, order_id)
        existing = conn.execute(select(outbound_batches).where(
            outbound_batches.c.case_id == line['case_id'],
            outbound_batches.c.batch_id == line['batch_id'],
            outbound_batches.c.revision == line['revision'])).mappings().one()
        duplicate = dict(conn.execute(select(quota_lines).where(
            quota_lines.c.id == line_id)).mappings().one())
        return {'status': '重复签收', 'line': duplicate, 'signed_order_id': existing['order_id']}
    conn.execute(quota_lines.update().where(quota_lines.c.id == line_id).values(
        status=QuotaLineStatus.SIGNED.value, signed_by=actor,
        signed_at=now, sign_note=note or None))
    _refresh_order_status(conn, order_id)
    return {'status': '已签收', 'line': dict(conn.execute(select(quota_lines).where(
        quota_lines.c.id == line_id)).mappings().one())}


def mark_superseded_lines(conn, case_id):
    """Pending lines whose frozen version fell behind (e.g. concurrent re-weigh)."""
    latest = select(
        cloth_ledger.c.batch_id,
        func.max(cloth_ledger.c.revision).label('revision')
    ).where(cloth_ledger.c.case_id == case_id).group_by(cloth_ledger.c.batch_id).subquery()
    rows = conn.execute(select(quota_lines).join(
        latest, and_(quota_lines.c.batch_id == latest.c.batch_id,
                     quota_lines.c.revision != latest.c.revision)).where(
        quota_lines.c.case_id == case_id,
        quota_lines.c.status == QuotaLineStatus.PENDING.value)).mappings().all()
    order_ids = set()
    for row in rows:
        conn.execute(quota_lines.update().where(quota_lines.c.id == row['id']).values(
            status=QuotaLineStatus.SUPERSEDED.value))
        order_ids.add(row['order_id'])
    for order_id in order_ids:
        _refresh_order_status(conn, order_id)
    return len(rows)


def outbound_reconciliation(conn, case_id):
    """Shipments plus late adjustments, for the outbound reconciliation view."""
    shipped = [dict(row) for row in conn.execute(select(outbound_batches).where(
        outbound_batches.c.case_id == case_id).order_by(outbound_batches.c.id)).mappings().all()]
    adjustments = [dict(row) for row in conn.execute(select(outbound_adjustments).where(
        outbound_adjustments.c.case_id == case_id).order_by(outbound_adjustments.c.id)).mappings().all()]
    return shipped, adjustments
