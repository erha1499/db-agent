// Synthetic HTTP contracts only. Real MySQL writes and browser acceptance are separate.
import { expect, test } from '@playwright/test';
import type { BrowserContext, Page } from '@playwright/test';

const target = {
  id: 'local_inventory',
  kind: 'mysql',
  host: '127.0.0.1',
  port: 13316,
  database: 'db_agent_changes',
  table: 'inventory',
  column: 'quantity',
  max_rows: 1,
  max_quantity: 1000000,
};
function changeItem(id: string, status = 'preview') {
  return {
    plan: {
      id,
      owner: 'alice',
      scope: 'synthetic-scope',
      target: target.id,
      target_fingerprint: 'f'.repeat(64),
      request_id: 'synthetic-request-1',
      item_id: 1,
      before: { quantity: 10, version: 1 },
      after: { quantity: 20, version: 2 },
      created_at: 1789128000,
      expires_at: 4070908800,
      recovery_of: null as string | null,
    },
    digest: 'a'.repeat(64),
    status,
    evidence: null as null | { outcome: string; code: string; receipt_verified: boolean },
  };
}
async function setup(context: BrowserContext) {
  const state = {
    enabled: true,
    canApprove: true,
    unauthorized: false,
    disconnectExecute: false,
    disconnectPreview: false,
    executingGate: null as Promise<void> | null,
  };
  const items: ReturnType<typeof changeItem>[] = [];
  const calls: { path: string; method: string; body: unknown }[] = [];
  const identity = {
    username: 'alice',
    display_name: 'Alice',
    authorization_version: 'v1',
    allowed_tables: ['orders'],
    model_tables: [],
    model_enabled: false,
  };
  await context.route('**/api/**', async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.slice(4);
    const body = request.postDataJSON();
    calls.push({ path, method: request.method(), body });
    const send = (json: unknown, status = 200) => route.fulfill({ json, status });
    if (path === '/auth/session')
      return send({ authenticated: true, identity, session_id: 'test' });
    if (state.unauthorized) return send({ error: { message: '登录已失效。' } }, 401);
    if (path === '/status')
      return send({
        database: 'db_agent',
        database_configured: true,
        model_configured: false,
        changes_enabled: state.enabled,
        identity,
        active_run_id: null,
      });
    if (path === '/conversations') return send({ conversations: [] });
    if (path === '/changes')
      return send({
        targets: state.enabled ? [target] : [],
        changes: state.enabled ? items : [],
        can_approve: state.enabled && state.canApprove,
      });
    if (path === '/changes/preview') {
      const existing = items.find((item) => item.plan.request_id === body.request_id);
      if (existing) return send(existing);
      const current = changeItem('1'.repeat(32));
      current.plan.item_id = body.item_id;
      current.plan.after.quantity = body.quantity;
      current.plan.request_id = body.request_id;
      items.push(current);
      if (state.disconnectPreview) {
        state.disconnectPreview = false;
        return route.abort('connectionreset');
      }
      return send(current, 201);
    }
    const match = path.match(/^\/changes\/([^/]+)(?:\/(approve|execute|reconcile|recover))?$/);
    if (match) {
      const current = items.find((item) => item.plan.id === match[1]);
      if (!current) return send({ error: { message: '变更不存在。' } }, 404);
      if (match[2] === 'approve') current.status = 'approved';
      if (match[2] === 'execute') {
        if (state.executingGate) await state.executingGate;
        if (state.disconnectExecute) return route.abort('connectionreset');
        current.status = 'committed';
        current.evidence = { outcome: 'committed', code: 'COMMITTED', receipt_verified: true };
      }
      if (match[2] === 'reconcile') {
        current.status = 'committed';
        current.evidence = {
          outcome: 'committed',
          code: 'RECEIPT_VERIFIED',
          receipt_verified: true,
        };
      }
      if (match[2] === 'recover') {
        const recovery = changeItem('2'.repeat(32));
        recovery.plan.before = current.plan.after;
        recovery.plan.after = {
          quantity: current.plan.before.quantity,
          version: current.plan.after.version + 1,
        };
        recovery.plan.recovery_of = current.plan.id;
        recovery.plan.request_id = body.request_id;
        items.push(recovery);
        return send(recovery, 201);
      }
      return send(current);
    }
    return send({ error: { message: 'Unexpected synthetic API' } }, 404);
  });
  return { state, calls, items };
}
async function open(page: Page) {
  await page.goto('/');
  await page.getByRole('button', { name: '受控变更', exact: true }).click();
  await expect(page.getByRole('dialog', { name: '受控变更' })).toBeVisible();
}
async function create(page: Page) {
  await page.getByRole('button', { name: '新建变更预览', exact: true }).click();
  await page.getByLabel('商品 ID', { exact: true }).fill('1');
  await page.getByLabel('变更后的库存数量', { exact: true }).fill('20');
  await page.getByRole('button', { name: '生成前后值预览', exact: true }).click();
}
async function approve(page: Page) {
  await page.getByRole('checkbox', { name: '我已核对目标与前后值', exact: true }).check();
  await page.getByRole('button', { name: '明确人工审批', exact: true }).click();
}

