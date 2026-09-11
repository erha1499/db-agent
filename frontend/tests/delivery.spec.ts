// These are HTTP contract doubles; live Web/MySQL/model evidence is separate.
import { test, expect } from '@playwright/test';
import type { Page } from '@playwright/test';
import { readFile } from 'node:fs/promises';

const identity = {
  username: 'analyst',
  display_name: '合成测试用户',
  authorization_version: 'v1',
  allowed_tables: ['orders'],
  model_tables: ['orders'],
  model_enabled: true,
};
const auth = {
  authenticated: true,
  session_id: 'test-session',
  identity,
  model_boundary: '合成 HTTP 替身：原文与获准结构发送模型，结果行不发送。',
};

const stamp = '2026-09-11T10:00:00Z';
const snapshot = {
  version: 'db-agent-result-v1',
  conversation_id: 'a',
  run_id: 'run-a',
  result_id: 'result-a',
  finished_at: stamp,
  prompt: '查询订单趋势',
  sql: 'SELECT paid_at, total FROM orders ORDER BY paid_at LIMIT 100',
  report: {
    result_id: 'result-a',
    status: 'ok',
    decision: 'ALLOW',
    execution_status: 'completed',
    result: {
      columns: [
        { name: 'value', type: 'date' },
        { name: 'value', type: 'decimal' },
      ],
      rows: [
        ['2026-01-01', '9007199254740993.01'],
        ['2026-03-01', '0.10'],
      ],
      row_count: 2,
      truncated: false,
      truncation_reason: null,
      result_bytes: 300,
      server_statement_status: 'completed',
    },
  },
  notes: [
    '分析仅针对本次已返回的结果行，仍受原 SQL 的 WHERE / LIMIT 等范围限制。',
    '当前 SQL 的结果已完整返回。',
  ],
  analysis: null,
};
const analysis = {
  selection: { dimension: 0, measure: 1, kind: 'comparison' },
  dimension_label: '1. value',
  measure_label: '2. value',
  aggregation: 'sum',
  points: [
    {
      dimension: '2026-01-01',
      label: '"2026-01-01"',
      sum: '9007199254740993.01',
      position: 1,
      row_count: 1,
      non_null_count: 1,
    },
    {
      dimension: '2026-03-01',
      label: '"2026-03-01"',
      sum: '0.10',
      position: 0,
      row_count: 1,
      non_null_count: 1,
    },
  ],
  zero_position: 0,
  minimum: '0.10',
  maximum: '9007199254740993.01',
  first_to_last_difference: '-9007199254740992.91',
  notes: ['仅针对返回行', 'NULL 不参与求和', '等距显示已返回时间点', '精确值以表格为准'],
};

async function setup(
  page: Page,
  options: {
    truncated?: boolean;
    empty?: boolean;
    gone?: boolean;
    failExport?: boolean;
    expiredExport?: boolean;
    mobile?: boolean;
  } = {},
) {
  const data = structuredClone(snapshot);
  if (options.truncated) {
    data.report.result.truncated = true;
    data.notes[1] = '结果已截断，只分析已返回部分，不能推断原查询总量。';
  }
  if (options.empty) {
    data.report.result.rows = [];
    data.report.result.row_count = 0;
  }
  const run = {
    id: 'run-a',
    conversation_id: 'a',
    request_id: 'request-a',
    prompt: data.prompt,
    mode: 'chat',
    created_at: stamp,
    finished_at: stamp,
    status: 'completed',
    answer: '可信结果',
    error: null,
    queries: [{ sql: data.sql, report: data.report }],
    analyses: [],
    events: [],
  };
  const conversation = {
    id: 'a',
    title: '交付分析',
    created_at: stamp,
    updated_at: stamp,
    runs: [run],
  };
  const calls: string[] = [];
  const exported: any[] = [];
  await page.route('**/api/**', async (route) => {
    const request = route.request(),
      path = new URL(request.url()).pathname.slice(4);
    calls.push(`${request.method()} ${path}`);
    if (path === '/auth/session') return route.fulfill({ json: auth });
    if (path === '/status')
      return route.fulfill({
        json: {
          identity,
          model_boundary: auth.model_boundary,
          database: 'db_agent',
          database_configured: true,
          model_configured: true,
          read_only: true,
        },
      });
    if (path === '/conversations')
      return route.fulfill({ json: { conversations: [conversation] } });
    if (path === '/conversations/a') return route.fulfill({ json: conversation });
    const resultPath = '/conversations/a/runs/run-a/results/result-a';
    if (path.startsWith(resultPath) && options.gone)
      return route.fulfill({
        status: 404,
        json: { error: { message: '结果不存在或不属于当前会话。' } },
      });
    if (path === resultPath) return route.fulfill({ json: data });
    if (path === resultPath + '/analysis')
      return route.fulfill({
        json: {
          ...data,
          analysis: {
            ...analysis,
            selection: request.postDataJSON(),
            points: options.empty ? [] : analysis.points,
          },
        },
      });
    if (path === resultPath + '/export') {
      const selected = request.postDataJSON();
      exported.push(selected);
      if (options.expiredExport)
        return route.fulfill({ status: 401, json: { error: { message: '登录已失效。' } } });
      if (options.failExport)
        return route.fulfill({ status: 503, json: { error: { message: '无法读取本机历史。' } } });
      return route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          ...data,
          analysis: selected.analysis ? { ...analysis, selection: selected.analysis } : null,
        }),
      });
    }
    return route.fulfill({ status: 404, json: { error: { message: 'Unexpected synthetic API' } } });
  });
  await page.goto('/');
  if (options.mobile) await page.getByRole('button', { name: '打开会话列表' }).click();
  await page.getByRole('button', { name: '交付分析', exact: true }).click();
  await page.getByRole('tab', { name: '分析与交付', exact: true }).click();
  return { calls, exported };
}

