// Real browser acceptance; no route interception. Credentials only enter stdin/login.
import { chromium, expect as baseExpect } from '@playwright/test';
import { join } from 'node:path';
const expect = baseExpect.configure({ timeout: 10000 });
let browser;
const report = { planned: 10, passed: 0, failed_step: null, change_ids: [] };
try {
  let raw = '';
  for await (const chunk of process.stdin) raw += chunk;
  const input = JSON.parse(raw);
  raw = '';
  const url = new URL(input.base_url);
  if (url.hostname !== '127.0.0.1' || url.protocol !== 'http:') throw new Error('target');
  browser = await chromium.launch();
  const context = await browser.newContext({ viewport: { width: 1280, height: 1000 } });
  const page = await context.newPage();
  const actions = [];
  page.on('request', (request) => {
    if (request.method() === 'POST') actions.push(new URL(request.url()).pathname);
  });
  async function check(name, fn) {
    report.failed_step = name;
    await fn();
    report.passed += 1;
  }
  async function login(name) {
    await page.getByLabel('用户名', { exact: true }).fill(name);
    await page.getByLabel('密码', { exact: true }).fill(input.credentials[name]);
    await page.getByRole('button', { name: '登录', exact: true }).click();
    await expect(page.locator('.app-shell')).toBeVisible();
  }
  const panel = page.getByRole('dialog', { name: '受控变更', exact: true });
  async function action(button, suffix) {
    const pending = page.waitForResponse((response) =>
      response.request().method() === 'POST' && new URL(response.url()).pathname.endsWith(suffix));
    pending.catch(() => {}); // Preserve named failure output if clicking fails before a response.
    await panel.getByRole('button', { name: button, exact: true }).click();
    const response = await pending;
    expect(response.status()).toBe(200);
    return response.json();
  }
  await check('alice_real_login', async () => {
    await page.goto(url.origin);
    await login('alice');
    await page.getByRole('button', { name: '受控变更', exact: true }).click();
    await expect(panel).toBeVisible();
  });
  let original;
  await check('preview_real_before_and_target', async () => {
    await panel.getByRole('button', { name: '新建变更预览' }).click();
    await panel.getByLabel('商品 ID', { exact: true }).fill('1');
    await panel.getByLabel('变更后的库存数量').fill(String((input.before[0] + 3) % 1000001));
    original = await action('生成前后值预览', '/preview');
    expect(original.plan.before).toEqual({ quantity: input.before[0], version: input.before[1] });
    expect(original.status).toBe('preview');
    report.change_ids.push(original.plan.id);
    await expect(panel.getByRole('button', { name: '明确人工审批' })).toBeDisabled();
    await page.screenshot({ path: join(input.output_dir, 'preview.png'), fullPage: true });
  });
  await check('explicit_human_approval', async () => {
    await panel.getByRole('checkbox', { name: '我已核对目标与前后值' }).check();
    expect((await action('明确人工审批', '/approve')).status).toBe('approved');
    expect(actions.filter((path) => path.endsWith('/execute'))).toHaveLength(0);
  });
  await check('actual_commit', async () => {
    const result = await action('执行已审批变更', '/execute');
    expect(result.status).toBe('committed');
    expect(result.evidence.receipt_verified).toBe(true);
  });
  await check('reload_keeps_persisted_change_without_reexecute', async () => {
    await page.reload();
    await page.getByRole('button', { name: '受控变更', exact: true }).click();
    await panel.getByRole('button', { name: /变更 · 商品 1/ }).click();
    await expect(panel.getByText(original.plan.id, { exact: true })).toBeVisible();
    expect(actions.filter((path) => path.endsWith('/execute'))).toHaveLength(1);
  });
  await check('actual_receipt_reconciliation', async () => {
    expect((await action('核对提交结果', '/reconcile')).evidence.code).toBe('RECEIPT_CONFIRMED');
  });
  await check('recovery_is_new_preview', async () => {
    const recovery = await action('生成恢复预览', '/recover');
    expect(recovery.status).toBe('preview');
    expect(recovery.plan.recovery_of).toBe(original.plan.id);
    report.change_ids.push(recovery.plan.id);
  });
  await check('separate_recovery_approval_and_execution', async () => {
    await panel.getByRole('checkbox', { name: '我已核对目标与前后值' }).check();
    await action('明确人工审批', '/approve');
    const recovered = await action('执行已审批变更', '/execute');
    expect(recovered.evidence.current).toEqual({ quantity: input.before[0], version: input.before[1] + 2 });
    expect(actions.filter((path) => path.endsWith('/execute'))).toHaveLength(2);
  });
  await check('bob_real_login_and_cross_user_denial', async () => {
    await page.keyboard.press('Escape');
    await page.getByRole('button', { name: '退出登录', exact: true }).click();
    await login('bob');
    await expect(page.getByRole('button', { name: '受控变更', exact: true })).toHaveCount(0);
    await expect(panel).toHaveCount(0);
    const access = await page.evaluate(async (id) => {
      const headers = { 'X-DB-Agent-Client': 'web' };
      const session = await (await fetch('/api/auth/session', { headers })).json();
      const options = { headers: { ...headers, 'X-DB-Agent-Session': session.session_id } };
      const workspace = await (await fetch('/api/status', options)).json();
      const changes = await (await fetch('/api/changes', options)).json();
      return { enabled: workspace.changes_enabled, changes,
        otherUserStatus: (await fetch(`/api/changes/${id}`, options)).status };
    }, original.plan.id);
    expect(access.enabled).toBe(false);
    expect(access.changes).toEqual({ targets: [], changes: [], can_approve: false });
    expect(access.otherUserStatus).toBe(400);
  });
  await check('narrow_layout_and_no_model_dispatch', async () => {
    await page.setViewportSize({ width: 390, height: 844 });
    await expect(page.locator('.app-shell')).toBeVisible();
    await expect(panel).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    expect(actions.some((path) => /\/runs$/.test(path))).toBe(false);
    await page.screenshot({ path: join(input.output_dir, 'bob-mobile.png'), fullPage: true });
  });
  report.failed_step = null;
} catch {
  // Exceptions can contain password fields or HTTP bodies; print only named check outcome.
} finally {
  await browser?.close();
}
console.log(JSON.stringify(report));
process.exitCode = report.passed === report.planned ? 0 : 1;
