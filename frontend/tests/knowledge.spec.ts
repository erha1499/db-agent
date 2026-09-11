// Knowledge UI behavior uses isolated synthetic HTTP identities and stores.
import { expect, test } from '@playwright/test';
import type { BrowserContext, Page } from '@playwright/test';
import type { KnowledgeDraft, KnowledgeItem } from '../src/types';

const draft: KnowledgeDraft = {
  kind: 'metric',
  title: '已支付订单口径',
  definition: '只统计已支付订单，不包含取消订单。',
  source: '合成业务字典',
  source_version: 'demo-v1',
  invalidation_condition: '状态字段定义变更时失效',
  expires_at: '2099-01-01T00:00:00+00:00',
  tables: ['orders'],
  sql: null,
  relationship: null,
};
function item(id: string, title: string, table: string): KnowledgeItem {
  return {
    id,
    payload: { ...draft, title, tables: [table] },
    digest: `digest-${id}`,
    state: 'confirmed',
    created_at: '2026-09-11T00:00:00Z',
    confirmed_at: '2026-09-11T00:01:00Z',
    revoked_at: null,
    reason: null,
    schema_hashes: {},
  };
}
async function setup(context: BrowserContext) {
  const state = { user: 'alice', sessionId: 'first', unauthorized: false };
  const items: Record<string, KnowledgeItem[]> = {
    alice: [
      item('a', 'alice 的订单口径', 'orders'),
      item('beyond-model', '仅本机客户资料', 'customers'),
    ],
    bob: [item('b', 'bob 的客户口径', 'customers')],
  };
  const calls: { path: string; method: string; body: any }[] = [];
  const identity = () => ({
    username: state.user,
    display_name: state.user,
    authorization_version: 'v1',
    allowed_tables: state.user === 'alice' ? ['orders', 'customers'] : ['customers'],
    model_tables: state.user === 'alice' ? ['orders'] : [],
    model_enabled: state.user === 'alice',
  });
  const auth = () => ({
    authenticated: !!state.user,
    identity: state.user ? identity() : undefined,
    session_id: state.sessionId,
    model_boundary: '仅获准资料可发送配置模型；结果行不发送。',
  });
  await context.route('**/api/**', async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.slice(4);
    const body = request.postDataJSON();
    calls.push({ path, method: request.method(), body });
    const send = (json: unknown, status = 200) => route.fulfill({ json, status });
    if (path === '/auth/session') return send(auth());
    if (path === '/auth/logout') {
      state.user = '';
      return send({ authenticated: false });
    }
    if (path === '/auth/login') {
      state.user = body.username;
      state.sessionId = 'next';
      return send(auth());
    }
    if (
      state.unauthorized ||
      !state.user ||
      request.headers()['x-db-agent-session'] !== state.sessionId
    )
      return send({ error: { message: '登录已失效。' } }, 401);
    if (path === '/status')
      return send({
        database: 'db_agent',
        database_configured: true,
        model_configured: true,
        identity: identity(),
        active_run_id: null,
      });
    if (path === '/conversations') return send({ conversations: [] });
    if (path === '/knowledge') {
      if (request.method() === 'POST') {
        const created = {
          ...item('new', body.title, body.tables[0]),
          payload: body,
          state: 'draft' as const,
          confirmed_at: null,
        };
        items[state.user].push(created);
        return send(created, 201);
      }
      return send({ knowledge: items[state.user] });
    }
    const match = path.match(/^\/knowledge\/([^/]+)(?:\/(confirm|revoke))?$/);
    if (match) {
      const current = items[state.user].find((candidate) => candidate.id === match[1]);
      if (!current) return send({ error: { message: '资料不属于当前身份。' } }, 404);
      if (match[2] === 'confirm') {
        current.state = 'confirmed';
        current.confirmed_at = '2026-09-11T01:00:00Z';
      }
      if (match[2] === 'revoke') {
        current.state = 'revoked';
        current.revoked_at = '2026-09-11T02:00:00Z';
        current.reason = body.reason;
      }
      return send(current);
    }
    return send({ error: { message: 'Unexpected synthetic API' } }, 404);
  });
  return { state, calls, items };
}
async function open(page: Page) {
  await page.getByRole('button', { name: '业务知识', exact: true }).click();
  await expect(page.getByRole('dialog', { name: '业务知识' })).toBeVisible();
}