test('preview, reviewed approval, execution and recovery each require a separate user action', async ({
  page,
  context,
}) => {
  const { calls } = await setup(context);
  await open(page);
  await create(page);
  const detail = page.getByRole('region', { name: '变更详情' });
  await expect(detail).toContainText('127.0.0.1:13316 / db_agent_changes.inventory.quantity');
  await expect(detail).toContainText('alice');
  await expect(detail).toContainText('a'.repeat(64));
  await expect(detail.getByRole('table', { name: '变更前后值' })).toContainText('version');
  await expect(page.getByRole('button', { name: '明确人工审批', exact: true })).toBeDisabled();
  expect(calls.filter((call) => /\/(approve|execute)$/.test(call.path))).toHaveLength(0);
  await approve(page);
  await expect(page.getByRole('button', { name: '执行已审批变更', exact: true })).toBeEnabled();
  expect(calls.find((call) => call.path.endsWith('/approve'))?.body).toEqual({
    digest: 'a'.repeat(64),
  });
  expect(calls.filter((call) => call.path.endsWith('/execute'))).toHaveLength(0);
  await page.getByRole('button', { name: '执行已审批变更', exact: true }).click();
  await expect(detail.locator('.knowledge-state')).toHaveText('已确认提交');
  expect(calls.find((call) => call.path.endsWith('/execute'))?.body).toEqual({});
  await page.getByRole('button', { name: '生成恢复预览', exact: true }).click();
  await expect(detail).toContainText('恢复原变更 ID');
  await expect(detail.locator('.knowledge-state')).toHaveText('待审批预览');
  await expect(
    page.getByRole('checkbox', { name: '我已核对目标与前后值', exact: true }),
  ).not.toBeChecked();
  expect(calls.filter((call) => call.path.endsWith('/execute'))).toHaveLength(1);
  await approve(page);
  await page.getByRole('button', { name: '执行已审批变更', exact: true }).click();
  await expect(detail.locator('.knowledge-state')).toHaveText('已确认提交');
  expect(calls.filter((call) => call.path.endsWith('/execute'))).toHaveLength(2);
});

