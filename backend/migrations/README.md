# 初始结构

`src/models/tables.py` 定义初始关系结构。API 启动时显式创建演示库的表；此版本只包含初始 schema。需要在已有部署升级表结构时，应单独引入版本化迁移。

## 配额签收扩展表（2026-11）

为支持同一档案续收复秤、配额单整批签收与出库对账，新增四张表，均由
`schema.create_all` 在空库上创建；已部署的旧库需要手工执行等价 DDL：

- `cloth_ledger`：按档案持久化的批次修订账本，`(case_id, batch_id, revision)` 唯一，保留全部生效、过期与重复版本。
- `cloth_quota_orders`：复核签署证据时开出的配额单，冻结开单时的账本位点 `ledger_seq`。
- `cloth_quota_lines`：配额单批次行，冻结批次与修订号；同一批次版本允许出现在多张配额单上，签收幂等由出库表唯一约束保证。
- `cloth_outbound_batches`：整批签收后的出库记录，`(case_id, batch_id, revision)` 唯一，是"不重复扣减"的最终防线。
- `cloth_outbound_adjustments`：已出库批次的补传复秤，只用于出库对账，不参与在库量计算。

升级要点：`cloth_quota_orders.evidence_id` 为普通索引（同一证据可重复开单），
不要加唯一约束；PostgreSQL 16 上签收冲突依赖 SAVEPOINT（`begin_nested`）回滚子事务。
