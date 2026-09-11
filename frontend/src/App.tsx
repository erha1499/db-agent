import { useCallback, useEffect, useId, useRef, useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import {
  ArrowDown,
  ArrowUp,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  CircleAlert,
  CircleCheck,
  Copy,
  Database,
  FileSearch,
  History,
  LoaderCircle,
  Menu,
  MessageSquare,
  Moon,
  PanelLeftClose,
  PanelRight,
  Pencil,
  Plus,
  Search,
  Square,
  Sun,
  Table2,
  Terminal,
  Trash2,
  X,
} from 'lucide-react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import hljs from 'highlight.js/lib/core';
import sql from 'highlight.js/lib/languages/sql';
import { api, message } from './api';
import type {
  AppStatus,
  Artifact,
  Conversation,
  QueryResult,
  Report,
  Run,
  RunEvent,
  Schema,
} from './types';

hljs.registerLanguage('sql', sql);
const active = (run: Run) => run.status === 'running' || run.status === 'cancelling';
const operations: Record<string, string> = {
  describe_table: '读取表结构',
  list_tables: '查看授权表',
  analyze_sql: '诊断 SQL',
  execute_query: '受控查询',
  semantic_review: '复核查询需求',
  query_intent: '核对业务口径',
};

function CopyButton({ text, label = '复制' }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState(false);
  useEffect(() => {
    if (copied) {
      const timer = setTimeout(() => setCopied(false), 1800);
      return () => clearTimeout(timer);
    }
  }, [copied]);
  return (
    <button
      className="text-button"
      title={error ? '复制失败，请手动选择文本' : label}
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setCopied(true);
          setError(false);
        } catch {
          setError(true);
        }
      }}
    >
      {copied ? <Check size={14} /> : <Copy size={14} />}
      {copied ? '已复制' : error ? '请手动复制' : label}
    </button>
  );
}

function Dialog({
  title,
  children,
  onClose,
  className = '',
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  className?: string;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = ref.current!;
    dialog.showModal();
    return () => dialog.close();
  }, []);
  return (
    <dialog
      ref={ref}
      className={`dialog ${className}`}
      onCancel={onClose}
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
      aria-label={title}
    >
      <div className="dialog-content">
        <header>
          <h2>{title}</h2>
          <button className="icon-button" aria-label="关闭" onClick={onClose}>
            <X size={19} />
          </button>
        </header>
        {children}
      </div>
    </dialog>
  );
}