test('exact values, trends and selected analysis export do not run queries', async ({ page }) => {
  const { calls, exported } = await setup(page);
  await page.getByRole('button', { name: '生成分析', exact: true }).click();
  await expect(page.getByRole('img', { name: '已返回结果分组对比图' })).toBeVisible();
  await expect(page.locator('.analysis-table')).toContainText('9007199254740993.01');
  await page.getByLabel('图表类型').selectOption('trend');
  await expect(page.locator('.analysis-table')).toHaveCount(0);
  await page.getByRole('button', { name: '生成分析', exact: true }).click();
  await expect(page.getByRole('img', { name: '已返回时间点趋势图' })).toBeVisible();
  await expect(page.locator('.analysis-summary')).toContainText('-9007199254740992.91');
  const downloading = page.waitForEvent('download');
  await page.getByRole('button', { name: '下载 JSON 快照', exact: true }).click();
  const download = await downloading;
  const downloaded = JSON.parse(await readFile((await download.path())!, 'utf-8'));
  expect(downloaded.report.result.rows).toEqual(snapshot.report.result.rows);
  expect(exported[0].analysis.kind).toBe('trend');
  await page.getByText('结果来源与范围', { exact: true }).click();
  await expect(page.locator('.delivery-provenance')).toContainText('LIMIT 100');
  expect(calls.filter((call) => /^POST .*\/runs$/.test(call))).toHaveLength(0);
});

test('changing selection clears old analysis and raw export has no stale analysis', async ({
  page,
}) => {
  const { exported } = await setup(page);
  await page.getByRole('button', { name: '生成分析', exact: true }).click();
  await expect(page.locator('.analysis-table')).toBeVisible();
  await page.getByLabel('图表类型').selectOption('trend');
  await page.getByLabel('分析维度').selectOption('1');
  await expect(page.getByLabel('图表类型')).toHaveValue('comparison');
  await expect(page.getByRole('button', { name: '生成分析', exact: true })).toBeDisabled();
  await expect(page.locator('.analysis-table')).toHaveCount(0);
  const downloading = page.waitForEvent('download');
  await page.getByRole('button', { name: '下载 JSON 快照', exact: true }).click();
  await downloading;
  expect(exported[0].analysis).toBeNull();
});

test('mobile truncated analysis retains scope without page overflow', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await setup(page, { truncated: true, mobile: true });
  await expect(page.locator('.delivery-scope')).toContainText('部分结果 · 已截断');
  await page.getByRole('button', { name: '生成分析', exact: true }).click();
  await expect(page.locator('.delivery-scope')).toContainText('不能推断原查询总量');
  expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
});

test('empty chart is honest and report remains exportable', async ({ page }) => {
  await setup(page, { empty: true });
  await page.getByRole('button', { name: '生成分析', exact: true }).click();
  await expect(page.getByText('当前结果为空，没有可绘制的数据点。')).toBeVisible();
  await expect(page.getByRole('button', { name: '下载 HTML 报告' })).toBeEnabled();
});

test('unavailable saved result cannot export stale browser data', async ({ page }) => {
  await setup(page, { gone: true });
  await expect(page.getByRole('alert')).toContainText('结果不存在或不属于当前会话');
  await expect(page.getByRole('button', { name: '下载 JSON 快照' })).toHaveCount(0);
});

test('failed download remains visible without success claim', async ({ page }) => {
  await setup(page, { failExport: true });
  await page.getByRole('button', { name: '下载 JSON 快照' }).click();
  await expect(page.getByRole('alert')).toContainText('无法读取本机历史');
  await expect(page.getByText('JSON 快照已下载', { exact: false })).toHaveCount(0);
});

test('export 401 removes saved result, chart and history instead of downloading cached data', async ({
  page,
}) => {
  await setup(page, { expiredExport: true });
  await page.getByRole('button', { name: '生成分析', exact: true }).click();
  await expect(page.locator('.analysis-table')).toBeVisible();
  const downloads: unknown[] = [];
  page.on('download', (download) => downloads.push(download));
  await page.getByRole('button', { name: '下载 JSON 快照' }).click();
  await expect(page.getByRole('heading', { name: '登录本机工作区' })).toBeVisible();
  await expect(page.locator('.app-shell, .analysis-table, .result-table')).toHaveCount(0);
  await expect(page.locator('body')).not.toContainText('9007199254740993.01');
  expect(downloads).toHaveLength(0);
});
