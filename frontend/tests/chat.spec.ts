// UI state tests use a synthetic HTTP contract. Live MySQL/model smoke is separate.
import { test, expect } from '@playwright/test';
import type { Page } from '@playwright/test';

const stamp = '2026-09-11T08:00:00Z';
const run = {
  id: 'run-a',
  conversation_id: 'a',
  request_id: 'synthetic-id',
  prompt: '查询订单金额',
  mode: 'chat',
  created_at: stamp,
  finished_at: stamp,
  status: 'completed',
  answer: '可信结果',
  error: null,
  analyses: [],
  missing_query_reports: 0,
  events: [
    {
      seq: 1,
      time: stamp,
      event: 'query_finished',
      operation: 'execute_query',
      status: 'ok',
      duration_ms: 20,
    },
  ],
  queries: [
    {
      sql: 'SELECT total_amount FROM orders',
      report: {
        status: 'ok',
        decision: 'ALLOW',
        execution_status: 'completed',
        duration_ms: 20,
        findings: [],
        result: {
          columns: [{ name: 'value' }, { name: 'value' }],
          rows: [
            ['9007199254740993', '130.00'],
            [null, '<img src=x onerror=alert(1)>'],
          ],
          row_count: 2,
          truncated: false,
          truncation_reason: null,
          result_bytes: 200,
          server_statement_status: 'completed',
        },
      },
    },
  ],
};

async function setup(
  page: Page,
  options: {
    failStartup?: boolean;
    missing?: boolean;
    slowB?: boolean;
    configured?: boolean;
    running?: boolean;
    slowSend?: boolean;
  } = {},
) {
  const data = structuredClone(run);
  if (options.missing) data.missing_query_reports = 1;
  if (options.running) {
    data.status = 'running';
    data.queries = [];
  }
  const store: Record<string, any> = {
    a: {
      id: 'a',
      title: '订单分析 A',
      created_at: stamp,
      updated_at: stamp,
      context_turns: 1,
      runs: [data],
    },
    b: { id: 'b', title: '订单分析 B', created_at: stamp, updated_at: stamp, runs: [] },
  };
  const calls: string[] = [];
  let startupFailed = false;
  await page.route('**/api/**', async (route) => {
    const request = route.request(),
      path = new URL(request.url()).pathname.slice(4);
    const method = request.method();
    calls.push(`${method} ${path}`);
    const body = request.postDataJSON();
    const send = (json: unknown, status = 200) => route.fulfill({ json, status });
    if (path === '/status') {
      if (options.failStartup && !startupFailed) {
        startupFailed = true;
        return send({ error: { message: '服务暂时不可用' } }, 503);
      }
      return send({
        database: 'db_agent',
        database_configured: options.configured !== false,
        model_configured: options.configured !== false,
        database_error: null,
        model_error: null,
        read_only: true,
        active_run_id: data.status === 'running' ? data.id : null,
      });
    }
    if (path === '/conversations') {
      if (method === 'POST') {
        store.c = { id: 'c', title: '新建对话', created_at: stamp, updated_at: stamp, runs: [] };
        return send(store.c, 201);
      }
      return send({
        conversations: Object.values(store).map((item) => ({
          ...item,
          active_run_id: item.id === 'a' && data.status === 'running' ? data.id : null,
        })),
      });
    }
    if (path === '/schema/tables') return send({ tables: [{ name: 'orders' }] });
    if (path === '/schema/tables/orders')
      return send({
        table: 'orders',
        columns: [{ name: 'total_amount', type: 'decimal(12,2)', nullable: 'YES' }],
        indexes: [],
        foreign_keys: [],
      });
    if (path === '/runs/run-a/cancel') {
      data.status = 'cancelled';
      Object.assign(data, {
        error: { code: 'CANCELLED', message: '运行已停止；未确认数据库语句最终状态。' },
      });
      return send(data);
    }
    if (path === '/runs/run-a') return send(data);
    const match = path.match(/^\/conversations\/([^/]+)(\/runs)?$/);
    if (match) {
      const id = match[1];
      if (method === 'DELETE') {
        delete store[id];
        return send({ deleted: true });
      }
      if (method === 'PATCH') {
        store[id].title = body.title;
        return send(store[id]);
      }
      if (match[2]) {
        if (options.slowSend) await new Promise((resolve) => setTimeout(resolve, 500));
        store[id].runs = [{ ...data, id: 'run-a', conversation_id: id, prompt: body.prompt }];
        return send(store[id].runs[0], 202);
      }
      if (id === 'b' && options.slowB) await new Promise((resolve) => setTimeout(resolve, 500));
      return send(store[id]);
    }
    return send({ error: { message: 'Unexpected synthetic API call' } }, 404);
  });
  await page.goto('/');
  return { calls, store, data };
}

