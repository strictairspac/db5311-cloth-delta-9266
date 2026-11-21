from enum import StrEnum


class Role(StrEnum):
    OPERATOR = 'operator'
    REVIEWER = 'reviewer'
    VIEWER = 'viewer'


class Status(StrEnum):
    QUEUED = 'queued'
    RUNNING = 'running'
    COMPLETE = 'complete'
    FAILED = 'failed'


class QuotaOrderStatus(StrEnum):
    PENDING = '待签收'
    PARTIAL = '部分签收'
    CLOSED = '已结清'


class QuotaLineStatus(StrEnum):
    PENDING = '待签收'
    SIGNED = '已签收'
    DUPLICATE = '重复签收'
    SUPERSEDED = '版本失效'
