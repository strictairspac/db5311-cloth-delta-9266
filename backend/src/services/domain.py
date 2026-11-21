"""纬回布库 donation stock, revision evidence and warehouse quota decisions."""

from collections import Counter

from src.services import cloth_ledger, cloth_stock
from src.services.cloth_quota import warehouse_plan


BOOK = '记账'
CANCEL = '撤销'
SPEC = {
    'name': '纬回布库·捐布分级汇总',
    'summary': '复核修订后的捐布等级库存，判断各等级仓位余量并列出超额时建议转裁的批次。',
    'customer': '纬回布库捐赠运营组与仓位班',
    'entity_labels': ['捐赠称重档案', '增量汇总运行', '仓位配额单'],
    'field_labels': {
        'opening_batches': '期初在库批次', 'revisions': '批次修订流水', 'quotas': '分级仓位配额',
        'batch_id': '捐布批次', 'revision': '修订号', 'action': '修订动作', 'grade': '布料等级',
        'weight_g': '称重（克）', 'pieces': '件数', 'capacity_g': '重量上限（克）',
        'capacity_pieces': '件数上限',
    },
    'guidance': '按收件顺序粘贴修订流水。记账填写完整新批次，撤销将等级留空并将重量、件数填为 0。',
    'sample': {
        'opening_batches': [
            {'batch_id': 'BL-101', 'revision': 1, 'grade': 'A级', 'weight_g': 18000, 'pieces': 12},
            {'batch_id': 'BL-102', 'revision': 1, 'grade': 'A级', 'weight_g': 12000, 'pieces': 8},
            {'batch_id': 'BL-201', 'revision': 1, 'grade': 'B级', 'weight_g': 16000, 'pieces': 11},
            {'batch_id': 'BL-301', 'revision': 1, 'grade': 'C级', 'weight_g': 9000, 'pieces': 6},
            {'batch_id': 'BL-501', 'revision': 1, 'grade': 'D级', 'weight_g': 3000, 'pieces': 2},
        ],
        'revisions': [
            {'batch_id': 'BL-101', 'revision': 3, 'action': BOOK, 'grade': 'B级', 'weight_g': 20000, 'pieces': 13},
            {'batch_id': 'BL-101', 'revision': 2, 'action': BOOK, 'grade': 'A级', 'weight_g': 17500, 'pieces': 11},
            {'batch_id': 'BL-101', 'revision': 3, 'action': BOOK, 'grade': 'B级', 'weight_g': 20000, 'pieces': 13},
            {'batch_id': 'BL-201', 'revision': 2, 'action': CANCEL, 'grade': '', 'weight_g': 0, 'pieces': 0},
            {'batch_id': 'BL-301', 'revision': 2, 'action': BOOK, 'grade': 'C级', 'weight_g': 7000, 'pieces': 6},
            {'batch_id': 'BL-401', 'revision': 1, 'action': BOOK, 'grade': 'A级', 'weight_g': 8000, 'pieces': 5},
            {'batch_id': 'BL-501', 'revision': 2, 'action': CANCEL, 'grade': '', 'weight_g': 0, 'pieces': 0},
        ],
        'quotas': [
            {'grade': 'A级', 'capacity_g': 23000, 'capacity_pieces': 12},
            {'grade': 'B级', 'capacity_g': 17000, 'capacity_pieces': 15},
            {'grade': 'C级', 'capacity_g': 10000, 'capacity_pieces': 8},
            {'grade': 'D级', 'capacity_g': 5000, 'capacity_pieces': 4},
        ],
    },
    'expect': {'生效修订数': 5, '重复修订数': 1, '过期修订数': 1, '库存差异等级数': 0, '建议转裁批次数': 2},
}


def _records(payload, name, keys):
    records = payload.get(name)
    label = SPEC['field_labels'][name]
    if not isinstance(records, list):
        raise ValueError(f'{label}须为数组')
    for index, row in enumerate(records, 1):
        location = f'{label}第 {index} 行'
        if not isinstance(row, dict) or row.keys() != keys:
            fields = '、'.join(SPEC['field_labels'][key] for key in sorted(keys))
            raise ValueError(f'{location}须包含且仅包含 {fields}')
        yield location, row


def _integer(row, key, minimum, location):
    value = row[key]
    if type(value) is not int or not minimum <= value <= 1_000_000_000:
        label = SPEC['field_labels'][key]
        raise ValueError(f'{location}的{label}须为 {minimum} 到 1000000000 之间的整数')


def _identifier(value, label, location):
    if not isinstance(value, str) or not 1 <= len(value) <= 64 or not value.isprintable() or value != value.strip():
        raise ValueError(f'{location}的{label}须为 1 到 64 个可打印字符，首尾须为空白以外的字符')


def _unique(value, seen, location):
    if value in seen:
        raise ValueError(f'{location}的标识「{value}」重复')
    seen.add(value)