test('renders actual contract values without losing precision, nulls, duplicate columns or escaping', async ({
  page,
}) => {
  await setup(page);
  await page.getByRole('button', { name: '订单分析 A', exact: true }).click();
  await expect(page.getByRole('cell', { name: '9007199254740993', exact: true })).toBeVisible();
  await expect(page.getByRole('cell', { name: '130.00', exact: true })).toBeVisible();
  await expect(page.getByRole('cell', { name: 'NULL', exact: true })).toBeVisible();
  await expect(page.getByRole('columnheader', { name: 'value', exact: true })).toHaveCount(2);
  await expect(page.locator('.result-table img')).toHaveCount(0);
  await page.getByRole('tab', { name: 'SQL', exact: true }).click();
  await expect(page.locator('.sql-code')).toContainText('SELECT total_amount FROM orders');
  await page.getByRole('tab', { name: '诊断', exact: true }).click();
  await expect(page.locator('.diagnosis-summary')).toContainText('检查通过');
});

test('missing evidence is explicit even when the available query succeeded', async ({ page }) => {
  await setup(page, { missing: true });
  await page.getByRole('button', { name: '订单分析 A', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('1 次查询调用缺少报告');
});

test('selecting the current conversation preserves its results and draft', async ({ page }) => {
  await setup(page);
  const selected = page.getByRole('button', { name: '订单分析 A', exact: true });
  await selected.click();
  const input = page.getByRole('textbox', { name: '输入数据问题' });
  await input.fill('继续按客户拆分');
  await selected.click();
  await expect(page.getByRole('cell', { name: '130.00', exact: true })).toBeVisible();
  await expect(input).toHaveValue('继续按客户拆分');
});

test('input edited during submission is preserved and the submitted prompt stays unchanged', async ({
  page,
}) => {
  const { store } = await setup(page, { slowSend: true });
  await page.getByRole('button', { name: '订单分析 B', exact: true }).click();
  const input = page.getByRole('textbox', { name: '输入数据问题' });
  await input.fill('第一条问题');
  await page.getByRole('button', { name: '发送问题' }).click();
  await input.fill('下一条草稿');
  await expect(page.getByRole('cell', { name: '130.00', exact: true })).toBeVisible();
  await expect(input).toHaveValue('下一条草稿');
  expect(store.b.runs[0].prompt).toBe('第一条问题');
});

test('final run stays visible when history sync fails, and recovery does not resend', async ({
  page,
}) => {
  const { calls, data } = await setup(page, { running: true });
  await page.getByRole('button', { name: '订单分析 A', exact: true }).click();
  await expect(page.getByRole('button', { name: '停止运行' })).toBeVisible();
  await page.route('**/api/conversations/a', (route) =>
    route.fulfill({ status: 503, json: { error: { message: '历史暂时不可用' } } }),
  );
  data.status = 'completed';
  data.queries = structuredClone(run.queries);
  await expect(page.getByRole('cell', { name: '130.00', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '停止运行' })).toHaveCount(0);
  await expect(page.getByRole('status')).toContainText('已取得本轮最终状态');
  await page.unroute('**/api/conversations/a');
  await page.getByRole('button', { name: '重新同步' }).click();
  await expect(page.getByRole('button', { name: '重新同步' })).toHaveCount(0);
  expect(calls.some((call) => call.startsWith('POST'))).toBe(false);
});

test('renames and deletes the captured target, and ignores new-chat shortcut inside dialog', async ({
  page,
}) => {
  const { calls } = await setup(page, { slowB: true });
  await page.getByRole('button', { name: '订单分析 A', exact: true }).click();
  await page.getByRole('button', { name: '订单分析 B', exact: true }).click();
  await expect(page.getByRole('button', { name: '删除会话', exact: true })).toBeDisabled();
  await expect(page.getByRole('heading', { name: '订单分析 B' })).toBeVisible();
  await page.getByRole('button', { name: '重命名会话' }).click();
  await page.keyboard.press('ControlOrMeta+k');
  await page.getByLabel('会话名称', { exact: true }).fill('改名 B');
  await page.getByRole('button', { name: '保存名称' }).click();
  await expect(page.getByRole('heading', { name: '改名 B' })).toBeVisible();
  await page.getByRole('button', { name: '删除会话', exact: true }).click();
  await page.getByRole('button', { name: '删除对话', exact: true }).click();
  expect(calls).toContain('DELETE /conversations/b');
  expect(calls).not.toContain('DELETE /conversations/a');
});

test('startup failure can reconnect, and Enter respects disabled sending', async ({ page }) => {
  const { calls } = await setup(page, { failStartup: true, configured: false });
  await page.getByRole('button', { name: '重新连接服务' }).click();
  await expect(page.getByRole('button', { name: '重新连接服务' })).toHaveCount(0);
  await page.getByRole('textbox', { name: '输入数据问题' }).fill('不应被发送');
  await page.getByRole('textbox', { name: '输入数据问题' }).press('Enter');
  expect(calls.some((call) => call.startsWith('POST'))).toBe(false);
});

test('running request restores stop control and cancellation reaches final state', async ({
  page,
}) => {
  await setup(page, { running: true });
  await page.getByRole('button', { name: '订单分析 A', exact: true }).click();
  await expect(page.getByRole('button', { name: '停止运行' })).toBeVisible();
  await page.getByRole('button', { name: '停止运行' }).click();
  await expect(page.getByRole('alert')).toContainText('运行已停止');
  await expect(page.getByRole('button', { name: '停止运行' })).toHaveCount(0);
});

test('table structure shows server metadata and drawer closes with Escape', async ({ page }) => {
  await setup(page);
  await page.getByRole('button', { name: '表结构', exact: true }).click();
  await page.getByRole('combobox', { name: '数据表', exact: true }).selectOption('orders');
  await expect(page.locator('.schema-columns')).toContainText('decimal(12,2)');
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toHaveCount(0);
});

test('clearing the selected table removes the loaded schema', async ({ page }) => {
  await setup(page);
  await page.getByRole('button', { name: '表结构', exact: true }).click();
  const select = page.getByRole('combobox', { name: '数据表', exact: true });
  await select.selectOption('orders');
  await expect(page.locator('.schema-columns')).toContainText('decimal(12,2)');
  await select.selectOption('');
  await expect(page.locator('.schema-columns')).toHaveCount(0);
  await expect(page.getByText('读取结构中…', { exact: true })).toHaveCount(0);
});

test('clearing a table while it loads clears the pending state and ignores its response', async ({
  page,
}) => {
  await setup(page);
  let releaseResponse!: () => void;
  const responseReady = new Promise<void>((resolve) => {
    releaseResponse = resolve;
  });
  let responseFinished!: () => void;
  const finished = new Promise<void>((resolve) => {
    responseFinished = resolve;
  });
  let requestStarted!: () => void;
  const started = new Promise<void>((resolve) => {
    requestStarted = resolve;
  });
  await page.route('**/api/schema/tables/orders', async (route) => {
    requestStarted();
    await responseReady;
    await route
      .fulfill({
        json: {
          table: 'orders',
          columns: [{ name: 'total_amount', type: 'decimal(12,2)', nullable: 'YES' }],
          indexes: [],
          foreign_keys: [],
        },
      })
      .catch(() => {});
    responseFinished();
  });
  await page.getByRole('button', { name: '表结构', exact: true }).click();
  const select = page.getByRole('combobox', { name: '数据表', exact: true });
  await select.selectOption('orders');
  await started;
  await expect(page.getByText('读取结构中…', { exact: true })).toBeVisible();
  await select.selectOption('');
  releaseResponse();
  await finished;
  await expect(page.getByText('读取结构中…', { exact: true })).toHaveCount(0);
  await expect(page.locator('.schema-columns')).toHaveCount(0);
});

test('mobile sidebar is inert while closed and traps focus while open; no page overflow', async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await setup(page);
  expect(await page.locator('.sidebar').evaluate((element) => element.hasAttribute('inert'))).toBe(
    true,
  );
  await page.getByRole('button', { name: '打开会话列表' }).click();
  await expect(page.getByRole('dialog', { name: '会话导航' })).toBeVisible();
  expect(
    await page.locator('.main-panel').evaluate((element) => element.hasAttribute('inert')),
  ).toBe(true);
  await page.keyboard.press('Escape');
  expect(await page.locator('.sidebar').evaluate((element) => element.hasAttribute('inert'))).toBe(
    true,
  );
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  );
});
