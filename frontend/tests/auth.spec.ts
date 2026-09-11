// These browser tests use synthetic HTTP identities, not real authentication/MySQL evidence.
import { expect, test } from '@playwright/test';
import type { BrowserContext, Page } from '@playwright/test';

const stamp = '2026-09-11T12:00:00Z';
const identities = {
  alice: {
    username: 'alice',
    display_name: '订单分析员',
    authorization_version: 'alice-v1',
    allowed_tables: ['orders', 'customers'],
    model_tables: ['orders'],
    model_enabled: true,
  },
  bob: {
    username: 'bob',
    display_name: '客户查看员',
    authorization_version: 'bob-v1',
    allowed_tables: ['customers'],
    model_tables: [],
    model_enabled: false,
  },
};
async function setup(context: BrowserContext, user: 'alice' | 'bob' | null = null) {
  const state = { user, sessionId: 'first', version: 'v1', unauthorized: false, forbidden: false };
  const calls: { path: string; method: string; body: any; sessionHeader: string | undefined }[] =
    [];
  const auth = () => ({
    authenticated: state.user !== null,
    session_id: state.user ? state.sessionId : null,
    identity: state.user
      ? { ...identities[state.user], authorization_version: state.version }
      : undefined,
    model_boundary: '用户原文、获准表结构、SQL 与计划摘要发送模型；查询结果行不发送。',
  });
  const conversation = () => ({
    id: state.user,
    title: `${state.user} 的私有历史`,
    created_at: stamp,
    updated_at: stamp,
    context_turns: 1,
    runs: [],
  });
  await context.route('**/api/**', async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.slice(4);
    const body = request.postDataJSON();
    calls.push({
      path,
      method: request.method(),
      body,
      sessionHeader: request.headers()['x-db-agent-session'],
    });
    const send = (json: unknown, status = 200) => route.fulfill({ json, status });
    if (path === '/auth/session') return send(auth());
    if (path === '/auth/login') {
      if (!(body.username in identities) || body.password !== 'synthetic-password')
        return send({ error: { code: 'INVALID_LOGIN', message: '用户名或密码错误。' } }, 401);
      state.user = body.username;
      state.sessionId = crypto.randomUUID();
      return send(auth());
    }
    if (path === '/auth/logout') {
      state.user = null;
      return send({ authenticated: false });
    }
    if (
      !state.user ||
      state.unauthorized ||
      request.headers()['x-db-agent-session'] !== state.sessionId
    )
      return send({ error: { message: '登录已过期。' } }, 401);
    if (path === '/status')
      return send({
        database: 'db_agent',
        database_configured: true,
        model_configured: true,
        database_error: null,
        model_error: null,
        read_only: true,
        active_run_id: null,
        identity: identities[state.user],
        model_boundary: auth().model_boundary,
      });
    if (path === '/conversations')
      return request.method() === 'POST'
        ? send(conversation(), 201)
        : send({ conversations: [conversation()] });
    if (path === `/conversations/${state.user}`) return send(conversation());
    if (path.endsWith('/runs'))
      return send(
        {
          id: 'run',
          conversation_id: state.user,
          request_id: body.request_id,
          prompt: body.prompt,
          mode: body.mode,
          created_at: stamp,
          finished_at: stamp,
          status: 'completed',
          answer: '直接查询已完成',
          error: null,
          queries: [],
          analyses: [],
          events: [],
        },
        202,
      );
    if (path === '/schema/tables') {
      if (state.forbidden) return send({ error: { message: '当前身份没有此项权限。' } }, 403);
      return send({ tables: identities[state.user].allowed_tables.map((name) => ({ name })) });
    }
    if (path.startsWith('/schema/tables/'))
      return send({
        table: path.split('/').at(-1),
        columns: [{ name: `${state.user}_private_column`, type: 'bigint', nullable: 'NO' }],
        indexes: [],
        foreign_keys: [],
      });
    return send({ error: { message: 'Unexpected synthetic request' } }, 404);
  });
  return { state, calls };
}
async function login(page: Page, name: string) {
  await page.getByLabel('用户名', { exact: true }).fill(name);
  await page.getByLabel('密码', { exact: true }).fill('synthetic-password');
  await page.getByRole('button', { name: '登录', exact: true }).click();
}

