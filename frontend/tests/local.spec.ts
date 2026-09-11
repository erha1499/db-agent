// Synthetic HTTP contracts; these tests do not establish live model/database behavior.
import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

const orderSql = 'SELECT * FROM orders ORDER BY created_at DESC LIMIT 10';

async function localWorkspace(page: Page, modelEnabled = true, tables = ['orders']) {
  const state = { sessionId: 'local-first', unauthorized: false, failConnection: false };
  const calls: { path: string; body: any; sessionHeader: string | undefined }[] = [];
  const identity = {
    username: 'local',
    display_name: '本机工作区',
    authorization_version: 'local-v1',
    allowed_tables: tables,
    model_tables: modelEnabled ? tables : [],
    model_enabled: modelEnabled,
  };
  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.slice(4);
    const body = request.postDataJSON();
    calls.push({ path, body, sessionHeader: request.headers()['x-db-agent-session'] });
    const send = (json: unknown, status = 200) => route.fulfill({ json, status });
    if (path === '/auth/session') {
      if (state.failConnection) return send({ error: { message: '服务暂时不可用' } }, 503);
      return send({
        access_mode: 'local',
        authenticated: true,
        session_id: state.sessionId,
        identity,
        model_boundary: '原文与表结构发送模型；查询结果行不发送。',
      });
    }
    if (state.unauthorized || request.headers()['x-db-agent-session'] !== state.sessionId)
      return send({ error: { message: '工作区连接已失效。' } }, 401);
    if (path === '/status')
      return send({
        database: 'db_agent',
        database_configured: true,
        model_configured: modelEnabled,
        database_error: null,
        model_error: null,
        read_only: true,
        active_run_id: null,
        changes_enabled: false,
        identity,
      });
    if (path === '/conversations')
      return request.method() === 'POST'
        ? send({ id: 'new', title: '新建对话', runs: [] }, 201)
        : send({ conversations: [] });
    if (path === '/conversations/new/runs')
      return send(
        {
          id: 'run',
          conversation_id: 'new',
          request_id: body.request_id,
          prompt: body.prompt,
          mode: body.mode,
          status: 'completed',
          answer: '合成测试结果',
          queries: [],
          analyses: [],
          events: [],
        },
        202,
      );
    if (path === '/schema/tables') return send({ tables: tables.map((name) => ({ name })) });
    return send({ error: { message: 'Unexpected synthetic request' } }, 404);
  });
  return { state, calls };
}

test('default local workspace opens without login and offers a ready-to-edit order question', async ({
  page,
}) => {
  const { calls } = await localWorkspace(page);
  await page.goto('/');
  await expect(page.locator('.app-shell')).toBeVisible();
  await expect(page.locator('.sidebar-footer')).toContainText('本机工作区');
  await expect(page.getByLabel('用户名', { exact: true })).toHaveCount(0);
  await expect(page.getByLabel('密码', { exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '退出登录' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '查看当前身份与权限' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '受控变更', exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: /查询业务数据/ }).click();
  await expect(page.getByRole('textbox', { name: '输入数据问题' })).toHaveValue(
    '帮我查询最近的10个订单',
  );
  expect(calls.every((call) => call.path !== '/auth/login')).toBe(true);
  expect(
    calls
      .filter((call) => !call.path.startsWith('/auth/'))
      .every((call) => call.sessionHeader === 'local-first'),
  ).toBe(true);
});

test('local workspace without a model starts in direct SQL mode and supplies an order example', async ({
  page,
}) => {
  const { calls } = await localWorkspace(page, false);
  await page.goto('/');
  await page.getByRole('button', { name: /查询业务数据/ }).click();
  await expect(page.getByRole('button', { name: '智能查询', exact: true })).toBeDisabled();
  await expect(page.getByRole('textbox', { name: '输入要执行的 SQL' })).toHaveValue(orderSql);
  await page.getByRole('button', { name: '发送问题' }).click();
  await expect(page.locator('.mode-badge')).toHaveText('直接查询 SQL');
  expect(calls.find((call) => call.path.endsWith('/runs'))?.body).toMatchObject({
    mode: 'query',
    prompt: orderSql,
  });
});

test('examples do not suggest orders outside the configured table scope', async ({ page }) => {
  await localWorkspace(page, true, ['customers']);
  await page.goto('/');
  await page.getByRole('button', { name: /查询业务数据/ }).click();
  await expect(page.getByRole('textbox', { name: '输入数据问题' })).toHaveValue(
    '帮我查看 customers 的前10条记录',
  );
});

test('an expired local session clears its draft and reconnects without a login form', async ({
  page,
}) => {
  const { state, calls } = await localWorkspace(page);
  await page.goto('/');
  await page.getByRole('textbox', { name: '输入数据问题' }).fill('旧工作区的草稿');
  state.unauthorized = true;
  await page.getByRole('button', { name: '表结构', exact: true }).click();
  await expect(page.locator('.app-shell')).toHaveCount(0);
  await expect(page.locator('body')).not.toContainText('旧工作区的草稿');
  await expect(page.getByLabel('用户名', { exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '重新连接工作区' })).toBeVisible();
  state.unauthorized = false;
  state.sessionId = 'local-new';
  await page.getByRole('button', { name: '重新连接工作区' }).click();
  await expect(page.locator('.app-shell')).toBeVisible();
  await expect(page.getByRole('textbox', { name: '输入数据问题' })).toHaveValue('');
  expect(calls.some((call) => call.path === '/status' && call.sessionHeader === 'local-new')).toBe(
    true,
  );
});

test('an unavailable startup connection offers retry without requiring a password', async ({
  page,
}) => {
  const { state } = await localWorkspace(page);
  state.failConnection = true;
  await page.goto('/');
  await expect(page.getByRole('alert')).toContainText('服务暂时不可用');
  await expect(page.getByLabel('密码', { exact: true })).toHaveCount(0);
  state.failConnection = false;
  await page.getByRole('button', { name: '重新连接工作区' }).click();
  await expect(page.locator('.app-shell')).toBeVisible();
});

test('a replacement local session clears the old workspace before accepting its new scope', async ({
  page,
}) => {
  const { state, calls } = await localWorkspace(page);
  await page.goto('/');
  await page.getByRole('textbox', { name: '输入数据问题' }).fill('旧配置的草稿');
  state.sessionId = 'replacement-local-session';
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await expect(page.locator('.app-shell')).toHaveCount(0);
  await expect(page.getByRole('alert')).toContainText('工作区连接已失效或配置已变更');
  await expect(page.locator('body')).not.toContainText('旧配置的草稿');
  await page.getByRole('button', { name: '重新连接工作区' }).click();
  await expect(page.locator('.app-shell')).toBeVisible();
  await expect(page.getByRole('textbox', { name: '输入数据问题' })).toHaveValue('');
  expect(
    calls.some((call) => call.path === '/status' && call.sessionHeader === state.sessionId),
  ).toBe(true);
});