def validate(payload):
    if not isinstance(payload, dict):
        raise ValueError('业务输入须为对象')
    batch_keys = {'batch_id', 'revision', 'grade', 'weight_g', 'pieces'}
    opening_ids = set()
    quota_grades = set()
    for name, keys in [('opening_batches', batch_keys), ('revisions', batch_keys | {'action'})]:
        for location, row in _records(payload, name, keys):
            _identifier(row['batch_id'], '批次号', location)
            _integer(row, 'revision', 1, location)
            if name == 'opening_batches':
                _unique(row['batch_id'], opening_ids, location)
            action = row.get('action', BOOK)
            if action not in (BOOK, CANCEL):
                raise ValueError(f'{location}的动作须为「记账」或「撤销」')
            minimum = 1 if action == BOOK else 0
            _integer(row, 'weight_g', minimum, location)
            _integer(row, 'pieces', minimum, location)
            if action == BOOK:
                _identifier(row['grade'], '等级', location)
            elif (row['grade'], row['weight_g'], row['pieces']) != ('', 0, 0):
                raise ValueError(f'{location}撤销时等级须留空，重量与件数须为 0')

    for location, row in _records(payload, 'quotas', {'grade', 'capacity_g', 'capacity_pieces'}):
        _identifier(row['grade'], '等级', location)
        _unique(row['grade'], quota_grades, location)
        _integer(row, 'capacity_g', 0, location)
        _integer(row, 'capacity_pieces', 0, location)


def _audit_row(index, event, status, retracted, added):
    row = {'流水行': index, '批次': event['batch_id'], '修订号': event['revision'],
           '动作': event['action'], '判定': status}
    for label, contribution in [('撤回', retracted), ('累加', added)]:
        row[f'{label}等级'] = contribution['grade'] if contribution else ''
        row[f'{label}重量（克）'] = contribution['weight_g'] if contribution else 0
        row[f'{label}件数'] = contribution['pieces'] if contribution else 0
    return row


def _conflict_report(index, event, stored, audit):
    identity = f"{event['batch_id']} 第 {event['revision']} 版"
    return {
        'summary': f'{identity}出现不同内容，暂停本次仓位与转裁发单。',
        'metrics': {'版本冲突数': 1, '首个冲突流水行': index},
        'sections': [
            {'title': '版本冲突', 'rows': [
                {'来源': source, '批次': row['batch_id'], '修订号': row['revision'], '动作': row['action'],
                 '等级': row['grade'], '重量（克）': row['weight_g'], '件数': row['pieces']}
                for source, row in [('先登记', stored), ('本次收件', event)]
            ]},
            {'title': '冲突前修订轨迹', 'rows': audit},
        ],
    }


