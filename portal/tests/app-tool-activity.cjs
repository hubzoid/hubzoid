// Compact tool activity: grouping, streaming, disclosures and small screens.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const { mkdirSync } = require('node:fs');
const { createApp, PASSWORD } = require('./app-fixture-server.cjs');
(async () => {
  const fixture = createApp({ mode: 'accounts' });
  const dev = process.env.APP_DEV ? await require('./app-dev-server.cjs').attachDev(fixture) : null;
  const base = `http://127.0.0.1:${await fixture.listen(0)}`;
  const browser = await chromium.launch();
  const shots = process.env.APP_SHOTS || '/private/tmp/hz-tool-activity';
  mkdirSync(shots, { recursive: true });
  let debugPage;
  try {
    const context = await browser.newContext({ viewport: { width: 1470, height: 860 }, serviceWorkers: 'block' });
    const page = await context.newPage();
    debugPage = page;
    page.setDefaultTimeout(10000);
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    page.on('console', m => { if (m.type() === 'error' && !m.text().includes('Failed to load resource')) errors.push(m.text()); });
    await page.goto(`${base}/auth`);
    await page.getByLabel('Email', { exact: true }).fill('ada@example.com');
    await page.getByLabel('Password', { exact: true }).fill(PASSWORD);
    await page.getByRole('button', { name: 'Sign in', exact: true }).click();
    await page.waitForURL(`${base}/`);
    const conv = fixture.state.conversations.get('c_adapricing01');
    const reply = fixture.state.messages.get(conv.head_id);
    const calls = Array.from({ length: 20 }, (_, i) => ({ type: 'tool-call', toolCallId: `call_long_${i}`, toolName: 'search_sources', args: { query: 'Stock count and order exceptions' }, result: { matches: ['Escalate order exceptions above 500 units.'] } }));
    // Adapt the older general fixture to the current conversation graph contract.
    const adapt = async context => {
      await context.route(`**/api/conversations/${conv.id}`, async route => {
        const response = await route.fetch();
        const snapshot = await response.json();
        await route.fulfill({ json: { ...snapshot, role: 'owner', permissions: { read: true, write: true, manage: true },
          branches: [{ id: 'b_activity', head_id: conv.head_id, queue_revision: 0 }],
          view: { branch_id: 'b_activity', view_head_id: conv.head_id, revision: 0 }, runs: [], queue: [], cursor: 0 } });
      });
      await context.route(`**/api/conversations/${conv.id}/events?*`, route => route.fulfill({ status: 200, contentType: 'text/event-stream', body: ': connected\n\n' }));
    };
    await adapt(context);
    reply.status = 'running';
    reply.content = [calls[0], { ...calls[1], result: undefined }];
    await page.goto(`${base}/c/${conv.id}`);
    const group = page.getByTestId('tool-group');
    await group.getByRole('button', { name: 'Search sources' }).waitFor();
    assert.equal(await group.getAttribute('data-tool-state'), 'running');
    assert.equal(await group.locator('.is-running').first().evaluate(e => getComputedStyle(e).animationName), 'hz-activity-shimmer');
    await page.emulateMedia({ reducedMotion: 'reduce' });
    assert.equal(await group.locator('.is-running').first().evaluate(e => getComputedStyle(e).animationName), 'none');
    await page.emulateMedia({ reducedMotion: 'no-preference' });
    reply.status = 'complete';
    reply.content = calls.slice(0, 3);
    await page.reload();
    await group.getByRole('button', { name: '3 tool calls', exact: true }).waitFor();
    await page.getByTestId('send').waitFor();
    assert.equal(await page.locator('[data-testid="tool-entry"]:visible').count(), 0);
    await group.getByRole('button', { name: '3 tool calls', exact: true }).press('Enter');
    const first = group.getByTestId('tool-entry').first();
    await first.getByRole('button').click();
    await first.getByText(/Escalate order exceptions/).waitFor();
    assert.equal(await first.getByText(', Done', { exact: true }).evaluate(e => e.className), 'sr-only');

    reply.content = [...calls, { type: 'text', text: 'Order exceptions above 500 units must be escalated.' }];
    await page.goto(`${base}/c/${conv.id}`);
    await group.getByRole('button', { name: '20 tool calls', exact: true }).waitFor();
    assert.equal(await page.locator('[data-testid="tool-entry"]:visible').count(), 0);
    for (const theme of ['light', 'dark']) {
      await page.evaluate(theme => document.documentElement.dataset.theme = theme, theme);
      await page.screenshot({ path: `${shots}/${theme}-collapsed.png` });
    }
    await group.getByRole('button').click();
    assert.equal(await group.locator('[data-testid="tool-entry"]:visible').count(), 20);
    await group.getByTestId('tool-entry').first().getByRole('button').click();
    await page.screenshot({ path: `${shots}/expanded.png` });
    await context.route('**/api/shares?*', route => route.fulfill({ json: { items: [] } }));
    await context.route('**/collaborators', route => route.fulfill({ json: { revision: 0, members: [] } }));
    await page.getByRole('button', { name: 'Share', exact: true }).click();
    const share = page.getByRole('dialog', { name: 'Share chat' });
    await share.getByText('Share this branch as it is now.', { exact: true }).waitFor();
    const help = share.getByRole('button', { name: 'About sharing' });
    await help.focus();
    await page.getByRole('tooltip').waitFor();
    await help.press('Enter');
    assert.equal(await help.getAttribute('aria-expanded'), 'true');
    await page.screenshot({ path: `${shots}/share-help.png` });
    await share.getByRole('button', { name: 'Close', exact: true }).click();
    // Text breaks a group; it must remain in chronological order.
    reply.content = [calls[0], calls[1], { type: 'text', text: 'Checking the next source.' }, calls[2], calls[3]];
    await page.reload();
    await page.getByTestId('tool-group').nth(1).waitFor();
    assert.equal(await page.getByTestId('tool-group').count(), 2);
    await page.getByText('Checking the next source.', { exact: true }).waitFor();
    for (const status of ['cancelled', 'error']) {
      reply.content = [calls[0], { ...calls[1], result: undefined }];
      reply.status = status;
      await page.reload();
      await group.getByRole('button', { name: status === 'cancelled' ? /Stopped/ : /Failed/ }).waitFor();
    }
    reply.status = 'complete';
    reply.content = [calls[0], { ...calls[1], result: { message: 'Source unavailable' }, isError: true }];
    await page.reload();
    await group.getByRole('button', { name: /Failed/ }).click();
    await group.getByTestId('tool-entry').last().getByRole('button').click();
    await group.getByText(/Source unavailable/).waitFor();
    // Approval cards must remain outside collapsed activity.
    for (const result of [{ status: 'waiting', request_id: 'req_test' }, { hub_change: { id: 123 } }]) {
      reply.content = [calls[0], { ...calls[1], result }];
      await page.reload();
      await page.getByTestId(result.request_id ? 'request-card' : 'hub-change-card').waitFor();
      assert.equal(await page.getByTestId('tool-group').count(), 0);
    }
    reply.content = [...calls, { type: 'text', text: 'Order exceptions above 500 units must be escalated.' }];
    const phone = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, storageState: await context.storageState(), serviceWorkers: 'block' });
    await adapt(phone);
    const mobile = await phone.newPage();
    await mobile.goto(`${base}/c/${conv.id}`);
    const toggle = mobile.getByTestId('tool-group').getByRole('button', { name: '20 tool calls', exact: true });
    await toggle.waitFor();
    assert.ok((await toggle.boundingBox()).height >= 44);
    await toggle.tap();
    await mobile.getByTestId('tool-entry').first().getByRole('button').tap();
    assert.ok(await mobile.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await mobile.screenshot({ path: `${shots}/phone.png` });
    await page.goto(`${base}/settings#/people`);
    await page.locator('.console-embed').waitFor({ timeout: 45000 });
    await page.locator('.hz-set-backlink').click();
    const composer = page.getByRole('textbox', { name: /^Message / });
    await composer.focus();
    const focus = await page.locator('.hz-composer').evaluate(e => {
      const s = getComputedStyle(e);
      return { outline: s.outlineColor, expected: s.getPropertyValue('--hz-mute').trim(), width: s.outlineWidth };
    });
    assert.equal(focus.width, '1px');
    // The neutral palette may have a slight warm tint, but never an orange focus ring.
    const rgb = focus.outline.match(/\d+/g).map(Number);
    assert.ok(Math.max(...rgb) - Math.min(...rgb) <= 12, `Focus ring is not neutral: ${focus.outline}`);
    await page.screenshot({ path: `${shots}/composer-focus.png` });
    assert.deepEqual(errors, []);
    console.log('Tool running/completed states, 20-call grouping, results, errors, cancellation, approvals, reduced motion and phone layout passed.');
  } catch (e) {
    console.error(await debugPage.locator("body").innerText());
    await debugPage.screenshot({ path: `${shots}/failure.png` });
    throw e;
  } finally {
    await browser.close();
    await fixture.close();
    await dev?.close();
  }
})().catch(e => { console.error(e); process.exitCode = 1; });
