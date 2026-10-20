// Actual app controls over five synthetic hubs. Covers the reported hover
// cascade, native/management selects, portal styling, and phone hit targets.
const { chromium } = require('playwright');
const { AxeBuilder } = require('@axe-core/playwright');
const assert = require('node:assert/strict');
const { mkdirSync } = require('node:fs');
const { designFixture } = require('./design-fixture.cjs');
const { PASSWORD } = require('./app-fixture-server.cjs');

(async () => {
  const fixture = await designFixture();
  const base = `http://127.0.0.1:${await fixture.app.listen(0)}`;
  const shots = process.env.APP_SHOTS || '/private/tmp/hz-app-design';
  mkdirSync(shots, { recursive: true });
  const browser = await chromium.launch();
  let page;
  try {
    const context = await browser.newContext({ viewport: { width: 1470, height: 860 }, serviceWorkers: 'block' });
    page = await context.newPage();
    page.setDefaultTimeout(30000);
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    const colours = locator => locator.evaluate(e => {
      const s = getComputedStyle(e);
      return { fg: s.color, bg: s.backgroundColor, border: s.borderColor };
    });
    const setMode = async mode => {
      await page.goto(`${base}/settings#/me/appearance`);
      const label = { light: 'Light', dark: 'Dark', oled: 'Black (OLED)' }[mode];
      await page.getByRole('radio', { name: label, exact: true }).click();
      await page.waitForFunction(mode => document.documentElement.dataset.appearance === mode, mode);
    };
    const choose = async (label, value) => {
      await page.getByRole('combobox', { name: label, exact: true }).click();
      await page.getByRole('option', { name: value, exact: true }).click();
    };
    async function overflow() {
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), page.url());
    }
    async function checkHover(button) {
      await page.waitForTimeout(150); // enabled/disabled colour transitions have settled
      const before = await colours(button);
      await button.hover();
      await page.waitForTimeout(150);
      const hover = await colours(button);
      assert.equal(hover.fg, before.fg, 'filled button keeps its label colour on hover');
      assert.notEqual(hover.bg, hover.fg);
    }
    await page.goto(`${base}/auth`);
    await page.getByLabel('Email', { exact: true }).fill('ada@example.com');
    await page.getByLabel('Password', { exact: true }).fill(PASSWORD);
    await checkHover(page.getByRole('button', { name: 'Sign in', exact: true }));
    await page.getByRole('button', { name: 'Sign in', exact: true }).click();
    await page.waitForURL(`${base}/`);

    for (const mode of ['light', 'dark', 'oled']) {
      await setMode(mode);
      const personalTabs = page.getByRole('navigation', { name: 'Personal preferences' });
      for (const tab of await personalTabs.getByRole('link').all()) {
        await tab.hover();
        assert.equal((await colours(tab)).bg, 'rgba(0, 0, 0, 0)');
      }
      await page.goto(`${base}/settings#/me/profile`);
      await page.getByLabel('Display name', { exact: true }).fill(`Ada ${mode}`);
      const save = page.getByRole('button', { name: 'Save', exact: true });
      await checkHover(save);
      const primary = await save.evaluate(e => getComputedStyle(document.documentElement).getPropertyValue('--hz-accent').trim());
      assert.equal(primary.toUpperCase(), fixture.theme.palettes[mode].action);
      await save.click();
      await page.getByText('Name saved.', { exact: true }).waitFor();
      await page.screenshot({ path: `${shots}/${mode}-profile.png` });

      await page.goto(`${base}/settings#/agents`);
      const agents = page.getByRole('table', { name: 'Agents', exact: true });
      await agents.waitFor({timeout: 60000});
      // A header row and one row per agent; the row is one link, with no shadow.
      assert.equal(await agents.getByRole('row').count(), 6);
      await agents.getByRole('link', { name: 'Open Operations', exact: true }).hover();
      assert.equal(await page.locator('.hz-agent-row').filter({ has: page.getByRole('link', { name: 'Open Operations', exact: true }) }).evaluate(e => getComputedStyle(e).boxShadow), 'none');
      await page.screenshot({ path: `${shots}/${mode}-agents.png` });

      await page.goto(`${base}/settings#/people`);
      await page.locator('.person-cell').first().waitFor();
      const addUser = page.getByRole('button', { name: 'Add user', exact: true });
      await checkHover(addUser);
      await page.getByRole('combobox', { name: 'Role', exact: true }).click();
      const popup = page.locator('.hz-native-popup.ant-select-dropdown');
      await popup.waitFor();
      const option = popup.locator('.ant-select-item-option').first();
      await option.hover();
      const colour = await option.evaluate(e => getComputedStyle(e).color);
      assert.equal(colour, await page.locator('.console-embed').evaluate(e => getComputedStyle(e).color));
      await page.screenshot({ path: `${shots}/${mode}-people-menu.png` });
      await page.keyboard.press('Escape');
      await addUser.click();
      const drawer = page.getByRole('dialog');
      await drawer.waitFor();
      const review = drawer.getByRole('button', { name: 'Review', exact: true });
      await checkHover(review);
      await review.click();
      assert.ok(await review.isDisabled());
      await page.screenshot({ path: `${shots}/${mode}-add-user.png` });
      await page.keyboard.press('Escape');

      // New Radix control, including keyboard selection and focus restoration.
      await page.goto(`${base}/settings#/workflow-access`);
      const agent = page.getByRole('combobox', { name: 'Agent', exact: true });
      await agent.waitFor();
      await agent.click();
      const operations = page.getByRole('option', { name: 'Operations', exact: true });
      await operations.hover();
      const axe = await new AxeBuilder({ page }).include('.hz-select-content').analyze();
      assert.deepEqual(axe.violations.map(v => v.id), []);
      await page.screenshot({ path: `${shots}/${mode}-agent-menu.png` });
      await page.keyboard.press('Tab');
      assert.ok(await page.locator('.hz-select-content').evaluate(e => e.contains(document.activeElement)));
      await page.keyboard.press('Escape');
      await page.waitForFunction(() => document.activeElement?.getAttribute('role') === 'combobox');
      await choose('Agent', 'Operations');
      assert.match(await agent.innerText(), /Operations/);
      await overflow();
      console.log(`PASS: ${mode} shared actions, five agents, management popup/drawer, Radix menu and tabs`);
    }
    // Brand token overrides still reach native controls; do not hardcode orange.
    await page.goto(`${base}/settings#/me/profile`);
    await page.getByLabel('Display name').fill('Ada branded');
    await page.evaluate(() => {
      document.documentElement.style.setProperty('--hz-accent', '#1F6F5C');
      document.documentElement.style.setProperty('--hz-accent-hover', '#185948');
      document.documentElement.style.setProperty('--hz-accent-ink', '#FFFFFF');
    });
    const custom = page.getByRole('button', { name: 'Save', exact: true });
    await page.waitForFunction(() => {
      const button = document.querySelector('form button.hz-btn-primary');
      return button && !button.disabled && getComputedStyle(button).backgroundColor === 'rgb(31, 111, 92)';
    });
    assert.equal((await colours(custom)).bg, 'rgb(31, 111, 92)');
    await checkHover(custom);

    for (const width of [1024, 390]) {
      await page.setViewportSize({ width, height: width === 390 ? 844 : 768 });
      for (const hash of ['/me/profile', '/me/appearance', '/me/connections', '/agents', '/people', '/agents/finance/access', '/agents/finance/runs', '/settings/chat', '/settings/appearance', '/workflow-access']) {
        await page.goto(`${base}/settings#${hash}`);
        await page.locator('.hz-set-main').waitFor();
        await page.waitForTimeout(180);
        await overflow();
      }
      if (width === 390) {
        const select = page.getByRole('combobox', { name: 'Agent', exact: true });
        assert.ok((await select.boundingBox()).height >= 44);
        await select.click();
        for (const option of await page.getByRole('option').all()) assert.ok((await option.boundingBox()).height >= 44);
        await page.keyboard.press('Escape');
      }
      await page.screenshot({ path: `${shots}/settings-${width}.png` });
    }
    const member = await browser.newContext({ viewport: { width: 390, height: 844 }, serviceWorkers: 'block' });
    const mp = await member.newPage();
    await mp.goto(`${base}/auth`);
    await mp.getByLabel('Email', { exact: true }).fill('sam@example.com');
    await mp.getByLabel('Password', { exact: true }).fill(PASSWORD);
    await mp.getByRole('button', { name: 'Sign in', exact: true }).click();
    await mp.waitForURL(`${base}/`);
    await mp.goto(`${base}/settings#/me/profile`);
    await mp.getByLabel('Display name').waitFor();
    assert.equal(await mp.locator('.hz-set-nav').getByRole('link', { name: 'Team', exact: true }).count(), 0);
    assert.ok(await mp.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    assert.deepEqual(errors, []);
    console.log('PASS: custom action tokens, tablet/phone settings, 44px Radix options, member preferences');
  } catch (error) {
    if (page) {
      console.error(await page.locator('body').innerText().catch(() => ''));
      await page.screenshot({ path: `${shots}/failure.png` }).catch(() => {});
    }
    throw error;
  } finally {
    await browser.close();
    await fixture.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
