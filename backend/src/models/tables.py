from sqlalchemy import (JSON, CheckConstraint, Column, ForeignKey, Integer,
                        MetaData, String, Table, Text, UniqueConstraint)

schema = MetaData()
users = Table('accounts', schema,
    Column('id', Integer, primary_key=True),
    Column('username', String(40), nullable=False, unique=True),
    Column('password_hash', String(200), nullable=False),
    Column('role', String(20), nullable=False))
cases = Table('business_cases', schema,
    Column('id', Integer, primary_key=True),
    Column('slug', String(64), nullable=False, unique=True),
    Column('title', String(120), nullable=False),
    Column('payload', JSON, nullable=False),
    Column('author', String(40), nullable=False),
    Column('created_at', String(32), nullable=False))
runs = Table('business_runs', schema,
    Column('id', Integer, primary_key=True),
    Column('case_id', ForeignKey('business_cases.id'), nullable=False, index=True),
    Column('status', String(20), nullable=False, index=True),
    Column('owner', String(40), nullable=False),
    Column('attempt_token', String(36)),
    Column('started_at', String(32)),
    Column('finished_at', String(32)),
    Column('created_at', String(32), nullable=False),
    Column('error', Text))
evidence = Table('business_evidence', schema,
    Column('id', Integer, primary_key=True),
    Column('run_id', ForeignKey('business_runs.id'), nullable=False, unique=True),
    Column('report', JSON, nullable=False),
    Column('digest', String(64), nullable=False),
    Column('reviewer', String(40)),
    Column('review_note', String(500)),
    Column('reviewed_at', String(32)))
audit = Table('audit_events', schema,
    Column('id', Integer, primary_key=True),
    Column('actor', String(40), nullable=False),
    Column('action', String(32), nullable=False),
    Column('reference', String(50), nullable=False),
    Column('created_at', String(32), nullable=False))

# 持久化的批次修订账本：同一档案的期初与后续复秤都登记在这里，含过期与重复版本。
cloth_ledger = Table('cloth_ledger', schema,
    Column('id', Integer, primary_key=True),
    Column('case_id', ForeignKey('business_cases.id'), nullable=False, index=True),
    Column('batch_id', String(64), nullable=False),
    Column('revision', Integer, nullable=False),
    Column('action', String(8), nullable=False),
    Column('grade', String(64), nullable=False, server_default=''),
    Column('weight_g', Integer, nullable=False),
    Column('pieces', Integer, nullable=False),
    Column('sequence', Integer, nullable=False),
    Column('source_run_id', ForeignKey('business_runs.id')),
    Column('registered_at', String(32), nullable=False),
    UniqueConstraint('case_id', 'batch_id', 'revision', name='uq_cloth_ledger_identity'),
    CheckConstraint("action IN ('记账', '撤销')", name='ck_cloth_ledger_action'))

# 配额单：复核员签署证据即开单，记录开单时所依据的账本版本位点。
# 同一份证据允许重复开单（例如批次跨多单签收），因此 evidence_id 不加唯一约束。
quota_orders = Table('cloth_quota_orders', schema,
    Column('id', Integer, primary_key=True),
    Column('case_id', ForeignKey('business_cases.id'), nullable=False, index=True),
    Column('evidence_id', ForeignKey('business_evidence.id'), nullable=False, index=True),
    Column('ledger_seq', Integer, nullable=False),
    Column('status', String(16), nullable=False, server_default='待签收'),
    Column('created_by', String(40), nullable=False),
    Column('created_at', String(32), nullable=False),
    Column('closed_at', String(32)))

# 配额单批次行：冻结开单时的批次与修订号。同一批次版本允许出现在多张配额单上，
# 先签者生效；签收幂等由 cloth_outbound_batches 的唯一约束兜底。
quota_lines = Table('cloth_quota_lines', schema,
    Column('id', Integer, primary_key=True),
    Column('order_id', ForeignKey('cloth_quota_orders.id'), nullable=False, index=True),
    Column('case_id', ForeignKey('business_cases.id'), nullable=False, index=True),
    Column('batch_id', String(64), nullable=False),
    Column('revision', Integer, nullable=False),
    Column('grade', String(64), nullable=False),
    Column('weight_g', Integer, nullable=False),
    Column('pieces', Integer, nullable=False),
    Column('status', String(16), nullable=False, server_default='待签收'),
    Column('signed_by', String(40)),
    Column('signed_at', String(32)),
    Column('sign_note', String(500)))

# 已出库批次（整批签收后写入，下次在库量计算时扣除）。
outbound_batches = Table('cloth_outbound_batches', schema,
    Column('id', Integer, primary_key=True),
    Column('case_id', ForeignKey('business_cases.id'), nullable=False, index=True),
    Column('batch_id', String(64), nullable=False),
    Column('revision', Integer, nullable=False),
    Column('grade', String(64), nullable=False),
    Column('weight_g', Integer, nullable=False),
    Column('pieces', Integer, nullable=False),
    Column('line_id', ForeignKey('cloth_quota_lines.id'), nullable=False),
    Column('order_id', ForeignKey('cloth_quota_orders.id'), nullable=False),
    Column('shipped_by', String(40), nullable=False),
    Column('shipped_at', String(32), nullable=False),
    UniqueConstraint('case_id', 'batch_id', 'revision', name='uq_outbound_batch_version'))

# 出库后补传的复秤：不再触碰在库，只作为出库对账修正留痕。
outbound_adjustments = Table('cloth_outbound_adjustments', schema,
    Column('id', Integer, primary_key=True),
    Column('case_id', ForeignKey('business_cases.id'), nullable=False, index=True),
    Column('batch_id', String(64), nullable=False),
    Column('outbound_revision', Integer, nullable=False),
    Column('revision', Integer, nullable=False),
    Column('action', String(8), nullable=False),
    Column('grade', String(64), nullable=False, server_default=''),
    Column('weight_g', Integer, nullable=False),
    Column('pieces', Integer, nullable=False),
    Column('registered_at', String(32), nullable=False),
    UniqueConstraint('case_id', 'batch_id', 'revision', name='uq_outbound_adjustment_identity'))
