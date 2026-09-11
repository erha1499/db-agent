// Explicit live browser acceptance. Credentials arrive only through stdin; no HTTP route doubles.
import { chromium, expect as baseExpect } from '@playwright/test';
import { readFile, stat } from 'node:fs/promises';
import { isAbsolute, join } from 'node:path';
import { randomUUID } from 'node:crypto';

const expect = baseExpect.configure({ timeout: 15000 });
const checks = [];
const names = [
  'alice_login_identity',
  'alice_direct_query_fixture',
  'alice_saved_result_download',
  'alice_knowledge_draft_review',
  'alice_knowledge_confirm_reference_only',
  'bob_login_schema_scope',
  'bob_direct_query_fixture',
  'bob_knowledge_list_isolation',
  'bob_cannot_retrieve_alice_objects',
  'alice_persisted_knowledge_revoke',
  'narrow_screen_layout',
  'only_direct_query_requests',
];
let browser;
let startupError = false;
try {
  let raw = '';
  for await (const chunk of process.stdin) {
    raw += chunk;
    if (Buffer.byteLength(raw) > 16384) throw new Error('invalid_input');
  }
  const input = JSON.parse(raw);
  raw = '';
  const target = new URL(input.base_url);
  const directory = input.output_dir;
  if (
    target.protocol !== 'http:' ||
    !['127.0.0.1', 'localhost'].includes(target.hostname) ||
    target.username ||
    target.password ||
    target.search ||
    target.hash ||
    target.pathname !== '/' ||
    !isAbsolute(directory) ||
    !['alice', 'bob'].every(
      (name) =>
        typeof input.credentials?.[name] === 'string' && input.credentials[name].length >= 12,
    )
  )
    throw new Error('invalid_input');
  const output = await stat(directory);
  if (!output.isDirectory() || (output.mode & 0o077) !== 0)
    throw new Error('private_output_required');
  browser = await chromium.launch();
  const context = await browser.newContext({
    viewport: { width: 1280, height: 900 },
    acceptDownloads: true,
  });
  const page = await context.newPage();
  page.setDefaultTimeout(15000);
  const runModes = [];
  page.on('request', (request) => {
    if (
      request.method() === 'POST' &&
      /\/api\/conversations\/[^/]+\/runs$/.test(new URL(request.url()).pathname)
    )
      runModes.push(request.postDataJSON()?.mode);
  });
  async function check(name, action) {
    try {
      if ((await action()) === false)
        checks.push({ name, status: 'skipped', error: 'dependency_unavailable' });
      else checks.push({ name, status: 'passed' });
    } catch {
      // Playwright exceptions can echo field values or response bodies. Do not emit them.
      checks.push({ name, status: 'failed', error: 'browser_check_failed' });
    }
  }
  async function closeDialog() {
    if (await page.locator('dialog[open]').count()) await page.keyboard.press('Escape');
  }
  async function login(name) {
    await page.getByLabel('用户名', { exact: true }).fill(name);
    await page.getByLabel('密码', { exact: true }).fill(input.credentials[name]);
    await page.getByRole('button', { name: '登录', exact: true }).click();
    await expect(page.locator('.app-shell')).toBeVisible();
  }
  async function query(sql, expectedCell) {
    await closeDialog();
    await page
      .getByRole('button', { name: /新建对话/ })
      .first()
      .click();
    await page.getByRole('button', { name: '直接查询 SQL', exact: true }).click();
    await page.getByRole('textbox', { name: '输入要执行的 SQL' }).fill(sql);
    const response = page.waitForResponse(
      (response) =>
        response.request().method() === 'POST' &&
        /\/api\/conversations\/[^/]+\/runs$/.test(new URL(response.url()).pathname),
    );
    await page.getByRole('button', { name: '发送问题' }).click();
    const submitted = await (await response).json();
    await expect(page.getByRole('cell', { name: expectedCell, exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: '停止运行' })).toHaveCount(0);
    return submitted;
  }
  async function openKnowledge() {
    await closeDialog();
    await page.getByRole('button', { name: '业务知识', exact: true }).click();
    await expect(page.getByRole('dialog', { name: '业务知识' })).toBeVisible();
  }
  let aliceRun;
  let aliceResult;
  let knowledge;
  let aliceReady = false;
  let bobReady = false;
  const knowledgeTitle = `本机浏览器验收 ${randomUUID().slice(0, 8)}`;

  await check(names[0], async () => {
    await page.goto(target.origin);
    await login('alice');
    aliceReady = true;
    await page.getByRole('button', { name: '查看当前身份与权限' }).click();
    const dialog = page.getByRole('dialog', { name: '当前身份与权限' });
    await expect(dialog).toContainText('alice');
    await expect(dialog.locator('.scope-tables').nth(0)).toContainText('orders');
    await expect(dialog.locator('.scope-tables').nth(0)).toContainText('customers');
    await expect(dialog.locator('.scope-tables').nth(1)).toHaveText('orders');
    await page.screenshot({ path: join(directory, 'browser-alice-identity.png') });
    await closeDialog();
  });
  await check(names[1], async () => {
    if (!aliceReady) return false;
    aliceRun = await query('SELECT id, total_amount FROM orders ORDER BY id LIMIT 2', '1001');
    await expect(page.getByRole('cell', { name: '100.00', exact: true })).toBeVisible();
    await expect(page.getByRole('cell', { name: '30.00', exact: true })).toBeVisible();
    await page.screenshot({ path: join(directory, 'browser-alice-query.png') });
  });
  await check(names[2], async () => {
    if (!aliceRun) return false;
    await page.getByRole('tab', { name: '分析与交付', exact: true }).click();
    const downloadReady = page.waitForEvent('download');
    await page.getByRole('button', { name: '下载 JSON 快照' }).click();
    const download = await downloadReady;
    const path = join(directory, 'browser-alice-result.json');
    await download.saveAs(path);
    aliceResult = JSON.parse(await readFile(path, 'utf8'));
    expect(aliceResult.report.result.rows).toEqual([
      [1001, '100.00'],
      [1002, '30.00'],
    ]);
    expect(aliceResult.report.result.truncated).toBe(false);
  });
  await check(names[3], async () => {
    if (!aliceReady) return false;
    await openKnowledge();
    await page.getByRole('button', { name: '新建资料', exact: true }).click();
    await page.getByLabel('资料草稿 JSON').fill(
      JSON.stringify({
        kind: 'metric',
        title: knowledgeTitle,
        definition: "orders 中 status='paid' 表示已支付订单；本资料仅用于本机合成验收。",
        source: 'tests/fixtures/mysql_business.sql',
        source_version: 'local-synthetic-fixture',
        invalidation_condition: '合成表结构、状态定义或来源文件变化时失效。',
        expires_at: new Date(Date.now() + 86400_000).toISOString(),
        tables: ['orders'],
        sql: null,
        relationship: null,
      }),
    );
    const created = page.waitForResponse(
      (response) =>
        response.request().method() === 'POST' &&
        new URL(response.url()).pathname === '/api/knowledge',
    );
    await page.getByRole('button', { name: '保存待确认资料' }).click();
    knowledge = await (await created).json();
    expect(knowledge.state).toBe('draft');
    await expect(page.locator('.knowledge-payload')).toContainText('local-synthetic-fixture');
    await expect(page.locator('.knowledge-provenance')).toContainText(knowledge.digest);
    await expect(page.getByRole('button', { name: '确认这份资料' })).toBeDisabled();
  });
  await check(names[4], async () => {
    if (!knowledge?.id) return false;
    await page.getByRole('checkbox').check();
    await page.getByRole('button', { name: '确认这份资料' }).click();
    await expect(page.locator('.knowledge-state')).toHaveText('已确认');
    await page.screenshot({ path: join(directory, 'browser-alice-knowledge.png') });
    const before = runModes.length;
    await page.getByRole('button', { name: '引用到本轮智能查询' }).click();
    expect(await page.getByRole('textbox', { name: '输入数据问题' }).inputValue()).toContain(
      `[[knowledge:${knowledge.id}]]`,
    );
    expect(runModes.length).toBe(before);
  });
  await check(names[5], async () => {
    if (!aliceReady) return false;
    await closeDialog();
    await page.getByRole('button', { name: '退出登录' }).click();
    await expect(page.locator('.app-shell')).toHaveCount(0);
    await login('bob');
    bobReady = true;
    await expect(page.getByRole('textbox', { name: '输入数据问题' })).toHaveValue('');
    await page.getByRole('button', { name: '表结构', exact: true }).click();
    const select = page.getByLabel('数据表', { exact: true });
    await expect(select.locator('option')).toHaveCount(2);
    expect(await select.locator('option').allTextContents()).toEqual(['选择一张表', 'customers']);
    await closeDialog();
  });
  await check(names[6], async () => {
    if (!bobReady) return false;
    await query('SELECT id, customer_code FROM customers ORDER BY id LIMIT 2', 'SYN-C001');
    await expect(page.getByRole('cell', { name: 'SYN-C002', exact: true })).toBeVisible();
    await page.screenshot({ path: join(directory, 'browser-bob-query.png') });
  });
  await check(names[7], async () => {
    if (!bobReady || !knowledge?.id) return false;
    await openKnowledge();
    await expect(page.getByRole('button', { name: '刷新列表', exact: true })).toBeEnabled();
    await expect(page.locator('.knowledge-list')).not.toContainText(knowledgeTitle);
    await closeDialog();
  });
  await check(names[8], async () => {
    if (!bobReady || !aliceRun || !aliceResult || !knowledge?.id) return false;
    const resultPath = `/api/conversations/${aliceRun.conversation_id}/runs/${aliceRun.id}/results/${aliceResult.result_id}`;
    const statuses = await page.evaluate(
      async ({ conversationId, resultPath, knowledgeId }) => {
        const publicHeaders = { 'X-DB-Agent-Client': 'web', 'Content-Type': 'application/json' };
        const session = await (await fetch('/api/auth/session', { headers: publicHeaders })).json();
        const headers = { ...publicHeaders, 'X-DB-Agent-Session': session.session_id };
        const paths = [
          `/api/conversations/${conversationId}`,
          resultPath,
          `/api/knowledge/${knowledgeId}`,
        ];
        const codes = [];
        for (const path of paths) {
          const response = await fetch(path, { headers });
          const body = await response.json();
          codes.push({
            status: response.status,
            hasError: !!body.error,
            hasPayload: !!body.payload,
          });
        }
        codes.push(
          (
            await fetch(`${resultPath}/export`, {
              method: 'POST',
              headers,
              body: JSON.stringify({ format: 'json', analysis: null }),
            })
          ).status,
        );
        return codes;
      },
      { conversationId: aliceRun.conversation_id, resultPath, knowledgeId: knowledge.id },
    );
    expect(statuses.slice(0, 2)).toEqual([
      { status: 404, hasError: true, hasPayload: false },
      { status: 404, hasError: true, hasPayload: false },
    ]);
    expect([400, 403, 404]).toContain(statuses[2].status);
    expect(statuses[2].hasError).toBe(true);
    expect(statuses[2].hasPayload).toBe(false);
    expect(statuses[3]).toBe(404);
  });
  await check(names[9], async () => {
    if (!bobReady || !knowledge?.id) return false;
    await closeDialog();
    await page.getByRole('button', { name: '退出登录' }).click();
    await login('alice');
    await openKnowledge();
    await page
      .getByRole('button', { name: `${knowledgeTitle} 业务口径 · 已确认`, exact: true })
      .click();
    await expect(page.locator('.knowledge-state')).toHaveText('已确认');
    await page.getByLabel('撤销原因').fill('本轮合成验收结束，停止后续引用。');
    await page.getByRole('button', { name: '撤销资料', exact: true }).click();
    await expect(page.locator('.knowledge-state')).toHaveText('已撤销');
    await expect(page.getByRole('button', { name: '引用到本轮智能查询' })).toHaveCount(0);
  });
  await check(names[10], async () => {
    if (!aliceReady) return false;
    await closeDialog();
    await page.setViewportSize({ width: 390, height: 844 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(
      true,
    );
    await page.screenshot({ path: join(directory, 'browser-mobile.png'), fullPage: true });
  });
  await check(names[11], async () => {
    expect(runModes).toEqual(['query', 'query']);
  });
  await context.close();
} catch {
  startupError = true;
} finally {
  await browser?.close().catch(() => {});
}
for (const name of names)
  if (!checks.some((check) => check.name === name))
    checks.push({
      name,
      status: 'skipped',
      error: startupError ? 'browser_setup_failed' : 'dependency_unavailable',
    });
const passed = checks.filter((check) => check.status === 'passed').length;
process.stdout.write(JSON.stringify({ passed, planned: names.length, checks }) + '\n');
process.exitCode = passed === names.length ? 0 : 1;