def solve(payload, engine, case_id=None, run_id=None):
    validate(payload)
    audit = []
    counts = Counter()
    with engine.begin() as conn:
        cloth_stock.initialize(conn)
        if case_id is None:
            # 独立试算（无档案上下文）：仍按单事务临时表产出完整证据。
            for row in payload['opening_batches']:
                cloth_stock.apply_revision(conn, {**row, 'action': BOOK})
            for index, event in enumerate(payload['revisions'], 1):
                status, retracted, added = cloth_stock.apply_revision(conn, event)
                if status == '版本冲突':
                    return _conflict_report(index, event, retracted, audit)
                counts[status] += 1
                audit.append(_audit_row(index, event, status, retracted, added))
        else:
            # 同一档案续收复秤：临时表完全从持久账本重放，每次运行都是幂等重算。
            shipped_rows, _ = cloth_ledger.outbound_reconciliation(conn, case_id)
            shipped_batches = {row['batch_id'] for row in shipped_rows}
            ledger_current = conn.execute(
                cloth_ledger.cloth_ledger.select().where(
                    cloth_ledger.cloth_ledger.c.case_id == case_id)).mappings().all()
            if not ledger_current:
                # 首次运行：期初在库批次登记进持久账本。
                for row in payload['opening_batches']:
                    cloth_ledger.register_revision(conn, case_id, {**row, 'action': BOOK}, run_id)
                ledger_current = conn.execute(
                    cloth_ledger.cloth_ledger.select().where(
                        cloth_ledger.cloth_ledger.c.case_id == case_id)).mappings().all()
            known = {(row['batch_id'], row['revision']): dict(row) for row in ledger_current}
            known_adjustments = {(row['batch_id'], row['revision']): dict(row) for row in conn.execute(
                cloth_ledger.outbound_adjustments.select().where(
                    cloth_ledger.outbound_adjustments.c.case_id == case_id)).mappings()}
            # 本次运行只登记账本中尚不存在的新事件；身份已存在则比对内容识别重复或冲突。
            run_events = []
            for event in payload['revisions']:
                identity = (event['batch_id'], event['revision'])
                if event['batch_id'] in shipped_batches:
                    # 已出库批次后来补传复秤：只登记出库调整，绝不触碰在库。
                    if identity in known_adjustments:
                        stored = known_adjustments[identity]
                        if all(stored[key] == event[key] for key in
                               ('action', 'grade', 'weight_g', 'pieces')):
                            run_events.append(('出库后补传', '重复', event))
                            continue
                        return _conflict_report(len(audit) + 1, event, stored, audit)
                    adjustment = {
                        'batch_id': event['batch_id'],
                        'outbound_revision': next(
                            row['revision'] for row in shipped_rows
                            if row['batch_id'] == event['batch_id']),
                        'revision': event['revision'], 'action': event['action'],
                        'grade': event['grade'], 'weight_g': event['weight_g'],
                        'pieces': event['pieces']}
                    try:
                        cloth_ledger.register_outbound_adjustment(conn, case_id, adjustment)
                    except cloth_ledger.LedgerConflict as exc:
                        _, stored = exc.args
                        return _conflict_report(len(audit) + 1, event, stored, audit)
                    run_events.append(('出库后补传', '生效', event))
                    continue
                if identity in known:
                    stored = known[identity]
                    if all(stored[key] == event[key] for key in
                           ('action', 'grade', 'weight_g', 'pieces')):
                        run_events.append(('账本', '重复', event))
                    else:
                        return _conflict_report(len(audit) + 1, event, stored, audit)
                    continue
                try:
                    status = cloth_ledger.register_revision(conn, case_id, event, run_id)
                except cloth_ledger.LedgerConflict as exc:
                    event2, stored = exc.args
                    return _conflict_report(len(audit) + 1, event2, stored, audit)
                run_events.append(('账本', status, event))
            # 从持久账本全量重放（已出库批次除外），重建临时库存与修订轨迹。
            ledger_rows = conn.execute(
                cloth_ledger.cloth_ledger.select().where(
                    cloth_ledger.cloth_ledger.c.case_id == case_id)
                .order_by(cloth_ledger.cloth_ledger.c.sequence)).mappings().all()
            for row in ledger_rows:
                if row['batch_id'] in shipped_batches:
                    continue
                event = {key: row[key] for key in
                         ('batch_id', 'revision', 'action', 'grade', 'weight_g', 'pieces')}
                cloth_stock.apply_revision(conn, event)
            for index, (kind, status, event) in enumerate(run_events, 1):
                if kind == '出库后补传':
                    counts['出库后补传'] += 1
                    audit.append(_audit_row(index, event, '出库后补传', None, None))
                else:
                    counts[status] += 1
                    audit.append(_audit_row(index, event, status, None, None))

        reconciled = conn.execute(cloth_stock.RECONCILE).mappings().all()
        differences = sum(row['differs'] for row in reconciled)
        metrics = {
            '生效修订数': counts['生效'], '重复修订数': counts['重复'], '过期修订数': counts['过期'],
            '出库后补传数': counts['出库后补传'],
            '核对库存等级数': len(reconciled), '库存差异等级数': differences,
        }
        sections = [
            {'title': 'PostgreSQL 全量对账', 'rows': [
                {'等级': row['grade'], '增量重量（克）': row['incremental_weight'], '全量重量（克）': row['full_weight'],
                 '增量件数': row['incremental_pieces'], '全量件数': row['full_pieces'],
                 '判定': '有差异' if row['differs'] else '一致'}
                for row in reconciled
            ]},
            {'title': '批次修订轨迹', 'rows': audit},
        ]
        if differences:
            return {'summary': f'{differences} 个等级库存存在差异，暂停本次仓位与转裁发单。',
                    'metrics': metrics, 'sections': sections}
        active_batches = conn.execute(cloth_stock.ACTIVE_BATCHES).mappings().all()
        if case_id is not None:
            # 已整批签收出库的批次从下次在库量中扣掉。
            superseded = cloth_ledger.mark_superseded_lines(conn, case_id)
            metrics['版本失效配额行数'] = superseded
            if shipped_rows:
                sections.append({'title': '已出库批次（在库量已扣除）', 'rows': [
                    {'批次': row['batch_id'], '修订号': row['revision'], '等级': row['grade'],
                     '出库重量（克）': row['weight_g'], '出库件数': row['pieces'],
                     '签收人': row['shipped_by'], '签收时间': row['shipped_at']}
                    for row in shipped_rows]})
            _, adjustments = cloth_ledger.outbound_reconciliation(conn, case_id)
            if adjustments:
                sections.append({'title': '出库后补传复秤（仅修正出库对账）', 'rows': [
                    {'批次': row['batch_id'], '出库修订号': row['outbound_revision'],
                     '补传修订号': row['revision'], '动作': row['action'], '等级': row['grade'],
                     '重量（克）': row['weight_g'], '件数': row['pieces'],
                     '登记时间': row['registered_at']}
                    for row in adjustments]})

    decisions, cuts, missing_quotas = warehouse_plan(reconciled, active_batches, payload['quotas'])
    metrics.update({'建议转裁批次数': len(cuts), '缺少配额等级数': missing_quotas})
    sections[1:1] = [
        {'title': '分级仓位配额单', 'rows': decisions},
        {'title': '建议转裁批次', 'rows': cuts},
    ]
    summary = f'库存全量对账一致，建议转裁 {len(cuts)} 批。'
    if missing_quotas:
        summary += f'{missing_quotas} 个等级缺少配额，暂停这些等级发单。'
    return {'summary': summary, 'metrics': metrics, 'sections': sections}
