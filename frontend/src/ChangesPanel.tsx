import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { LoaderCircle, Plus, RefreshCw } from 'lucide-react';
import { ApiError, api, message } from './api';

type ChangeTarget = {
  id: 'local_inventory';
  kind: 'mysql';
  host: string;
  port: number;
  database: string;
  table: 'inventory';
  column: 'quantity';
  max_rows: number;
  max_quantity: number;
};
type Row = { quantity: number; version: number };
type ChangeItem = {
  plan: {
    id: string;
    owner: string;
    scope: string;
    target: string;
    target_fingerprint: string;
    request_id: string;
    item_id: number;
    before: Row;
    after: Row;
    created_at: number;
    expires_at: number;
    recovery_of: string | null;
  };
  digest: string;
  status:
    | 'preview'
    | 'approved'
    | 'executing'
    | 'unknown'
    | 'committed'
    | 'rejected'
    | 'rolled_back'
    | 'not_committed';
  evidence: null | {
    outcome: string;
    code: string;
    current?: Row;
    receipt_verified?: boolean;
  };
};
type ChangeList = { targets: ChangeTarget[]; changes: ChangeItem[]; can_approve: boolean };
type Action = 'approve' | 'execute' | 'reconcile' | 'recover';

const stateNames: Record<ChangeItem['status'], string> = {
  preview: '待审批预览',
  approved: '已审批，尚未确认执行结果',
  executing: '执行中，结果待核对',
  unknown: '提交结果不确定',
  committed: '已确认提交',
  rejected: '已拒绝执行',
  rolled_back: '已回滚',
  not_committed: '已核对未提交',
};
const timeLabel = (seconds: number) =>
  new Date(seconds * 1000).toLocaleString(undefined, { timeZoneName: 'short' });
const targetLabel = (target: ChangeTarget) =>
  `${target.kind} · ${target.host}:${target.port} / ${target.database}.${target.table}.${target.column}`;

