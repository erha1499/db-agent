import { useEffect, useState } from 'react';
import { LoaderCircle, Plus, RefreshCw } from 'lucide-react';
import { api, message } from './api';
import type { Identity, KnowledgeDraft, KnowledgeItem } from './types';

const kindNames = { metric: '业务口径', relationship: '表间关系', sql_template: 'SQL 模板' };
const stateNames = { draft: '待确认', confirmed: '已确认', revoked: '已撤销' };
function example(kind: KnowledgeDraft['kind'], tables: string[]): string {
  const table = tables[0] || 'table_name';
  const other = tables[1] || 'other_table';
  const draft: KnowledgeDraft = {
    kind,
    title: `示例：${kindNames[kind]}（请修改）`,
    definition: '请填写核对过的业务定义与适用范围；此示例尚未验证。',
    source: '请填写实际资料来源',
    source_version: '请填写来源版本',
    invalidation_condition: '来源口径、字段含义或关联规则变更时失效。',
    expires_at: new Date(Date.now() + 30 * 86400_000).toISOString(),
    tables: kind === 'relationship' ? [table, other] : [table],
    sql: kind === 'sql_template' ? `SELECT id FROM ${table} LIMIT 10` : null,
    relationship:
      kind === 'relationship'
        ? {
            table,
            columns: ['other_id'],
            referenced_table: other,
            referenced_columns: ['id'],
          }
        : null,
  };
  return JSON.stringify(draft, null, 2);
}