test('requires login, rejects incorrect password, and shows server identity/model boundaries', async ({
  page,
  context,
}) => {
  const { calls } = await setup(context);
  await page.goto('/');
  await expect(page.getByRole('heading', { name: '登录本机工作区' })).toBeVisible();
  expect(calls.every((call) => call.path === '/auth/session')).toBe(true);
  await page.getByLabel('用户名', { exact: true }).fill('alice');
  await page.getByLabel('密码', { exact: true }).fill('incorrect');
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('用户名或密码错误');
  await expect(page.getByLabel('密码', { exact: true })).toHaveValue('');
  await login(page, 'alice');
  await page.getByRole('button', { name: '查看当前身份与权限' }).click();
  const dialog = page.getByRole('dialog', { name: '当前身份与权限' });
  await expect(dialog).toContainText('alice');
  await expect(dialog.locator('.scope-tables').nth(0)).toHaveText('orders、customers');
  await expect(dialog.locator('.scope-tables').nth(1)).toHaveText('orders');
  await expect(dialog).toContainText('查询结果行不发送');
  const storage = await page.evaluate(() => ({
    local: { ...localStorage },
    session: { ...sessionStorage },
  }));
  expect(JSON.stringify(storage)).not.toMatch(/synthetic-password|alice|token/);
});
test('model-disabled identity can submit explicit SQL without inheriting chat history', async ({
  page,
  context,
}) => {
  const { calls } = await setup(context, 'bob');
  await page.goto('/');
  await page.getByRole('button', { name: 'bob 的私有历史', exact: true }).click();
  await expect(page.getByRole('button', { name: '智能查询', exact: true })).toBeDisabled();
  await expect(page.getByRole('button', { name: '直接查询 SQL', exact: true })).toHaveAttribute(
    'aria-pressed',
    'true',
  );
  await expect(page.locator('.composer-hint')).toContainText('不调用模型，不沿用历史条件');
  await page
    .getByRole('textbox', { name: '输入要执行的 SQL' })
    .fill('SELECT id FROM customers LIMIT 1');
  await page.getByRole('button', { name: '发送问题' }).click();
  await expect(page.locator('.mode-badge')).toHaveText('直接查询 SQL');
  const submitted = calls.find((call) => call.path.endsWith('/runs'));
  expect(submitted?.body).toMatchObject({
    mode: 'query',
    prompt: 'SELECT id FROM customers LIMIT 1',
  });
  expect(Object.keys(submitted!.body).sort()).toEqual(['mode', 'prompt', 'request_id']);
});
test('401 clears history, draft and schema; another identity starts clean', async ({
  page,
  context,
}) => {
  const { state } = await setup(context, 'alice');
  await page.goto('/');
  await page.getByRole('button', { name: 'alice 的私有历史', exact: true }).click();
  await page.getByRole('textbox', { name: '输入数据问题' }).fill('alice 私有草稿');
  await page.getByRole('button', { name: '表结构', exact: true }).click();
  await page.getByLabel('数据表', { exact: true }).selectOption('orders');
  await expect(page.locator('.schema-columns')).toContainText('alice_private_column');
  state.unauthorized = true;
  await page.getByLabel('数据表', { exact: true }).selectOption('customers');
  await expect(page.getByRole('heading', { name: '登录本机工作区' })).toBeVisible();
  await expect(page.locator('.app-shell, .schema-columns')).toHaveCount(0);
  await expect(page.locator('body')).not.toContainText('alice 私有草稿');
  state.unauthorized = false;
  await login(page, 'bob');
  await expect(page.getByRole('button', { name: 'bob 的私有历史', exact: true })).toBeVisible();
  await expect(page.locator('body')).not.toContainText('alice');
  await expect(page.getByRole('textbox', { name: '输入要执行的 SQL' })).toHaveValue('');
});
test('resource denial is explicit and does not pretend the user is logged out', async ({
  page,
  context,
}) => {
  const { state } = await setup(context, 'alice');
  state.forbidden = true;
  await page.goto('/');
  await page.getByRole('button', { name: '表结构', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('当前身份没有此项权限');
  await expect(page.locator('.app-shell')).toBeVisible();
});
test('another tab logout immediately clears an open workspace', async ({ page, context }) => {
  await setup(context, 'alice');
  await page.goto('/');
  await page.getByRole('button', { name: 'alice 的私有历史', exact: true }).click();
  await page.getByRole('textbox', { name: '输入数据问题' }).fill('不能跨用户保留');
  const other = await context.newPage();
  await other.goto('/');
  await other.getByRole('button', { name: '退出登录' }).click();
  await expect(page.getByRole('heading', { name: '登录本机工作区' })).toBeVisible();
  await login(other, 'bob');
  await expect(other.getByRole('button', { name: 'bob 的私有历史', exact: true })).toBeVisible();
  await expect(page.locator('.app-shell')).toHaveCount(0);
  await expect(page.locator('body')).not.toContainText('不能跨用户保留');
});
test('same username with a new session clears data on resumed focus', async ({ page, context }) => {
  const { state } = await setup(context, 'alice');
  await page.goto('/');
  await expect(page.getByRole('button', { name: 'alice 的私有历史', exact: true })).toBeVisible();
  state.sessionId = 'replacement-session';
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await expect(page.getByRole('heading', { name: '登录本机工作区' })).toBeVisible();
  await expect(page.getByRole('alert')).toContainText('身份权限已变更');
});

test('permission version change with the same session clears data on focus', async ({
  page,
  context,
}) => {
  const { state } = await setup(context, 'alice');
  await page.goto('/');
  await expect(page.getByRole('button', { name: 'alice 的私有历史', exact: true })).toBeVisible();
  state.version = 'v2';
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await expect(page.getByRole('heading', { name: '登录本机工作区' })).toBeVisible();
});

test('a delayed previous-identity history response cannot reappear after login', async ({
  page,
  context,
}) => {
  await setup(context, 'alice');
  let release!: () => void;
  const released = new Promise<void>((resolve) => {
    release = resolve;
  });
  let started!: () => void;
  const requestStarted = new Promise<void>((resolve) => {
    started = resolve;
  });
  await page.route('**/api/conversations/alice', async (route) => {
    started();
    await released;
    await route
      .fulfill({ json: { id: 'alice', title: '迟到的 alice 私有数据', runs: [] } })
      .catch(() => {});
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'alice 的私有历史', exact: true }).click();
  await requestStarted;
  await page.getByRole('button', { name: '退出登录' }).click();
  await login(page, 'bob');
  await expect(page.getByRole('button', { name: 'bob 的私有历史', exact: true })).toBeVisible();
  release();
  await page.getByRole('button', { name: 'bob 的私有历史', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'bob 的私有历史' })).toBeVisible();
  await expect(page.locator('body')).not.toContainText('alice');
});

test('protected requests bind the verified page session and reject a cookie switch before tab notification', async ({
  page,
  context,
}) => {
  const { state, calls } = await setup(context, 'alice');
  await page.goto('/');
  await expect(page.getByRole('button', { name: 'alice 的私有历史', exact: true })).toBeVisible();
  expect(
    calls.filter((call) => call.path.startsWith('/auth/')).every((call) => !call.sessionHeader),
  ).toBe(true);
  expect(
    calls
      .filter((call) => !call.path.startsWith('/auth/'))
      .every((call) => call.sessionHeader === 'first'),
  ).toBe(true);
  state.user = 'bob';
  state.sessionId = 'bob-session';
  await page.getByRole('textbox', { name: '输入数据问题' }).fill('不得写进 bob 历史的 alice 草稿');
  await page.getByRole('button', { name: '发送问题' }).click();
  await expect(page.getByRole('heading', { name: '登录本机工作区' })).toBeVisible();
  expect(calls.filter((call) => call.path.endsWith('/runs'))).toHaveLength(0);
});