test('draft requires full review and explicit confirmation; reference never sends automatically; revocation removes use', async ({
  page,
  context,
}) => {
  const { calls } = await setup(context);
  await page.goto('/');
  await open(page);
  await page.getByRole('button', { name: '新建资料', exact: true }).click();
  await page.getByLabel('资料草稿 JSON').fill(JSON.stringify(draft));
  await page.getByRole('button', { name: '保存待确认资料' }).click();
  await expect(page.locator('.knowledge-payload')).toContainText(draft.definition);
  await expect(page.locator('.knowledge-payload')).toContainText(draft.source_version);
  await expect(page.locator('.knowledge-provenance')).toContainText('digest-new');
  await expect(page.getByRole('button', { name: '确认这份资料' })).toBeDisabled();
  await expect(page.getByRole('button', { name: '引用到本轮智能查询' })).toHaveCount(0);
  await page.getByRole('checkbox').check();
  await page.getByRole('button', { name: '确认这份资料' }).click();
  await expect(page.locator('.knowledge-state')).toHaveText('已确认');
  expect(calls.find((call) => call.path.endsWith('/confirm'))?.body).toEqual({
    digest: 'digest-new',
  });
  await page.getByRole('button', { name: '引用到本轮智能查询' }).click();
  await expect(page.getByRole('textbox', { name: '输入数据问题' })).toHaveValue(
    '[[knowledge:new]]',
  );
  expect(calls.some((call) => call.path.endsWith('/runs'))).toBe(false);
  await open(page);
  await page.getByRole('button', { name: '已支付订单口径 业务口径 · 已确认' }).click();
  await page.getByLabel('撤销原因').fill('来源口径已更改');
  await page.getByRole('button', { name: '撤销资料', exact: true }).click();
  await expect(page.locator('.knowledge-state')).toHaveText('已撤销');
  await expect(page.locator('.knowledge-provenance')).toContainText('来源口径已更改');
  await expect(page.getByRole('button', { name: '引用到本轮智能查询' })).toHaveCount(0);
});

test('malformed JSON stays in the editor and cannot be submitted', async ({ page, context }) => {
  const { calls } = await setup(context);
  await page.goto('/');
  await open(page);
  await page.getByRole('button', { name: '新建资料', exact: true }).click();
  await page.getByLabel('资料草稿 JSON').fill('{broken');
  await page.getByRole('button', { name: '保存待确认资料' }).click();
  await expect(page.getByRole('alert')).toContainText('不是有效 JSON');
  await expect(page.getByLabel('资料草稿 JSON')).toHaveValue('{broken');
  expect(calls.filter((call) => call.path === '/knowledge' && call.method === 'POST')).toHaveLength(
    0,
  );
});

test('knowledge lists and details clear across identities; model-disabled users can manage but cannot quote', async ({
  page,
  context,
}) => {
  await setup(context);
  await page.goto('/');
  await open(page);
  await page.getByRole('button', { name: 'alice 的订单口径 业务口径 · 已确认' }).click();
  await expect(page.locator('.knowledge-payload')).toContainText('alice');
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: '退出登录' }).click();
  await page.getByLabel('用户名', { exact: true }).fill('bob');
  await page.getByLabel('密码', { exact: true }).fill('synthetic-password');
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await open(page);
  await expect(page.locator('.knowledge-list')).not.toContainText('alice');
  await page.getByRole('button', { name: 'bob 的客户口径 业务口径 · 已确认' }).click();
  await expect(page.getByRole('button', { name: '引用到本轮智能查询' })).toBeDisabled();
  await expect(page.getByRole('button', { name: '新建资料', exact: true })).toBeEnabled();
  await expect(page.locator('.knowledge-detail')).toContainText('当前身份禁用模型');
});

test('401 clears loaded knowledge content and list', async ({ page, context }) => {
  const { state } = await setup(context);
  await page.goto('/');
  await open(page);
  await page.getByRole('button', { name: 'alice 的订单口径 业务口径 · 已确认' }).click();
  await expect(page.locator('.knowledge-payload')).toContainText('alice');
  state.unauthorized = true;
  await page.getByRole('button', { name: '刷新列表', exact: true }).click();
  await expect(page.getByRole('heading', { name: '登录本机工作区' })).toBeVisible();
  await expect(page.locator('.knowledge-panel')).toHaveCount(0);
  await expect(page.locator('body')).not.toContainText('alice 的订单口径');
});

test('allowed database knowledge beyond model tables cannot be inserted', async ({
  page,
  context,
}) => {
  await setup(context);
  await page.goto('/');
  await open(page);
  await page.getByRole('button', { name: '仅本机客户资料 业务口径 · 已确认' }).click();
  await expect(page.getByRole('button', { name: '引用到本轮智能查询' })).toBeDisabled();
  await expect(page.locator('.knowledge-detail')).toContainText('模型范围之外的表');
});
