import { useEffect, useState } from 'react';
import { BarChart3, Download, LoaderCircle, TrendingUp } from 'lucide-react';
import { api, message } from './api';
import type { AnalysisSelection, ResultAnalysis, ResultSnapshot } from './types';

const numericTypes = new Set([
  'tinyint',
  'smallint',
  'mediumint',
  'int',
  'bigint',
  'decimal',
  'float',
  'double',
  'year',
]);
const timeTypes = new Set(['date', 'datetime', 'timestamp', 'year']);

function Chart({ analysis }: { analysis: ResultAnalysis }) {
  const points = analysis.points;
  if (!points.length) return <p className="delivery-empty">当前结果为空，没有可绘制的数据点。</p>;
  if (analysis.selection.kind === 'comparison')
    return (
      <div className="comparison-chart" role="img" aria-label="已返回结果分组对比图">
        {points.map((point, index) => (
          <div className="comparison-row" key={index}>
            <span className="chart-label" title={point.label}>
              {point.label}
            </span>
            <span className="bar-track">
              <span className="bar-zero" style={{ left: `${analysis.zero_position * 100}%` }} />
              {point.position !== null && (
                <span
                  className="bar-mark"
                  style={{
                    left: `${Math.min(point.position, analysis.zero_position) * 100}%`,
                    width: `${Math.abs(point.position - analysis.zero_position) * 100}%`,
                  }}
                />
              )}
            </span>
            <span className="chart-value" title={point.sum ?? 'NULL'}>
              {point.sum ?? 'NULL'}
            </span>
          </div>
        ))}
      </div>
    );
  const width = Math.max(640, points.length * 100);
  const xy = (i: number, p: number) => [
    100 + (i * (width - 200)) / Math.max(1, points.length - 1),
    220 - p * 170,
  ];
  return (
    <div className="trend-scroll">
      <svg width={width} height={290} role="img" aria-label="已返回时间点趋势图">
        <line
          x1="40"
          x2={width - 30}
          y1={220 - analysis.zero_position * 170}
          y2={220 - analysis.zero_position * 170}
          className="trend-zero"
        />
        {points.map((point, i) => {
          const [x, y] = xy(i, point.position ?? 0);
          const previous = i ? points[i - 1].position : null;
          return (
            <g key={i}>
              {point.position !== null && previous !== null && (
                <line
                  x1={xy(i - 1, previous)[0]}
                  y1={xy(i - 1, previous)[1]}
                  x2={x}
                  y2={y}
                  className="trend-line"
                />
              )}
              {point.position !== null ? (
                <circle cx={x} cy={y} r="5" className="trend-point">
                  <title>
                    {point.label}: {point.sum}
                  </title>
                </circle>
              ) : (
                <text x={x} y="130" textAnchor="middle">
                  NULL
                </text>
              )}
              <text x={x} y="255" textAnchor="middle" transform={`rotate(-20 ${x} 255)`}>
                <title>{point.label}</title>
                {point.label.length > 16 ? `${point.label.slice(0, 16)}…` : point.label}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}

export default function ResultDelivery({ path }: { path: string }) {
  const [snapshot, setSnapshot] = useState<ResultSnapshot | null>(null);
  const [dimension, setDimension] = useState(0);
  const [measure, setMeasure] = useState(-1);
  const [kind, setKind] = useState<AnalysisSelection['kind']>('comparison');
  const [selection, setSelection] = useState<AnalysisSelection | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [downloaded, setDownloaded] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setSnapshot(null);
    api<ResultSnapshot>(path, { signal: controller.signal })
      .then((value) => {
        if (controller.signal.aborted) return;
        setSnapshot(value);
        const columns = value.report.result.columns;
        const metric = columns.findIndex(
          (c, i) => i > 0 && numericTypes.has((c.type || '').toLowerCase()),
        );
        setDimension(0);
        setMeasure(metric);
        setSelection(null);
        setError('');
      })
      .catch((error) => {
        if (!controller.signal.aborted) setError(message(error));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [path]);
  if (loading)
    return (
      <div className="delivery-panel">
        <LoaderCircle size={18} className="spin" />
        读取已保存的结果…
      </div>
    );
  if (!snapshot)
    return (
      <div className="delivery-panel notice error" role="alert">
        {error || '结果无法取回。请返回会话重试。'}
      </div>
    );
  const columns = snapshot.report.result.columns;
  const analysis = snapshot.analysis;
  const clearAnalysis = () => {
    setSelection(null);
    setSnapshot({ ...snapshot, analysis: null });
    setError('');
    setDownloaded('');
  };
  async function analyze() {
    setBusy(true);
    setError('');
    setDownloaded('');
    const next = { dimension, measure, kind };
    try {
      const value = await api<ResultSnapshot>(`${path}/analysis`, {
        method: 'POST',
        body: JSON.stringify(next),
      });
      setSnapshot(value);
      setSelection(next);
    } catch (error) {
      setError(message(error));
    } finally {
      setBusy(false);
    }
  }
  async function download(format: 'json' | 'html') {
    setBusy(true);
    setError('');
    setDownloaded('');
    try {
      const response = await fetch(`/api${path}/export`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-DB-Agent-Client': 'web' },
        body: JSON.stringify({ format, analysis: selection }),
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => null);
        throw new Error(payload?.error?.message || '下载失败，请检查本机服务后重试。');
      }
      const url = URL.createObjectURL(await response.blob());
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = `db-agent-result-${snapshot!.result_id}.${format}`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      setDownloaded(
        format === 'html'
          ? 'HTML 报告已下载，可在浏览器打开或打印。'
          : 'JSON 快照已下载，包含原始结果及当前已生成的分析。',
      );
    } catch (error) {
      setError(message(error));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="delivery-panel">
      <div className="delivery-heading">
        <div>
          <h3>从这份结果继续分析</h3>
          <p>选择维度和指标，按已返回行分组求和。</p>
        </div>
        <span className="readonly-badge">本机计算</span>
      </div>
      <div className={`delivery-scope ${snapshot.report.result.truncated ? 'is-truncated' : ''}`}>
        <strong>
          {snapshot.report.result.truncated
            ? '部分结果 · 已截断'
            : `已保存 ${snapshot.report.result.row_count} 行`}
        </strong>
        <p>{snapshot.notes[0]}</p>
        {snapshot.report.result.truncated && <p>{snapshot.notes[1]}</p>}
      </div>
      <fieldset className="analysis-controls" disabled={busy}>
        <label>
          维度
          <select
            aria-label="分析维度"
            value={dimension}
            onChange={(e) => {
              const next = Number(e.target.value);
              setDimension(next);
              if (!timeTypes.has((columns[next]?.type || '').toLowerCase())) setKind('comparison');
              clearAnalysis();
            }}
          >
            {columns.map((c, i) => (
              <option key={i} value={i}>
                {i + 1}. {c.name} ({c.type})
              </option>
            ))}
          </select>
        </label>
        <label>
          指标 · 求和
          <select
            aria-label="分析指标"
            value={measure}
            onChange={(e) => {
              setMeasure(Number(e.target.value));
              clearAnalysis();
            }}
          >
            <option value={-1}>选择数值列</option>
            {columns.map(
              (c, i) =>
                numericTypes.has((c.type || '').toLowerCase()) && (
                  <option key={i} value={i}>
                    {i + 1}. {c.name} ({c.type})
                  </option>
                ),
            )}
          </select>
        </label>
        <label>
          展示方式
          <select
            aria-label="图表类型"
            value={kind}
            onChange={(e) => {
              setKind(e.target.value as AnalysisSelection['kind']);
              clearAnalysis();
            }}
          >
            <option value="comparison">分组对比</option>
            <option
              value="trend"
              disabled={!timeTypes.has((columns[dimension]?.type || '').toLowerCase())}
            >
              时间趋势
            </option>
          </select>
        </label>
        <button
          className="primary-button"
          disabled={measure < 0 || measure === dimension}
          onClick={analyze}
        >
          {kind === 'trend' ? <TrendingUp size={16} /> : <BarChart3 size={16} />}生成分析
        </button>
      </fieldset>
      {error && (
        <div className="notice error" role="alert">
          {error}
        </div>
      )}
      {analysis ? (
        <>
          <div className="analysis-title">
            <strong>
              {analysis.dimension_label} / {analysis.measure_label}
            </strong>
            <span>分组合计 · {analysis.points.length} 组</span>
          </div>
          <Chart analysis={analysis} />
          <div className="analysis-summary">
            <div>
              <span>分组合计最小值</span>
              <strong>{analysis.minimum ?? 'NULL / 无数值'}</strong>
            </div>
            <div>
              <span>分组合计最大值</span>
              <strong>{analysis.maximum ?? 'NULL / 无数值'}</strong>
            </div>
            {kind === 'trend' && (
              <div>
                <span>最后与首个时间点差额</span>
                <strong>{analysis.first_to_last_difference ?? 'NULL / 无法计算'}</strong>
              </div>
            )}
          </div>
          <div className="table-scroll">
            <table className="result-table analysis-table">
              <thead>
                <tr>
                  <th>维度</th>
                  <th>指标合计</th>
                  <th>返回行</th>
                  <th>非 NULL 行</th>
                </tr>
              </thead>
              <tbody>
                {analysis.points.map((p, i) => (
                  <tr key={i}>
                    <td>{p.label}</td>
                    <td className="numeric">{p.sum ?? 'NULL'}</td>
                    <td>{p.row_count}</td>
                    <td>{p.non_null_count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <ul className="delivery-notes">
            {analysis.notes.slice(1).map((note, i) => (
              <li key={i}>{note}</li>
            ))}
          </ul>
        </>
      ) : (
        <p className="delivery-empty">图表使用你选择的返回列。单列或无数值结果可直接下载报告。</p>
      )}
      <div className="delivery-actions">
        <button className="secondary-button" disabled={busy} onClick={() => download('html')}>
          <Download size={15} />
          下载 HTML 报告
        </button>
        <button className="secondary-button" disabled={busy} onClick={() => download('json')}>
          <Download size={15} />
          下载 JSON 快照
        </button>
      </div>
      {downloaded && (
        <p className="success" role="status">
          {downloaded}
        </p>
      )}
      <details className="delivery-provenance">
        <summary>结果来源与范围</summary>
        <p>
          保存于 {snapshot.finished_at} · 结果 {snapshot.result_id}
        </p>
        <pre>{snapshot.sql}</pre>
        <ul>
          {snapshot.notes.map((note, i) => (
            <li key={i}>{note}</li>
          ))}
        </ul>
      </details>
    </div>
  );
}