export default function KnowledgePanel({
  identity,
  canInsert,
  onInsert,
}: {
  identity: Identity;
  canInsert: boolean;
  onInsert: (reference: string) => void;
}) {
  const [items, setItems] = useState<KnowledgeItem[]>([]);
  const [selected, setSelected] = useState<KnowledgeItem | null>(null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(() => example('metric', identity.allowed_tables));
  const [reviewed, setReviewed] = useState(false);
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState('');
  async function reload(signal?: AbortSignal) {
    setBusy(true);
    setError('');
    try {
      const next = (await api<{ knowledge: KnowledgeItem[] }>('/knowledge', { signal })).knowledge;
      setItems(next);
      setSelected((current) =>
        current ? next.find((item) => item.id === current.id) || null : null,
      );
      setReviewed(false);
    } catch (cause) {
      if (!signal?.aborted) setError(message(cause));
    } finally {
      if (!signal?.aborted) setBusy(false);
    }
  }
  useEffect(() => {
    const controller = new AbortController();
    void reload(controller.signal);
    return () => controller.abort();
  }, []);
  function remember(item: KnowledgeItem) {
    setItems((current) => [item, ...current.filter((old) => old.id !== item.id)]);
    setSelected(item);
    setEditing(false);
    setReviewed(false);
    setReason('');
  }
  async function select(id: string) {
    setSelected(null);
    setEditing(false);
    setReviewed(false);
    setReason('');
    setBusy(true);
    setError('');
    try {
      remember(await api<KnowledgeItem>(`/knowledge/${encodeURIComponent(id)}`));
    } catch (cause) {
      setError(message(cause));
    } finally {
      setBusy(false);
    }
  }
  async function create() {
    setBusy(true);
    setError('');
    try {
      let parsed: unknown;
      try {
        parsed = JSON.parse(draft);
      } catch {
        throw new Error('草稿不是有效 JSON，请检查引号、逗号和括号。');
      }
      remember(
        await api<KnowledgeItem>('/knowledge', { method: 'POST', body: JSON.stringify(parsed) }),
      );
    } catch (cause) {
      setError(message(cause));
    } finally {
      setBusy(false);
    }
  }
  async function change(action: 'confirm' | 'revoke') {
    if (!selected || (action === 'confirm' && !reviewed)) return;
    setBusy(true);
    setError('');
    try {
      remember(
        await api<KnowledgeItem>(`/knowledge/${encodeURIComponent(selected.id)}/${action}`, {
          method: 'POST',
          body: JSON.stringify(action === 'confirm' ? { digest: selected.digest } : { reason }),
        }),
      );
    } catch (cause) {
      setError(message(cause));
    } finally {
      setBusy(false);
    }
  }
  const expired = selected ? Date.parse(selected.payload.expires_at) <= Date.now() : false;
  const modelScope = selected?.payload.tables.every((table) =>
    identity.model_tables.includes(table),
  );
  const insertDisabled =
    busy ||
    !canInsert ||
    !identity.model_enabled ||
    !modelScope ||
    selected?.state !== 'confirmed' ||
    expired;

  return (
    <div className="knowledge-panel">
      <p className="muted small">
        仅展示当前身份和权限范围内的资料。每轮智能查询需显式引用；资料不能授予查询权限。权限、来源、结构或有效期变化后，服务端会重新检查引用。
      </p>
      <div className="knowledge-toolbar">
        <button
          className="secondary-button"
          disabled={busy || !identity.allowed_tables.length}
          onClick={() => {
            setEditing(true);
            setSelected(null);
            setError('');
          }}
        >
          <Plus size={15} />
          新建资料
        </button>
        <button className="text-button" disabled={busy} onClick={() => void reload()}>
          <RefreshCw size={14} />
          刷新列表
        </button>
      </div>
      {error && (
        <div className="notice error" role="alert">
          {error}
        </div>
      )}
      <div className="knowledge-layout">
        <nav className="knowledge-list" aria-label="业务知识列表">
          {items.map((item) => (
            <button
              key={item.id}
              disabled={busy}
              className={selected?.id === item.id ? 'selected' : ''}
              onClick={() => void select(item.id)}
            >
              <strong>{item.payload.title}</strong>
              <span>
                {kindNames[item.payload.kind]} · {stateNames[item.state]}
              </span>
            </button>
          ))}
          {!items.length && !busy && <p className="muted small">当前没有已保存的资料。</p>}
        </nav>
        <section className="knowledge-detail" aria-label="业务知识详情">
          {busy && (
            <div className="panel-loading">
              <LoaderCircle size={18} className="spin" />
              读取或保存中…
            </div>
          )}
          {editing ? (
            <>
              <h3>编写待确认资料</h3>
              <p className="muted small">
                填写实际来源、版本、失效条件与带时区的到期时间。示例字段与 SQL
                需按真实表结构修改；保存只创建草稿。
              </p>
              <div className="knowledge-examples" aria-label="填入合成示例">
                {(Object.keys(kindNames) as KnowledgeDraft['kind'][]).map((kind) => (
                  <button
                    className="text-button"
                    key={kind}
                    disabled={busy}
                    onClick={() => setDraft(example(kind, identity.allowed_tables))}
                  >
                    {kindNames[kind]}示例
                  </button>
                ))}
              </div>
              <label className="field-label">
                资料草稿 JSON
                <textarea
                  className="knowledge-editor"
                  value={draft}
                  maxLength={16000}
                  spellCheck={false}
                  disabled={busy}
                  onChange={(event) => setDraft(event.target.value)}
                />
              </label>
              <button className="primary-button" disabled={busy || !draft.trim()} onClick={create}>
                保存待确认资料
              </button>
            </>
          ) : selected ? (
            <>
              <h3>{selected.payload.title}</h3>
              <p className="knowledge-state">
                {stateNames[selected.state]}
                {expired ? ' · 已过期' : ''}
              </p>
              <pre className="knowledge-payload">{JSON.stringify(selected.payload, null, 2)}</pre>
              <dl className="knowledge-provenance">
                <dt>资料 ID</dt>
                <dd>{selected.id}</dd>
                <dt>内容摘要 digest</dt>
                <dd>{selected.digest}</dd>
                <dt>创建时间</dt>
                <dd>{selected.created_at}</dd>
                {selected.confirmed_at && (
                  <>
                    <dt>确认时间</dt>
                    <dd>{selected.confirmed_at}</dd>
                  </>
                )}
                {selected.revoked_at && (
                  <>
                    <dt>撤销时间</dt>
                    <dd>{selected.revoked_at}</dd>
                    <dt>撤销原因</dt>
                    <dd>{selected.reason}</dd>
                  </>
                )}
              </dl>
              {selected.state === 'draft' && (
                <div className="knowledge-confirm">
                  <label>
                    <input
                      type="checkbox"
                      checked={reviewed}
                      disabled={busy || expired}
                      onChange={(event) => setReviewed(event.target.checked)}
                    />
                    我已核对以上完整内容、来源、版本与有效期，确认作为业务资料保存。
                  </label>
                  <button
                    className="primary-button"
                    disabled={busy || !reviewed || expired}
                    onClick={() => void change('confirm')}
                  >
                    确认这份资料
                  </button>
                </div>
              )}
              {selected.state === 'confirmed' && (
                <>
                  <button
                    className="primary-button"
                    disabled={insertDisabled}
                    onClick={() => onInsert(`[[knowledge:${selected.id}]]`)}
                  >
                    引用到本轮智能查询
                  </button>
                  <p className="muted small">
                    {!identity.model_enabled
                      ? '当前身份禁用模型，可管理资料但不能引用到智能查询。'
                      : !modelScope
                        ? '这份资料包含模型范围之外的表，不能用于智能查询。'
                        : expired
                          ? '资料已过期，不能引用。'
                          : '仅插入引用，不自动发送。执行前仍会重新核对资料与数据权限。'}
                  </p>
                </>
              )}
              {selected.state !== 'revoked' && (
                <div className="knowledge-revoke">
                  <label className="field-label">
                    撤销原因
                    <input
                      value={reason}
                      maxLength={500}
                      disabled={busy}
                      onChange={(event) => setReason(event.target.value)}
                    />
                  </label>
                  <button
                    className="secondary-button"
                    disabled={busy || !reason.trim()}
                    onClick={() => void change('revoke')}
                  >
                    撤销资料
                  </button>
                </div>
              )}
            </>
          ) : (
            !busy && <p className="muted">选择一份资料查看完整内容，或新建待确认资料。</p>
          )}
        </section>
      </div>
    </div>
  );
}
