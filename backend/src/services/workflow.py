import hashlib
import uuid

from fastapi import HTTPException
from sqlalchemy import and_, or_, select

from src.config.settings import cache, engine
from src.constants.workflow import Status
from src.models.tables import audit, cases, evidence, runs
from src.services import cloth_ledger as ledger
from src.services import domain
from src.utils.encoding import canonical
from src.utils.time import utc_time


def log(conn, actor, action, reference):
    conn.execute(audit.insert().values(actor=actor, action=action, reference=str(reference), created_at=utc_time()))


def recent(table):
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(select(table).order_by(table.c.id.desc()).limit(100)).mappings()]


def found(conn, table, identity):
    row = conn.execute(select(table).where(table.c.id == identity)).mappings().first()
    if row is None:
        raise HTTPException(404, '找不到该记录')
    return dict(row)


def make_case(title, payload, actor):
    title = title.strip()
    if not title:
        raise HTTPException(422, '档案名称不能为空')
    try:
        if len(canonical(payload)) > 512 * 1024:
            raise ValueError('业务输入超过 512 KiB')
        if callable(getattr(domain, 'validate', None)):
            domain.validate(payload)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from None
    with engine.begin() as conn:
        case_id = conn.execute(cases.insert().values(slug=uuid.uuid4().hex, title=title, payload=payload,
                               author=actor, created_at=utc_time())).inserted_primary_key[0]
        log(conn, actor, 'create_case', case_id)
        return found(conn, cases, case_id)


def enqueue(case_id, actor):
    with engine.begin() as conn:
        found(conn, cases, case_id)
        run_id = conn.execute(runs.insert().values(case_id=case_id, status=Status.QUEUED,
                              owner=actor, created_at=utc_time())).inserted_primary_key[0]
        log(conn, actor, 'enqueue', run_id)
        result = found(conn, runs, run_id)
    try:
        cache.lpush('worker:wake', 'queued')
        cache.ltrim('worker:wake', 0, 99)
    except Exception:
        # The durable database queue is polled even if Redis is temporarily unavailable.
        pass
    return result


def claim():
    ready = or_(runs.c.status == Status.QUEUED,
                and_(runs.c.status == Status.RUNNING, runs.c.started_at < utc_time(-600)))
    with engine.begin() as conn:
        run = conn.execute(select(runs.c.id, runs.c.case_id).where(ready).order_by(runs.c.id)
                           .limit(1).with_for_update(skip_locked=True)).first()
        if not run:
            return None
        token = str(uuid.uuid4())
        conn.execute(runs.update().where(runs.c.id == run.id).values(
            started_at=utc_time(), status=Status.RUNNING, attempt_token=token))
        return run.id, token, run.case_id, found(conn, cases, run.case_id)['payload']


def finish(run_id, token, result=None, error=None):
    digest = hashlib.sha256(canonical(result)).hexdigest() if result is not None else None
    status = Status.FAILED if error else Status.COMPLETE
    with engine.begin() as conn:
        count = conn.execute(runs.update().where(runs.c.id == run_id,
                        runs.c.attempt_token == token, runs.c.status == Status.RUNNING)
                        .values(status=status, finished_at=utc_time(), error=error)).rowcount
        if not count:
            return
        if result is not None:
            conn.execute(evidence.insert().values(run_id=run_id, report=result, digest=digest))
        log(conn, 'worker', status.value, run_id)


def review(identity, note, actor):
    note = note.strip()
    if not note:
        raise HTTPException(422, '复核意见不能为空')
    with engine.begin() as conn:
        row = found(conn, evidence, identity)
        count = conn.execute(evidence.update().where(evidence.c.id == identity, evidence.c.reviewed_at.is_(None))
                            .values(review_note=note, reviewer=actor, reviewed_at=utc_time())).rowcount
        if count != 1:
            raise HTTPException(409, '该证据已经复核')
        # 复核签署即冻结建议转裁批次，开出仓位配额单；无转裁建议则不开单。
        order_id = None
        cuts = next((section['rows'] for section in row['report'].get('sections', [])
                     if section['title'] == '建议转裁批次'), [])
        if cuts:
            run_case_id = _case_id_of_run(conn, row['run_id'])
            order_id = ledger.create_order(conn, run_case_id, identity, cuts, actor)
        log(conn, actor, 'review', identity)
        result = found(conn, evidence, identity)
        result['quota_order_id'] = order_id
        return result


def _case_id_of_run(conn, run_id):
    return conn.execute(select(runs.c.case_id).where(runs.c.id == run_id)).scalar_one()


def append_revisions(case_id, revisions, actor):
    """同一档案续收复秤：校验后并入档案流水并发起一次重算运行。"""
    with engine.begin() as conn:
        case = found(conn, cases, case_id)
    payload = dict(case['payload'])
    payload['revisions'] = list(payload.get('revisions', [])) + revisions
    try:
        domain.validate(payload)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from None
    with engine.begin() as conn:
        conn.execute(cases.update().where(cases.c.id == case_id).values(payload=payload))
        run_id = conn.execute(runs.insert().values(case_id=case_id, status=Status.QUEUED,
                              owner=actor, created_at=utc_time())).inserted_primary_key[0]
        log(conn, actor, 'append_revision', run_id)
    try:
        cache.lpush('worker:wake', 'queued')
        cache.ltrim('worker:wake', 0, 99)
    except Exception:
        pass
    return found_id(runs, run_id)


def found_id(table, identity):
    with engine.connect() as conn:
        return found(conn, table, identity)


def list_quota_orders(case_id=None):
    with engine.begin() as conn:
        orders = ledger.list_orders(conn, case_id)
        result = []
        for order in orders:
            detail = ledger.get_order(conn, order['id'])
            detail['case_title'] = conn.execute(select(cases.c.title).where(
                cases.c.id == order['case_id'])).scalar_one()
            result.append(detail)
        return result


def quota_order_detail(order_id):
    with engine.connect() as conn:
        detail = ledger.get_order(conn, order_id)
        if detail is None:
            raise HTTPException(404, '找不到该配额单')
        detail['case_title'] = conn.execute(select(cases.c.title).where(
            cases.c.id == detail['order']['case_id'])).scalar_one()
        return detail


def sign_quota_line(order_id, line_id, note, actor):
    note = (note or '').strip()
    with engine.begin() as conn:
        try:
            outcome = ledger.sign_line(conn, order_id, line_id, actor, note)
        except LookupError:
            raise HTTPException(404, '找不到该配额批次行') from None
        except ledger.OrderClosed:
            raise HTTPException(409, '该配额单已结清') from None
        log(conn, actor, 'quota_sign', line_id)
        if outcome['status'] == '重复签收':
            log(conn, actor, 'quota_line_duplicate', line_id)
        elif outcome['status'] == '版本失效':
            log(conn, actor, 'quota_line_stale', line_id)
    # 事务提交后再返回冲突响应，保证「版本失效」行状态已经落库。
    if outcome['status'] == '版本失效':
        line = outcome['line']
        raise HTTPException(409, f"批次 {line['batch_id']} 已从第 {line['revision']} 版修订到第 "
                                 f"{outcome['latest_revision']} 版，本行失效，请按新版本重新开单")
    return outcome