export default function ChangesPanel() {
  const [targets, setTargets] = useState<ChangeTarget[]>([]);
  const [items, setItems] = useState<ChangeItem[]>([]);
  const [selected, setSelected] = useState<ChangeItem | null>(null);
  const [canApprove, setCanApprove] = useState(false);
  const [editing, setEditing] = useState(false);
  const [itemId, setItemId] = useState('');
  const [quantity, setQuantity] = useState('');
  const [reviewed, setReviewed] = useState(false);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState('');
  const [uncertain, setUncertain] = useState<Set<string>>(new Set());
  const [now, setNow] = useState(Date.now());
  const pending = useRef<AbortController | null>(null);
  const lifecycle = useRef<AbortController | null>(null);
  const previewRequest = useRef<string | null>(null);
  const recoveryRequests = useRef(new Map<string, string>());

  // Serialize user actions immediately, including clicks before React re-renders.
  async function run(work: (signal: AbortSignal) => Promise<void>) {
    const controller = lifecycle.current;
    if (
      (pending.current && !pending.current.signal.aborted) ||
      !controller ||
      controller.signal.aborted
    )
      return;
    pending.current = controller;
    setBusy(true);
    setError('');
    try {
      await work(controller.signal);
    } catch (cause) {
      if (!controller.signal.aborted) {
        setError(message(cause));
        if (cause instanceof ApiError && [403, 404].includes(cause.status)) {
          setSelected(null);
          setItems([]);
          setTargets([]);
          setCanApprove(false);
          setEditing(false);
        }
      }
    } finally {
      if (pending.current === controller) pending.current = null;
      if (!controller.signal.aborted) setBusy(false);
    }
  }
  async function reload(signal: AbortSignal) {
    const next = await api<ChangeList>('/changes', { signal });
    setTargets(next.targets);
    setItems(next.changes);
    setCanApprove(next.can_approve);
    setSelected((current) =>
      current ? next.changes.find((item) => item.plan.id === current.plan.id) || null : null,
    );
    if (!next.targets.length) setEditing(false);
    setReviewed(false);
  }
  useEffect(() => {
    const controller = new AbortController();
    lifecycle.current = controller;
    void run(reload);
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, []);

  function remember(item: ChangeItem) {
    setItems((current) => [item, ...current.filter((old) => old.plan.id !== item.plan.id)]);
    setSelected(item);
    setEditing(false);
    setReviewed(false);
  }
  function select(id: string) {
    void run(async (signal) => {
      remember(await api<ChangeItem>(`/changes/${encodeURIComponent(id)}`, { signal }));
    });
  }
  const target = targets[0];
  const selectedTarget = targets.find((candidate) => candidate.id === selected?.plan.target);
  const expired = selected ? selected.plan.expires_at * 1000 <= now : false;
  const needsReconcile =
    selected &&
    (uncertain.has(selected.plan.id) ||
      ['executing', 'unknown'].includes(selected.status) ||
      selected.evidence?.outcome === 'unknown');

  function preview(event: FormEvent) {
    event.preventDefault();
    if (!target) return;
    void run(async (signal) => {
      if (
        !/^\d+$/.test(itemId) ||
        !/^\d+$/.test(quantity) ||
        Number(itemId) < 1 ||
        Number(itemId) > 1_000_000_000 ||
        Number(quantity) > target.max_quantity
      )
        throw new Error('商品 ID 需为 1–1000000000 的整数，数量需为允许范围内的非负整数。');
      previewRequest.current ??= crypto.randomUUID();
      remember(
        await api<ChangeItem>('/changes/preview', {
          method: 'POST',
          signal,
          body: JSON.stringify({
            target: target.id,
            item_id: Number(itemId),
            quantity: Number(quantity),
            request_id: previewRequest.current,
          }),
        }),
      );
    });
  }

  function change(action: Action) {
    if (!selected || !selectedTarget) return;
    if (action === 'approve' && (!reviewed || !canApprove || expired)) return;
    if (
      action === 'execute' &&
      (selected.status !== 'approved' || expired || needsReconcile || !canApprove)
    )
      return;
    if (action === 'recover' && (selected.status !== 'committed' || needsReconcile)) return;
    const id = selected.plan.id;
    void run(async (signal) => {
      let body: object = {};
      if (action === 'approve') body = { digest: selected.digest };
      if (action === 'recover') {
        if (!recoveryRequests.current.has(id))
          recoveryRequests.current.set(id, crypto.randomUUID());
        body = { request_id: recoveryRequests.current.get(id) };
      }
      try {
        const item = await api<ChangeItem>(`/changes/${encodeURIComponent(id)}/${action}`, {
          method: 'POST',
          signal,
          body: JSON.stringify(body),
        });
        remember(item);
        if (
          action === 'reconcile' &&
          !['unknown', 'executing'].includes(item.status) &&
          item.evidence?.outcome !== 'unknown'
        )
          setUncertain((current) => {
            const next = new Set(current);
            next.delete(id);
            return next;
          });
      } catch (cause) {
        if (action === 'execute' && !signal.aborted) {
          setUncertain((current) => new Set(current).add(id));
          const explanation = `${message(cause)} 未取得可确认的执行结果。保留本变更 ID，请刷新状态或核对提交结果；不要另建变更重复写入。`;
          throw cause instanceof ApiError
            ? new ApiError(explanation, cause.status, cause.code)
            : new Error(explanation);
        }
        throw cause;
      }
    });
  }

  return (
    <div className="changes-panel">
      <p className="muted small">
        仅支持获准的本机合成 MySQL 目标，按商品 ID
        修改单行库存数量。预览不写入；人工审批后仍需单独执行。
        当前允许有审批权限的本人审批自己的变更，不是双人审批流程。
      </p>
      <div className="knowledge-toolbar">
        <button
          className="secondary-button"
          disabled={busy || !target}
          onClick={() => {
            setSelected(null);
            setEditing(true);
            setReviewed(false);
            setItemId('');
            setQuantity('');
            previewRequest.current = null;
            setError('');
          }}
        >
          <Plus size={15} />
          新建变更预览
        </button>
        <button className="text-button" disabled={busy} onClick={() => void run(reload)}>
          <RefreshCw size={14} />
          刷新变更列表
        </button>
      </div>
      {error && (
        <div className="notice error" role="alert">
          {error}
        </div>
      )}
      {!busy && !target && !error && <p className="muted">当前身份或数据源未启用受控变更。</p>}
      <div className="knowledge-layout">
        <nav className="knowledge-list" aria-label="变更列表">
          {items.map((item) => (
            <button
              key={item.plan.id}
              className={selected?.plan.id === item.plan.id ? 'selected' : ''}
              disabled={busy}
              onClick={() => select(item.plan.id)}
            >
              <strong>
                {item.plan.recovery_of ? '恢复' : '变更'} · 商品 {item.plan.item_id}
              </strong>
              <span>{stateNames[item.status]}</span>
              <span>{item.plan.id}</span>
            </button>
          ))}
          {!busy && target && !items.length && (
            <p className="muted small">当前没有已保存的变更。</p>
          )}
        </nav>
        <section className="knowledge-detail" aria-label="变更详情" aria-busy={busy}>
          {busy && (
            <div className="panel-loading" role="status">
              <LoaderCircle size={18} className="spin" />
              正在读取或处理变更…
            </div>
          )}
          {editing && target ? (
            <form onSubmit={preview}>
              <h3>创建单行库存变更预览</h3>
              <p className="small">目标：{targetLabel(target)}</p>
              <p className="muted small">
                最多 {target.max_rows} 行；数量范围 0–{target.max_quantity}。不接受任意 SQL。
              </p>
              <label className="field-label">
                商品 ID
                <input
                  type="number"
                  min={1}
                  max={1_000_000_000}
                  step={1}
                  required
                  value={itemId}
                  disabled={busy}
                  onChange={(event) => {
                    setItemId(event.target.value);
                    previewRequest.current = null;
                  }}
                />
              </label>
              <label className="field-label">
                变更后的库存数量
                <input
                  type="number"
                  min={0}
                  max={target.max_quantity}
                  step={1}
                  required
                  value={quantity}
                  disabled={busy}
                  onChange={(event) => {
                    setQuantity(event.target.value);
                    previewRequest.current = null;
                  }}
                />
              </label>
              <div className="knowledge-toolbar">
                <button className="primary-button" disabled={busy || !itemId || !quantity}>
                  生成前后值预览
                </button>
              </div>
            </form>
          ) : selected ? (
            <>
              <h3>
                {selected.plan.recovery_of ? '恢复变更预览' : '库存变更'} · 商品{' '}
                {selected.plan.item_id}
              </h3>
              <p className="knowledge-state" role="status">
                {stateNames[selected.status]}
                {expired && ['preview', 'approved'].includes(selected.status) ? ' · 已过期' : ''}
              </p>
              <p className="small">
                目标：{selectedTarget ? targetLabel(selectedTarget) : '当前授权目标不可用'}
              </p>
              <div className="table-scroll">
                <table className="result-table" aria-label="变更前后值">
                  <thead>
                    <tr>
                      <th>字段</th>
                      <th>变更前</th>
                      <th>变更后</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr>
                      <th>quantity</th>
                      <td>{selected.plan.before.quantity}</td>
                      <td>{selected.plan.after.quantity}</td>
                    </tr>
                    <tr>
                      <th>version</th>
                      <td>{selected.plan.before.version}</td>
                      <td>{selected.plan.after.version}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <dl className="knowledge-provenance">
                <dt>变更 ID</dt>
                <dd>{selected.plan.id}</dd>
                <dt>创建身份</dt>
                <dd>{selected.plan.owner}</dd>
                <dt>创建时间</dt>
                <dd>{timeLabel(selected.plan.created_at)}</dd>
                <dt>审批和执行有效期至</dt>
                <dd>{timeLabel(selected.plan.expires_at)}</dd>
                <dt>审批内容摘要 digest</dt>
                <dd>{selected.digest}</dd>
                <dt>目标指纹</dt>
                <dd>{selected.plan.target_fingerprint}</dd>
                {selected.plan.recovery_of && (
                  <>
                    <dt>恢复原变更 ID</dt>
                    <dd>{selected.plan.recovery_of}</dd>
                  </>
                )}
              </dl>
              {needsReconcile && (
                <p className="notice" role="alert">
                  {selected.status === 'committed'
                    ? '历史已确认提交；最新核对证据不可用。恢复前请先成功核对提交结果。'
                    : '尚不能确认是否提交。请核对提交结果；刷新只读取状态，不会再次执行。不要另建相同变更重复写入。'}
                </p>
              )}
              {selected.evidence && (
                <section aria-label="执行或核对证据">
                  <h3>执行或核对证据</h3>
                  <p className="small">
                    {selected.evidence.outcome} · {selected.evidence.code}
                  </p>
                  {selected.evidence.current && (
                    <p className="small">
                      本次观测 quantity={selected.evidence.current.quantity}，version=
                      {selected.evidence.current.version}
                    </p>
                  )}
                  <p className="muted small">
                    {selected.evidence.receipt_verified
                      ? '本次证据已核对数据库提交凭证。'
                      : '本次证据未确认数据库提交凭证。'}{' '}
                    恢复执行前仍会重新核对前值与版本。
                  </p>
                </section>
              )}
              {selected.status === 'preview' && (
                <div className="knowledge-confirm">
                  <label>
                    <input
                      type="checkbox"
                      checked={reviewed}
                      disabled={busy || expired || !canApprove || !selectedTarget}
                      onChange={(event) => setReviewed(event.target.checked)}
                    />
                    我已核对目标与前后值
                  </label>
                  <button
                    className="primary-button"
                    disabled={busy || !reviewed || expired || !canApprove || !selectedTarget}
                    onClick={() => change('approve')}
                  >
                    明确人工审批
                  </button>
                  {!canApprove && <p className="muted small">当前身份没有审批权限。</p>}
                </div>
              )}
              {selected.status === 'approved' && (
                <div className="knowledge-toolbar">
                  <button
                    className="primary-button"
                    disabled={busy || expired || !!needsReconcile || !selectedTarget || !canApprove}
                    onClick={() => change('execute')}
                  >
                    执行已审批变更
                  </button>
                </div>
              )}
              <div className="knowledge-toolbar">
                <button
                  className="text-button"
                  disabled={busy}
                  onClick={() => select(selected.plan.id)}
                >
                  刷新此变更状态
                </button>
                <button
                  className="secondary-button"
                  disabled={busy || !selectedTarget || selected.status === 'preview'}
                  onClick={() => change('reconcile')}
                >
                  核对提交结果
                </button>
                {selected.status === 'committed' && (
                  <button
                    className="secondary-button"
                    disabled={busy || !selectedTarget || !!needsReconcile}
                    onClick={() => change('recover')}
                  >
                    生成恢复预览
                  </button>
                )}
              </div>
              {selected.status === 'committed' && (
                <p className="muted small">
                  恢复会创建新的反向变更预览，仍需重新审批和执行；当前值已变化时可被拒绝。
                </p>
              )}
              {expired && ['preview', 'approved'].includes(selected.status) && (
                <p className="muted small">
                  这份预览已过期，不能审批或执行。重新预览会重新读取前值。
                </p>
              )}
            </>
          ) : (
            !busy && target && <p className="muted">选择已保存的变更查看证据，或新建前后值预览。</p>
          )}
        </section>
      </div>
    </div>
  );
}
