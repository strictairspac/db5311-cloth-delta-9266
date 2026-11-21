import {heading, panel} from '../components/common/heading.js';
import {records} from '../components/common/records.js';
import {topic} from '../constants/project.js';
import {date, esc} from '../utils/dom.js';
import {submit} from '../hooks/submit.js';

const LINE_STATE = {
  '待签收': 'pending',
  '已签收': 'complete',
  '重复签收': 'duplicate',
  '版本失效': 'failed',
};

export function quotaPage(state, identity) {
  const orders = state.quotaOrders || [];
  const selected = orders.find(row => row.order.id === identity) || orders[0];
  if (!selected) {
    return heading(topic.modules[3], '复核员签署证据后自动开出仓位配额单，裁切组在此按批次与修订号整批签收。')
      + '<p class="empty">还没有配额单。请先在「仓位配额单」证据页完成复核签署。</p>';
  }
  const listRows = orders.map(row => [
    `<a href="#quota/${row.order.id}" ${row.order.id === selected.order.id ? 'aria-current="true"' : ''}>配额单 #${row.order.id}</a>`,
    esc(row.case_title), esc(row.order.status), esc(date(row.order.created_at))]);
  const {order, lines} = selected;
  const canSign = state.session.role === 'viewer';
  const lineRows = lines.map(line => {
    const stateKey = LINE_STATE[line.status] || 'pending';
    const signInfo = line.signed_at
      ? `${esc(line.signed_by || '')} · ${esc(date(line.signed_at))}` : '—';
    const action = line.status === '待签收'
      ? (canSign
          ? `<form class="sign-form" data-line="${line.id}"><input name="note" maxlength="500" placeholder="签收备注（可选）"><button type="submit">整批签收</button></form>`
          : '<span class="note">待裁切组签收</span>')
      : signInfo;
    return [esc(line.batch_id), `第 ${esc(line.revision)} 版`, esc(line.grade),
            esc(line.weight_g), esc(line.pieces),
            `<span class="status" data-state="${stateKey}">${esc(line.status)}</span>`,
            line.sign_note ? esc(line.sign_note) : '—', action];
  });
  const detail = `${heading(`配额单 #${order.id}`, `${esc(selected.case_title)} · 开单账本位点 ${esc(order.ledger_seq)}`,
    `<span class="status" data-state="${LINE_STATE[order.status] || 'pending'}">${esc(order.status)}</span>`)}
    <div class="spread"><section aria-label="配额批次行">
      ${records(['批次', '冻结版本', '等级', '重量（克）', '件数', '签收状态', '备注', '签收人 / 操作'], lineRows, '该配额单没有批次行。')}
    </section>
    <aside class="brief">
      <h2>发单信息</h2>
      <dl><dt>开单证据</dt><dd>#${esc(order.evidence_id)}</dd>
      <dt>开单人</dt><dd>${esc(order.created_by)}</dd>
      <dt>开单时间</dt><dd>${esc(date(order.created_at))}</dd>
      <dt>结清时间</dt><dd>${order.closed_at ? esc(date(order.closed_at)) : '—'}</dd></dl>
      <p class="note">同一批次版本若已在另一张配额单签走，本行记为「重复签收」且不再扣减在库；冻结修订号被新复秤取代时记为「版本失效」，需按新版本重新开单。</p>
    </aside></div>`;
  return `${heading(topic.modules[3], '按批次与修订号核对实物，整批签收；页面可查每批实际签走的是哪一版。')}
    <div class="spread"><section aria-label="配额单目录">${records(['配额单', '档案', '状态', '开单时间'], listRows)}</section>
    <aside class="brief"><h2>签收规则</h2><p class="note">先签者生效；重试签收不会重复扣减。已出库批次的补传复秤只修正出库对账，不改变在库量。</p></aside></div>
    ${panel(`配额单 #${order.id} 批次明细`, detail)}`;
}

export function bindQuota(api, update, fail) {
  const path = location.hash.slice(1).split('/');
  const orderId = Number(path[1]) || 0;
  document.querySelectorAll('.sign-form').forEach(form => {
    const lineId = Number(form.dataset.line);
    form.addEventListener('submit', async event => {
      event.preventDefault();
      const button = event.submitter;
      if (button) button.disabled = true;
      try {
        const data = new FormData(form);
        const result = await api(`/quota-orders/${orderId}/lines/${lineId}/sign`, {
          method: 'POST', data: {note: data.get('note') || ''}});
        if (result.status === '重复签收') {
          const message = document.getElementById('message');
          if (message) {
            message.textContent = '该批次版本已在另一张配额单签走，未重复扣减，可继续签收其余批次。';
            message.className = 'note';
          }
        }
        await update();
      } catch (error) {
        fail(error);
      } finally {
        if (button) button.disabled = false;
      }
    });
  });
}