function ResultTable({ result }: { result: QueryResult }) {
  const [page, setPage] = useState(0);
  const count = 15;
  const pages = Math.max(1, Math.ceil(result.rows.length / count));
  return (
    <>
      {result.truncated && (
        <div className="notice warning">
          <CircleAlert size={16} />
          仅返回部分结果，不能据此判断原查询总量。
        </div>
      )}
      <div className="table-scroll">
        <table className="result-table">
          <thead>
            <tr>
              <th className="row-index">#</th>
              {result.columns.map((column, index) => (
                <th key={index} scope="col">
                  {column.name}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {result.rows.slice(page * count, (page + 1) * count).map((row, ri) => (
              <tr key={ri}>
                <td className="row-index">{page * count + ri + 1}</td>
                {row.map((value, ci) => (
                  <td
                    key={ci}
                    className={
                      typeof value === 'number' ||
                      (typeof value === 'string' && /^-?\d+(\.\d+)?$/.test(value))
                        ? 'numeric'
                        : ''
                    }
                  >
                    {value === null ? (
                      <span className="null-value">NULL</span>
                    ) : typeof value === 'boolean' ? (
                      String(value)
                    ) : (
                      value
                    )}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {result.rows.length === 0 && (
        <div className="empty-result">
          <Search size={23} />
          <strong>没有匹配的数据</strong>
          <span>查询已完成，可以调整筛选条件后再次查询。</span>
        </div>
      )}
      <div className="result-footer">
        <span>
          返回 {result.row_count} 行 · {result.truncated ? '结果已截断' : '当前 SQL 结果完整'}
        </span>
        {pages > 1 && (
          <div className="pagination">
            <button
              className="icon-button"
              disabled={page === 0}
              aria-label="上一页"
              onClick={() => setPage(page - 1)}
            >
              <ChevronLeft size={15} />
            </button>
            <span>
              {page + 1} / {pages}
            </span>
            <button
              className="icon-button"
              disabled={page + 1 === pages}
              aria-label="下一页"
              onClick={() => setPage(page + 1)}
            >
              <ChevronRight size={15} />
            </button>
          </div>
        )}
      </div>
    </>
  );
}

function Diagnosis({ report }: { report: Report }) {
  const labels: Record<string, string> = {
    ALLOW: '检查通过',
    REVIEW: '需要风险审核',
    BLOCK: '检查未通过',
    UNKNOWN: '证据不足',
  };
  const good = report.decision === 'ALLOW';
  return (
    <div className="diagnosis">
      <div className={`diagnosis-summary ${good ? 'success' : 'warning-text'}`}>
        {good ? <CircleCheck size={18} /> : <CircleAlert size={18} />}
        <strong>{labels[report.decision] || '未取得结论'}</strong>
        <code>{report.decision}</code>
      </div>
      {report.findings?.map((finding, index) => (
        <div className="finding" key={index}>
          <p>{finding.message}</p>
          <code>{finding.rule_id}</code>
        </div>
      ))}
      {!report.findings?.length && <p className="muted">未触发当前检查规则。</p>}
      {!!report.plan_summary?.tables?.length && (
        <>
          <h4>执行计划估算</h4>
          <div className="table-scroll">
            <table className="result-table">
              <thead>
                <tr>
                  <th>表</th>
                  <th>访问方式</th>
                  <th>索引</th>
                  <th>估算扫描行</th>
                </tr>
              </thead>
              <tbody>
                {report.plan_summary.tables.map((table, index) => (
                  <tr key={index}>
                    <td>{String(table.table_name ?? table.table ?? table.name ?? '—')}</td>
                    <td>{String(table.access_type ?? '—')}</td>
                    <td>{String(table.key ?? '—')}</td>
                    <td>{String(table.rows_examined_per_scan ?? '—')}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
      <details className="limitations">
        <summary>诊断范围与结果说明</summary>
        <ul>
          {(report.limitations || ['本次仍由执行入口重新检查；预检通过不代表查询已完成。']).map(
            (item, index) => (
              <li key={index}>{item}</li>
            ),
          )}
        </ul>
      </details>
    </div>
  );
}

function EvidenceCard({ artifact, kind }: { artifact: Artifact; kind: 'query' | 'analysis' }) {
  const cardId = useId();
  const { report } = artifact;
  const [tab, setTab] = useState(kind === 'query' ? 'result' : 'diagnosis');
  const isComplete = report.status === 'ok' && !!report.result;
  const status =
    kind === 'analysis'
      ? 'SQL 诊断'
      : isComplete
        ? report.result?.truncated
          ? '结果已截断'
          : '查询完成'
        : report.execution_status === 'not_started' || report.status === 'rejected'
          ? '未执行查询'
          : '未取得可确认的结果';
  const tabs =
    kind === 'query'
      ? [
          ['result', '查询结果'],
          ['sql', 'SQL'],
          ['diagnosis', '诊断'],
        ]
      : [
          ['diagnosis', '诊断'],
          ['sql', 'SQL'],
        ];
  return (
    <section className="evidence-card" aria-label={status}>
      <div className="evidence-heading">
        <span className={isComplete ? 'success' : ''}>
          {kind === 'query' ? <Table2 size={15} /> : <FileSearch size={15} />}
          {status}
        </span>
        {report.duration_ms != null && (
          <span className="muted">总耗时 {(report.duration_ms / 1000).toFixed(2)} 秒</span>
        )}
      </div>
      <div className="evidence-tabs" role="tablist" aria-label="查询详情">
        {tabs.map(([id, label], index) => (
          <button
            key={id}
            id={`${cardId}-${id}`}
            aria-controls={`${cardId}-panel`}
            type="button"
            role="tab"
            aria-selected={tab === id}
            tabIndex={tab === id ? 0 : -1}
            onClick={() => setTab(id)}
            onKeyDown={(event) => {
              if (event.key === 'ArrowRight' || event.key === 'ArrowLeft') {
                event.preventDefault();
                const next =
                  (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
                setTab(tabs[next][0]);
                (event.currentTarget.parentElement?.children[next] as HTMLElement).focus();
              }
            }}
          >
            {label}
          </button>
        ))}
      </div>
      <div id={`${cardId}-panel`} role="tabpanel" aria-labelledby={`${cardId}-${tab}`}>
        {tab === 'result' &&
          (report.result ? (
            <ResultTable result={report.result} />
          ) : (
            <div className="no-result">
              <CircleAlert size={21} />
              <div>
                <strong>{status}</strong>
                <p>
                  {report.error?.message ||
                    report.findings?.[0]?.message ||
                    '查看诊断了解本次检查结论。'}
                </p>
              </div>
            </div>
          ))}
        {tab === 'sql' && (
          <>
            <div className="code-actions">
              <span>MySQL</span>
              <CopyButton text={artifact.sql} label="复制 SQL" />
            </div>
            <pre className="sql-code">
              <code
                dangerouslySetInnerHTML={{
                  __html: hljs.highlight(artifact.sql, { language: 'sql' }).value,
                }}
              />
            </pre>
          </>
        )}
        {tab === 'diagnosis' && <Diagnosis report={report} />}
      </div>
    </section>
  );
}

function eventLabel(event: RunEvent) {
  if (event.event === 'run_started') return '开始处理请求';
  if (event.event === 'run_finished')
    return event.status === 'ok' ? '本次运行结束' : '本次运行已结束';
  if (event.operation)
    return `${operations[event.operation] || event.operation}${event.status === 'error' ? '未完成' : '完成'}`;
  if (event.event === 'model_finished')
    return event.status === 'error' ? '模型请求未完成' : '模型响应完成';
  return '运行状态更新';
}

function RunMessage({
  run,
  onRetry,
}: {
  run: Run;
  onRetry: (prompt: string, mode: Run['mode']) => void;
}) {
  const pending = active(run);
  return (
    <article className="run-message">
      <div className="user-message">
        <div>
          {run.mode === 'analyze' && (
            <span className="mode-badge">
              <FileSearch size={12} />
              仅诊断 SQL
            </span>
          )}
          <p>{run.prompt}</p>
        </div>
      </div>
      <div className="assistant-head">
        <span className="assistant-avatar">
          <Database size={16} />
        </span>
        <strong>DB Agent</strong>
        <span className="message-time">
          {new Date(run.created_at).toLocaleTimeString('zh-CN', {
            hour: '2-digit',
            minute: '2-digit',
          })}
        </span>
      </div>
      <div className="assistant-body">
        {pending && (
          <div className="running-label" role="status">
            <LoaderCircle size={16} className="spin" />
            {run.status === 'cancelling' ? '正在停止运行…' : '正在处理你的请求…'}
          </div>
        )}
        {run.queries.length > 0 ? (
          <p className="answer-summary">
            {run.queries.every((q) => q.report.status === 'ok' && q.report.result)
              ? `已取得 ${run.queries.length === 1 ? '' : `${run.queries.length} 份`}查询结果，数据和使用的 SQL 如下。`
              : '本次查询的执行状态与检查结果如下。'}
          </p>
        ) : (
          run.answer && (
            <div className="markdown">
              <Markdown
                remarkPlugins={[remarkGfm]}
                skipHtml
                components={{
                  a: (props) => (
                    <a href={props.href} target="_blank" rel="noopener noreferrer">
                      {props.children}
                    </a>
                  ),
                }}
              >
                {run.answer}
              </Markdown>
            </div>
          )
        )}
        {run.queries.map((artifact, index) => (
          <EvidenceCard key={`q-${index}`} artifact={artifact} kind="query" />
        ))}
        {!!run.missing_query_reports && (
          <div className="notice warning" role="alert">
            <CircleAlert size={16} />有 {run.missing_query_reports}{' '}
            次查询调用缺少报告，不能确认这些查询的执行结果。
          </div>
        )}
        {run.analyses.map((artifact, index) => (
          <EvidenceCard key={`a-${index}`} artifact={artifact} kind="analysis" />
        ))}
        {run.error && (
          <div className="notice error" role="alert">
            <CircleAlert size={18} />
            <div>
              {run.error.message}
              <span className="error-code">{run.error.code}</span>
            </div>
          </div>
        )}
        {!!run.events.length && (
          <details className="execution-trace">
            <summary>
              {pending ? '执行进度' : '查看执行过程'}
              <ChevronDown size={13} />
              <span>
                {run.events.filter((event) => event.event.endsWith('_finished')).length} 项记录
              </span>
            </summary>
            <ol>
              {run.events.map((event) => (
                <li key={event.seq}>
                  <span className={`trace-dot ${event.status === 'error' ? 'failed' : ''}`} />
                  <span>{eventLabel(event)}</span>
                  {event.duration_ms != null && (
                    <time>{(event.duration_ms / 1000).toFixed(2)} 秒</time>
                  )}
                </li>
              ))}
            </ol>
          </details>
        )}
        {!pending && (
          <div className="message-actions">
            {run.answer && <CopyButton text={run.answer} label="复制回答" />}
            {run.error && (
              <button className="text-button" onClick={() => onRetry(run.prompt, run.mode)}>
                编辑后重试
              </button>
            )}
          </div>
        )}
      </div>
    </article>
  );
}

function SchemaDrawer({ onClose }: { onClose: () => void }) {
  const [tables, setTables] = useState<{ name: string }[]>([]);
  const [table, setTable] = useState('');
  const [schema, setSchema] = useState<Schema | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    api<{ tables: { name: string }[] }>('/schema/tables', { signal: controller.signal })
      .then((data) => {
        setTables(data.tables);
        setLoading(false);
      })
      .catch((error) => {
        if (!controller.signal.aborted) {
          setError(message(error));
          setLoading(false);
        }
      });
    return () => controller.abort();
  }, []);
  useEffect(() => {
    if (!table) return;
    const controller = new AbortController();
    setLoading(true);
    setSchema(null);
    setError('');
    api<Schema>(`/schema/tables/${encodeURIComponent(table)}`, { signal: controller.signal })
      .then((data) => {
        setSchema(data);
        setLoading(false);
      })
      .catch((error) => {
        if (!controller.signal.aborted) {
          setError(message(error));
          setLoading(false);
        }
      });
    return () => controller.abort();
  }, [table]);
  return (
    <Dialog title="数据库结构" onClose={onClose} className="schema-dialog">
      <p className="muted small">仅展示当前授权范围内的实际表结构。</p>
      {error && (
        <div className="notice error" role="alert">
          {error}
        </div>
      )}
      {tables.length > 0 && (
        <label className="field-label">
          数据表
          <select
            aria-label="数据表"
            value={table}
            onChange={(event) => {
              setTable(event.target.value);
              if (!event.target.value) {
                setSchema(null);
                setError('');
                setLoading(false);
              }
            }}
          >
            <option value="">选择一张表</option>
            {tables.map((item) => (
              <option key={item.name} value={item.name}>
                {item.name}
              </option>
            ))}
          </select>
        </label>
      )}
      {loading && (
        <div className="panel-loading">
          <LoaderCircle className="spin" size={20} />
          读取结构中…
        </div>
      )}
      {!loading && !error && !tables.length && (
        <div className="empty-result">
          <Table2 />
          <strong>没有可见的数据表</strong>
          <span>请核对服务端表白名单与数据库权限。</span>
        </div>
      )}
      {schema && (
        <>
          <h3>
            字段 <span className="muted">{schema.columns.length}</span>
          </h3>
          <div className="schema-columns">
            {schema.columns.map((column) => (
              <div className="schema-column" key={column.name}>
                <code>{column.name}</code>
                <span>{column.type}</span>
                <span className="nullable">{column.nullable === 'YES' ? '可空' : '非空'}</span>
              </div>
            ))}
          </div>
          <h3>索引</h3>
          {schema.indexes.length ? (
            <div className="schema-indexes">
              {schema.indexes.map((index, n) => (
                <div key={n}>
                  <code>{index.name}</code>
                  <span>
                    {index.column} · 第 {index.position} 列{index.unique ? ' · 唯一' : ''}
                  </span>
                </div>
              ))}
            </div>
          ) : (
            <p className="muted small">未返回索引。</p>
          )}
          <h3>已声明的外键</h3>
          {schema.foreign_keys.length ? (
            schema.foreign_keys.map((key) => (
              <div className="foreign-key" key={key.name}>
                <code>{key.name}</code>
                <p>
                  {key.columns.join(', ')} → {key.referenced_table} (
                  {key.referenced_columns.join(', ')})
                </p>
              </div>
            ))
          ) : (
            <p className="muted small">当前授权范围内未返回外键关系。</p>
          )}
        </>
      )}
    </Dialog>
  );
}

export default function App() {
  const [status, setStatus] = useState<AppStatus | null>(null);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [conversation, setConversation] = useState<Conversation | null>(null);
  const [initialLoading, setInitialLoading] = useState(true);
  const [loadingConversation, setLoadingConversation] = useState(false);
  const [draft, setDraft] = useState('');
  const [mode, setMode] = useState<Run['mode']>('chat');
  const [sending, setSending] = useState(false);
  const [error, setError] = useState('');
  const [search, setSearch] = useState('');
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [schemaOpen, setSchemaOpen] = useState(false);
  const [dialog, setDialog] = useState<'rename' | 'delete' | null>(null);
  const [dialogTarget, setDialogTarget] = useState<Conversation | null>(null);
  const [isMobile, setIsMobile] = useState(() => window.matchMedia('(max-width:800px)').matches);
  const [title, setTitle] = useState('');
  const [theme, setTheme] = useState(() => localStorage.getItem('db-agent-theme') || 'system');
  const dark =
    theme === 'dark' ||
    (theme === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches);
  const [pollError, setPollError] = useState('');
  const [showBottom, setShowBottom] = useState(false);
  const [pollGeneration, setPollGeneration] = useState(0);
  const [pendingSyncRun, setPendingSyncRun] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const sidebarRef = useRef<HTMLElement>(null);
  const menuRef = useRef<HTMLButtonElement>(null);
  const nearBottom = useRef(true);
  const selectedRef = useRef<string | null>(selected);
  selectedRef.current = selected;
  const requestRef = useRef<{
    id: string;
    prompt: string;
    mode: string;
    conversation: string;
  } | null>(null);
  const runs = conversation?.runs || [];
  const currentRun = conversation?.id === selected ? runs.find(active) : undefined;
  const globallyBusy = !!status?.active_run_id || !!currentRun;

  const refreshList = useCallback(async () => {
    const [list, nextStatus] = await Promise.all([
      api<{ conversations: Conversation[] }>('/conversations'),
      api<AppStatus>('/status'),
    ]);
    setConversations(list.conversations);
    setStatus(nextStatus);
  }, []);
  useEffect(() => {
    let live = true;
    refreshList()
      .catch((error) => {
        if (live) setError(message(error));
      })
      .finally(() => {
        if (live) setInitialLoading(false);
      });
    return () => {
      live = false;
    };
  }, [refreshList]);
  useEffect(() => {
    if (theme === 'system') delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = theme;
    localStorage.setItem('db-agent-theme', theme);
  }, [theme]);
  useEffect(() => {
    const media = window.matchMedia('(max-width:800px)');
    const change = () => {
      setIsMobile(media.matches);
      if (!media.matches) setSidebarOpen(false);
    };
    media.addEventListener('change', change);
    return () => media.removeEventListener('change', change);
  }, []);
  useEffect(() => {
    if (isMobile && sidebarOpen) {
      sidebarRef.current?.querySelector<HTMLButtonElement>('button')?.focus();
      return () => menuRef.current?.focus();
    }
  }, [isMobile, sidebarOpen]);
  useEffect(() => {
    if (!selected) {
      setConversation(null);
      setLoadingConversation(false);
      return;
    }
    const controller = new AbortController();
    setLoadingConversation(true);
    setError('');
    api<Conversation>(`/conversations/${selected}`, { signal: controller.signal })
      .then((item) => {
        setConversation((previous) =>
          previous?.id === item.id
            ? {
                ...item,
                runs: [
                  ...(item.runs || []),
                  ...(previous.runs || []).filter(
                    (run) => !(item.runs || []).some((saved) => saved.id === run.id),
                  ),
                ],
              }
            : item,
        );
        nearBottom.current = true;
      })
      .catch((error) => {
        if (!controller.signal.aborted) setError(message(error));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoadingConversation(false);
      });
    return () => controller.abort();
  }, [selected]);
  useEffect(() => {
    const runId = currentRun?.id || status?.active_run_id || pendingSyncRun;
    if (!runId) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const run = await api<Run>(`/runs/${runId}`);
        if (stopped) return;
        setPollError('');
        if (!active(run)) {
          let snapshot;
          try {
            snapshot = await Promise.all([
              api<Conversation>(`/conversations/${run.conversation_id}`),
              api<{ conversations: Conversation[] }>('/conversations'),
              api<AppStatus>('/status'),
            ]);
          } catch (error) {
            if (stopped) return;
            if (run.conversation_id === selectedRef.current)
              setConversation((previous) =>
                previous
                  ? {
                      ...previous,
                      runs: [...(previous.runs || []).filter((item) => item.id !== run.id), run],
                    }
                  : previous,
              );
            setStatus((previous) =>
              previous?.active_run_id === run.id ? { ...previous, active_run_id: null } : previous,
            );
            setPendingSyncRun(run.id);
            setPollError(`已取得本轮最终状态，历史暂时无法同步：${message(error)}。不会重新执行。`);
            timer = setTimeout(poll, 3000);
            return;
          }
          if (stopped) return;
          const [updated, list, nextStatus] = snapshot;
          if (run.conversation_id === selectedRef.current) setConversation(updated);
          setConversations(list.conversations);
          setStatus(nextStatus);
          setPendingSyncRun(null);
          return;
        }
        if (run.conversation_id === selectedRef.current)
          setConversation((previous) =>
            previous
              ? {
                  ...previous,
                  runs: [...(previous.runs || []).filter((item) => item.id !== run.id), run],
                }
              : previous,
          );
      } catch (error) {
        if (!stopped)
          setPollError(
            `运行状态暂时无法同步：${message(error)} 页面恢复后会继续获取，不会重新执行。`,
          );
      }
      if (!stopped) timer = setTimeout(poll, 1000);
    };
    timer = setTimeout(poll, 500);
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [currentRun?.id, status?.active_run_id, pendingSyncRun, refreshList, pollGeneration]);
  useEffect(() => {
    if (nearBottom.current && scrollRef.current)
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [conversation, loadingConversation]);
  useEffect(() => {
    const input = inputRef.current;
    if (input) {
      input.style.height = 'auto';
      input.style.height = `${Math.min(input.scrollHeight, 180)}px`;
    }
  }, [draft]);

  const chooseConversation = (id: string | null) => {
    if (sending) return;
    setSidebarOpen(false);
    if (id !== null && id === selectedRef.current) return;
    setSelected(id);
    setConversation(null);
    setDraft('');
    setError('');
    requestRef.current = null;
    nearBottom.current = true;
  };
  async function send(event?: FormEvent) {
    event?.preventDefault();
    if (!draft.trim() || disabled || globallyBusy) return;
    setSending(true);
    setError('');
    let id = selected;
    try {
      if (!id) {
        const created = await api<Conversation>('/conversations', { method: 'POST', body: '{}' });
        id = created.id;
        selectedRef.current = id;
        setSelected(id);
        setConversation({ ...created, runs: [] });
      }
      if (
        !requestRef.current ||
        requestRef.current.prompt !== draft ||
        requestRef.current.mode !== mode ||
        requestRef.current.conversation !== id
      )
        requestRef.current = { id: crypto.randomUUID(), prompt: draft, mode, conversation: id };
      const run = await api<Run>(`/conversations/${id}/runs`, {
        method: 'POST',
        body: JSON.stringify({ prompt: draft, mode, request_id: requestRef.current.id }),
      });
      if (selectedRef.current === id) {
        setConversation((previous) =>
          previous
            ? {
                ...previous,
                runs: [...(previous.runs || []).filter((item) => item.id !== run.id), run],
              }
            : {
                id: id!,
                title: '新建对话',
                created_at: run.created_at,
                updated_at: run.created_at,
                runs: [run],
              },
        );
        setDraft((current) => (current === draft ? '' : current));
      }
      setStatus((previous) =>
        previous ? { ...previous, active_run_id: active(run) ? run.id : null } : previous,
      );
      nearBottom.current = true;
      requestRef.current = null;
      await refreshList();
    } catch (error) {
      setError(message(error));
      void refreshList().catch(() => {});
    } finally {
      setSending(false);
      inputRef.current?.focus();
    }
  }
  async function stopRun() {
    const id = currentRun?.id || status?.active_run_id;
    if (!id) return;
    try {
      const run = await api<Run>(`/runs/${id}/cancel`, { method: 'POST', body: '{}' });
      setConversation((previous) =>
        previous
          ? {
              ...previous,
              runs: (previous.runs || []).map((item) => (item.id === id ? run : item)),
            }
          : previous,
      );
    } catch (error) {
      setError(message(error));
    }
  }
  async function saveDialog(event: FormEvent) {
    event.preventDefault();
    if (!dialogTarget) return;
    const target = dialogTarget.id;
    try {
      if (dialog === 'rename') {
        const updated = await api<Conversation>(`/conversations/${target}`, {
          method: 'PATCH',
          body: JSON.stringify({ title }),
        });
        if (selectedRef.current === target) setConversation(updated);
      } else {
        await api(`/conversations/${target}`, { method: 'DELETE' });
        if (selectedRef.current === target) chooseConversation(null);
      }
      setDialog(null);
      await refreshList();
    } catch (error) {
      setDialog(null);
      setError(message(error));
    }
  }
  const filtered = conversations.filter((item) =>
    item.title.toLocaleLowerCase().includes(search.toLocaleLowerCase()),
  );
  const disabled =
    sending ||
    loadingConversation ||
    !!currentRun ||
    !status?.database_configured ||
    (mode === 'chat' && (!status.model_configured || !!conversation?.context_paused));
  return (
    <div className="app-shell">
      {sidebarOpen && (
        <button
          className="sidebar-overlay"
          aria-label="关闭会话列表"
          onClick={() => setSidebarOpen(false)}
        />
      )}
      <aside
        ref={sidebarRef}
        className={`sidebar ${sidebarOpen ? 'is-open' : ''}`}
        inert={isMobile && !sidebarOpen}
        role={isMobile && sidebarOpen ? 'dialog' : undefined}
        aria-modal={isMobile && sidebarOpen ? true : undefined}
        aria-label="会话导航"
        onKeyDown={(event) => {
          if (!isMobile || !sidebarOpen || event.key !== 'Tab') return;
          const nodes = Array.from(
            sidebarRef.current?.querySelectorAll<HTMLElement>('button:not(:disabled),input') || [],
          );
          const first = nodes[0],
            last = nodes.at(-1);
          if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last?.focus();
          } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first?.focus();
          }
        }}
      >
        <div className="brand">
          <span className="brand-icon">
            <Database size={21} />
          </span>
          <span>DB Agent</span>
          <button
            className="icon-button mobile-only"
            aria-label="收起会话列表"
            onClick={() => setSidebarOpen(false)}
          >
            <PanelLeftClose size={18} />
          </button>
        </div>
        <button className="new-chat" onClick={() => chooseConversation(null)}>
          <Plus size={18} />
          新建对话<span>⌘ K</span>
        </button>
        <label className="history-search">
          <Search size={15} />
          <input
            aria-label="搜索会话"
            placeholder="搜索对话"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
        </label>
        <div className="history-title">
          <History size={13} />
          最近对话<span>{conversations.length}</span>
        </div>
        <nav className="conversation-list" aria-label="历史会话">
          {initialLoading ? (
            <div className="sidebar-note">加载历史中…</div>
          ) : filtered.length ? (
            filtered.map((item) => (
              <button
                key={item.id}
                className={`conversation-item ${selected === item.id ? 'selected' : ''}`}
                onClick={() => chooseConversation(item.id)}
                aria-current={selected === item.id ? 'page' : undefined}
              >
                <MessageSquare size={15} />
                <span>{item.title}</span>
                {item.active_run_id && <LoaderCircle size={13} className="spin" />}
              </button>
            ))
          ) : (
            <div className="sidebar-note">{search ? '没有匹配的对话' : '你的对话将保存在这里'}</div>
          )}
        </nav>
        <footer className="sidebar-footer">
          <span className="workspace-icon">
            <Database size={17} />
          </span>
          <div>
            <strong>本地工作区</strong>
            <span>历史记录保存在本机</span>
          </div>
          <button
            className="icon-button"
            aria-label={dark ? '切换浅色外观' : '切换深色外观'}
            onClick={() => setTheme(dark ? 'light' : 'dark')}
          >
            {dark ? <Sun size={17} /> : <Moon size={17} />}
          </button>
        </footer>
      </aside>
      <main className="main-panel" inert={isMobile && sidebarOpen}>
        <header className="topbar">
          <div className="title-group">
            <button
              ref={menuRef}
              className="icon-button mobile-only"
              aria-label="打开会话列表"
              onClick={() => setSidebarOpen(true)}
            >
              <Menu size={20} />
            </button>
            <h1>{loadingConversation ? '读取对话中…' : conversation?.title || '新建对话'}</h1>
            {selected && (
              <>
                <button
                  className="icon-button title-action"
                  aria-label="重命名会话"
                  disabled={loadingConversation || !conversation}
                  onClick={() => {
                    setDialogTarget(conversation);
                    setTitle(conversation?.title || '');
                    setDialog('rename');
                  }}
                >
                  <Pencil size={15} />
                </button>
                <button
                  className="icon-button title-action"
                  aria-label="删除会话"
                  disabled={!!currentRun || loadingConversation || !conversation}
                  onClick={() => {
                    setDialogTarget(conversation);
                    setDialog('delete');
                  }}
                >
                  <Trash2 size={15} />
                </button>
              </>
            )}
          </div>
          <div className="source-controls">
            <span className="source-name">
              <Database size={14} />
              {status?.database || '未配置数据库'}
            </span>
            <span className="readonly-badge">只读</span>
            <button
              className="secondary-button schema-trigger"
              onClick={() => setSchemaOpen(true)}
              disabled={!status?.database_configured}
            >
              <PanelRight size={16} />
              <span>表结构</span>
            </button>
          </div>
        </header>
        <div
          className="message-scroll"
          ref={scrollRef}
          onScroll={() => {
            const node = scrollRef.current!;
            nearBottom.current = node.scrollHeight - node.scrollTop - node.clientHeight < 120;
            setShowBottom(!nearBottom.current);
          }}
        >
          <div className={`chat-content ${!runs.length ? 'is-empty' : ''}`}>
            {loadingConversation ? (
              <div className="panel-loading">
                <LoaderCircle className="spin" size={20} />
                读取对话中…
              </div>
            ) : runs.length ? (
              runs.map((run) => (
                <RunMessage
                  key={run.id}
                  run={run}
                  onRetry={(prompt, nextMode) => {
                    setDraft(prompt);
                    setMode(nextMode);
                    inputRef.current?.focus();
                  }}
                />
              ))
            ) : (
              <div className="welcome">
                <span className="welcome-icon">
                  <Database size={27} />
                </span>
                <h2>今天想查询什么？</h2>
                <p>用自然语言查询数据，或检查一段 SQL。</p>
                <div className="starter-grid">
                  <button
                    onClick={() => {
                      setMode('chat');
                      setDraft('当前有哪些我可以查询的数据表？');
                      inputRef.current?.focus();
                    }}
                  >
                    <Table2 size={20} />
                    <strong>了解数据结构</strong>
                    <span>看看当前有哪些可查询的数据表</span>
                  </button>
                  <button
                    onClick={() => {
                      setMode('chat');
                      setDraft('');
                      inputRef.current?.focus();
                    }}
                  >
                    <MessageSquare size={20} />
                    <strong>查询业务数据</strong>
                    <span>描述时间范围、指标和筛选条件</span>
                  </button>
                  <button
                    onClick={() => {
                      setMode('analyze');
                      setDraft('');
                      inputRef.current?.focus();
                    }}
                  >
                    <FileSearch size={20} />
                    <strong>诊断一段 SQL</strong>
                    <span>检查风险和执行计划，不执行查询</span>
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>
        <div className="composer-area">
          {showBottom && (
            <button
              className="jump-bottom icon-button"
              aria-label="回到最新消息"
              onClick={() => {
                scrollRef.current?.scrollTo({
                  top: scrollRef.current.scrollHeight,
                  behavior: 'smooth',
                });
                nearBottom.current = true;
              }}
            >
              <ArrowDown size={18} />
            </button>
          )}
          {error && (
            <div className="notice error" role="alert">
              <CircleAlert size={17} />
              <span>{error}</span>
              <button className="icon-button" aria-label="关闭提示" onClick={() => setError('')}>
                <X size={15} />
              </button>
            </div>
          )}
          {!initialLoading && !status && (
            <button
              className="secondary-button reconnect-button"
              onClick={() => {
                setInitialLoading(true);
                refreshList()
                  .then(() => setError(''))
                  .catch((error) => setError(message(error)))
                  .finally(() => setInitialLoading(false));
              }}
            >
              重新连接服务
            </button>
          )}
          {pollError && (
            <div className="notice warning" role="status">
              {pollError}
              <button
                className="text-button"
                onClick={() => setPollGeneration((value) => value + 1)}
              >
                重新同步
              </button>
            </div>
          )}
          {conversation?.context_paused && mode === 'chat' && !currentRun && (
            <div className="notice warning">
              <CircleAlert size={17} />
              <span>上轮未取得完整查询证据，连续口径已暂停。请新建对话并完整重述需求。</span>
              <button className="text-button" onClick={() => chooseConversation(null)}>
                新建对话
              </button>
            </div>
          )}
          {status &&
            (!status.database_configured || (mode === 'chat' && !status.model_configured)) && (
              <div className="notice warning">
                <CircleAlert size={17} />
                <span>{status.database_error || status.model_error}</span>
              </div>
            )}
          {globallyBusy && !currentRun && (
            <div className="notice neutral">
              <LoaderCircle size={15} className="spin" />
              <span>另一个会话正在运行。</span>
              <button
                className="text-button"
                onClick={() => {
                  const item = conversations.find(
                    (item) => item.active_run_id === status?.active_run_id,
                  );
                  if (item) chooseConversation(item.id);
                }}
              >
                查看运行
              </button>
            </div>
          )}
          <form className="composer" onSubmit={send}>
            <textarea
              ref={inputRef}
              aria-label={mode === 'chat' ? '输入数据问题' : '输入要诊断的 SQL'}
              placeholder={
                mode === 'chat'
                  ? '描述数据问题，写清时间范围和统计口径…'
                  : '粘贴完整 MySQL SQL，仅诊断，不执行…'
              }
              value={draft}
              maxLength={16384}
              rows={2}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                  event.preventDefault();
                  void send();
                }
              }}
            />
            <div className="composer-toolbar">
              <div className="mode-switch" aria-label="请求方式">
                <button
                  type="button"
                  aria-pressed={mode === 'chat'}
                  onClick={() => setMode('chat')}
                >
                  <MessageSquare size={14} />
                  智能查询
                </button>
                <button
                  type="button"
                  aria-pressed={mode === 'analyze'}
                  onClick={() => setMode('analyze')}
                >
                  <Terminal size={14} />
                  仅诊断 SQL
                </button>
              </div>
              {currentRun ? (
                <button
                  type="button"
                  className="stop-button"
                  aria-label="停止运行"
                  disabled={currentRun.status === 'cancelling'}
                  onClick={stopRun}
                >
                  <Square size={14} fill="currentColor" />
                  停止
                </button>
              ) : (
                <button
                  type="submit"
                  className="send-button"
                  aria-label="发送问题"
                  disabled={disabled || globallyBusy || !draft.trim()}
                >
                  {sending ? <LoaderCircle size={18} className="spin" /> : <ArrowUp size={19} />}
                </button>
              )}
            </div>
          </form>
          <div className="composer-hint">
            <span>
              {conversation?.context_turns
                ? `可沿用 ${conversation.context_turns} 轮成功查询的口径`
                : '查询前自动检查权限与 SQL 风险'}
            </span>
            <span>Enter 发送 · Shift + Enter 换行</span>
          </div>
        </div>
      </main>
      {schemaOpen && <SchemaDrawer onClose={() => setSchemaOpen(false)} />}
      {dialog && (
        <Dialog
          title={dialog === 'rename' ? '重命名会话' : '删除这段对话？'}
          onClose={() => setDialog(null)}
        >
          <form onSubmit={saveDialog}>
            {dialog === 'rename' ? (
              <label className="field-label">
                会话名称
                <input
                  autoFocus
                  value={title}
                  maxLength={80}
                  onChange={(event) => setTitle(event.target.value)}
                />
              </label>
            ) : (
              <p>“{dialogTarget?.title}”的消息、SQL 和结果将从本机历史中删除。</p>
            )}
            <div className="dialog-actions">
              <button type="button" className="secondary-button" onClick={() => setDialog(null)}>
                取消
              </button>
              <button
                type="submit"
                className={dialog === 'delete' ? 'danger-button' : 'primary-button'}
                disabled={dialog === 'rename' && !title.trim()}
              >
                {dialog === 'rename' ? '保存名称' : '删除对话'}
              </button>
            </div>
          </form>
        </Dialog>
      )}
      <KeyboardShortcut
        onNew={() => chooseConversation(null)}
        onClose={() => setSidebarOpen(false)}
      />
    </div>
  );
}

function KeyboardShortcut({ onNew, onClose }: { onNew: () => void; onClose: () => void }) {
  useEffect(() => {
    const listener = (event: KeyboardEvent) => {
      if (document.querySelector('dialog[open]')) return;
      if ((event.metaKey || event.ctrlKey) && event.key === 'k') {
        event.preventDefault();
        onNew();
      }
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', listener);
    return () => window.removeEventListener('keydown', listener);
  }, [onNew, onClose]);
  return null;
}