test('lost execution response preserves the change and only reconciliation clears uncertainty', async ({
  page,
  context,
}) => {
  const { state, calls, items } = await setup(context);
  items.push(changeItem('3'.repeat(32), 'approved'));
  state.disconnectExecute = true;
  await open(page);
  await page.getByRole('navigation', { name: '变更列表' }).getByRole('button').click();
  await page.getByRole('button', { name: '执行已审批变更', exact: true }).click();
  await expect(page.getByRole('alert').first()).toContainText('未取得可确认的执行结果');
  await expect(page.locator('.knowledge-provenance')).toContainText('3'.repeat(32));
  await expect(page.getByRole('button', { name: '执行已审批变更', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: '刷新此变更状态', exact: true }).click();
  await expect(page.getByRole('button', { name: '执行已审批变更', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: '核对提交结果', exact: true }).click();
  await expect(page.locator('.knowledge-detail .knowledge-state')).toHaveText('已确认提交');
  await expect(page.getByRole('region', { name: '执行或核对证据' })).toContainText(
    'RECEIPT_VERIFIED',
  );
  expect(calls.filter((call) => call.path.endsWith('/execute'))).toHaveLength(1);
  expect(calls.filter((call) => call.path === '/changes/preview')).toHaveLength(0);
});

test('pending execution disables duplicate dispatch and all other actions', async ({
  page,
  context,
}) => {
  const { state, calls, items } = await setup(context);
  items.push(changeItem('4'.repeat(32), 'approved'));
  let release!: () => void;
  state.executingGate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await open(page);
  await page.getByRole('navigation', { name: '变更列表' }).getByRole('button').click();
  await page.getByRole('button', { name: '执行已审批变更', exact: true }).click();
  await expect(page.getByRole('button', { name: '执行已审批变更', exact: true })).toBeDisabled();
  await expect(page.getByRole('button', { name: '新建变更预览', exact: true })).toBeDisabled();
  await expect(page.getByRole('button', { name: '核对提交结果', exact: true })).toBeDisabled();
  expect(calls.filter((call) => call.path.endsWith('/execute'))).toHaveLength(1);
  release();
  await expect(page.locator('.knowledge-detail .knowledge-state')).toHaveText('已确认提交');
});

test('disabled identity or source has no change entry or form', async ({ page, context }) => {
  const { state, calls } = await setup(context);
  state.enabled = false;
  await page.goto('/');
  await expect(page.locator('.app-shell')).toBeVisible();
  await expect(page.getByRole('button', { name: '受控变更', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '新建变更预览', exact: true })).toHaveCount(0);
  await expect(page.locator('.changes-panel')).toHaveCount(0);
  expect(
    calls.filter((call) => call.path.startsWith('/changes') && call.method === 'POST'),
  ).toHaveLength(0);
});

test('absence of approval permission and expired preview prevent approval', async ({
  page,
  context,
}) => {
  const { state, calls, items } = await setup(context);
  state.canApprove = false;
  const item = changeItem('5'.repeat(32));
  items.push(item);
  await open(page);
  await page.getByRole('navigation', { name: '变更列表' }).getByRole('button').click();
  await expect(
    page.getByRole('checkbox', { name: '我已核对目标与前后值', exact: true }),
  ).toBeDisabled();
  await expect(page.locator('.changes-panel')).toContainText('当前身份没有审批权限');
  state.canApprove = true;
  item.plan.expires_at = 1;
  await page.getByRole('button', { name: '刷新变更列表', exact: true }).click();
  await expect(page.locator('.knowledge-detail .knowledge-state')).toContainText('已过期');
  await expect(page.getByRole('button', { name: '明确人工审批', exact: true })).toBeDisabled();
  expect(calls.filter((call) => call.path.endsWith('/approve'))).toHaveLength(0);
});

test('expired approval cannot execute and unknown status only offers reconciliation', async ({
  page,
  context,
}) => {
  const { calls, items } = await setup(context);
  const item = changeItem('6'.repeat(32), 'approved');
  item.plan.expires_at = 1;
  items.push(item);
  await open(page);
  await page.getByRole('navigation', { name: '变更列表' }).getByRole('button').click();
  await expect(page.getByRole('button', { name: '执行已审批变更', exact: true })).toBeDisabled();
  item.status = 'unknown';
  await page.getByRole('button', { name: '刷新此变更状态', exact: true }).click();
  await expect(page.locator('.knowledge-detail .knowledge-state')).toHaveText('提交结果不确定');
  await expect(page.getByRole('button', { name: '执行已审批变更', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '核对提交结果', exact: true })).toBeEnabled();
  expect(calls.filter((call) => call.path.endsWith('/execute'))).toHaveLength(0);
});

test('identity invalidation clears saved change contents', async ({ page, context }) => {
  const { state, items } = await setup(context);
  items.push(changeItem('7'.repeat(32)));
  await open(page);
  await page.getByRole('navigation', { name: '变更列表' }).getByRole('button').click();
  await expect(page.locator('.knowledge-provenance')).toContainText('7'.repeat(32));
  state.unauthorized = true;
  await page.getByRole('button', { name: '刷新变更列表', exact: true }).click();
  await expect(page.getByRole('heading', { name: '登录本机工作区' })).toBeVisible();
  await expect(page.locator('.changes-panel')).toHaveCount(0);
  await expect(page.locator('body')).not.toContainText('7'.repeat(32));
});

test('retrying a failed preview retains its request identifier and never approves it', async ({
  page,
  context,
}) => {
  const { state, calls } = await setup(context);
  state.disconnectPreview = true;
  await open(page);
  await create(page);
  await expect(page.getByRole('alert')).toBeVisible();
  await expect(page.getByLabel('变更后的库存数量', { exact: true })).toHaveValue('20');
  await page.getByRole('button', { name: '生成前后值预览', exact: true }).click();
  await expect(page.locator('.knowledge-detail .knowledge-state')).toHaveText('待审批预览');
  const previews = calls.filter((call) => call.path === '/changes/preview');
  expect(previews).toHaveLength(2);
  expect(previews[0].body).toEqual(previews[1].body);
  expect(calls.filter((call) => /\/(approve|execute)$/.test(call.path))).toHaveLength(0);
});

test('unavailable latest evidence retains historical commit and blocks recovery until reconciliation', async ({
  page,
  context,
}) => {
  const { calls, items } = await setup(context);
  const item = changeItem('8'.repeat(32), 'committed');
  item.evidence = { outcome: 'unknown', code: 'CONNECTION_LOST', receipt_verified: false };
  items.push(item);
  await open(page);
  await page.getByRole('navigation', { name: '变更列表' }).getByRole('button').click();
  await expect(page.locator('.knowledge-detail .knowledge-state')).toHaveText('已确认提交');
  await expect(page.getByRole('alert')).toContainText('历史已确认提交；最新核对证据不可用');
  await expect(page.getByRole('button', { name: '生成恢复预览', exact: true })).toBeDisabled();
  expect(calls.filter((call) => call.path.endsWith('/recover'))).toHaveLength(0);
  await page.getByRole('button', { name: '核对提交结果', exact: true }).click();
  await expect(page.getByRole('button', { name: '生成恢复预览', exact: true })).toBeEnabled();
  await expect(page.getByRole('alert')).toHaveCount(0);
});
