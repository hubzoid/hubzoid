// Five synthetic hubs rendered by the actual app. No provider calls or secrets.
const { execFileSync } = require('node:child_process');
const path = require('node:path');
const { createApp } = require('./app-fixture-server.cjs');
const { createFixture } = require('./fixture.cjs');

async function designFixture({ dev = !!process.env.APP_DEV, mode = 'accounts' } = {}) {
  // Exercise the real server palette contract instead of maintaining a second
  // set of colours in JS fixtures. This module only needs Python's stdlib.
  const theme = JSON.parse(execFileSync(process.env.HZ_TEST_PYTHON || 'python3', [
    '-c', 'import json; from hubzoid.chat import theme; print(json.dumps(theme.public(theme.resolve({"version": 1}))))',
  ], { cwd: path.resolve(__dirname, '../..'), encoding: 'utf8' }));
  const management = createFixture();
  for (const [key, name] of [['operations', 'Operations'], ['people-ops', 'People Operations']]) {
    management.state.hubs.push({ key, name, path: `/srv/hubs/${key}` });
    management.state.catalogs[key] = management.state.catalogs.finance;
    management.state.workflows[key] = [];
    management.state.decisions[key] = [];
  }
  const agents = management.state.hubs.map(hub => ({
    id: hub.key, hub: hub.key, name: hub.name, description: `Sample ${hub.name.toLowerCase()} knowledge and workflows.`,
    api_base: `/b/${hub.key}`, suggestions: [], avatar_url: null,
  }));
  const presentation = {
    locale: {default: "en", allowed: ["en"]},
    appearance: {default_mode: "dark", allowed_modes: ["light", "dark", "oled", "system"], default_density: "comfortable", allowed_densities: ["comfortable", "compact"], default_text_scale: 1, allowed_text_scales: [1, 1.125, 1.25, 1.5]},
    preferences: {mode: true, density: true, text_scale: true, timezone: true}, timezone: "UTC", theme_source: null,
  };
  const controls = {
    "/api/chat/config": {revision: 0, digest: "synthetic-defaults", config: {version: 1, presentation}, applied_by: null, applied_at: null, file: {path: "chat/config.yaml", exists: false}, sources: {}},
    "/api/me/access": {agents: agents.map(a => ({hub: a.hub, name: a.name, extra: []})), features: [{key: "dictation", label: "Dictation", on: true}], contact: "admin@example.org", via_key: false},
    "/api/branding/active": {active: null}, "/api/branding/drafts": {items: []}, "/api/branding/history": {items: []},
  };
  const actions = ["run", "schedule", "manage", "view"];
  for (const a of agents) {
    controls[`${a.api_base}/api/workflow-access`] = {revision: 0, yaml: "version: 1\ngrants: []\n", grants: [], effective: [], actions};
    controls[`${a.api_base}/api/workflow-access/resources`] = {items: [{key: "monthly_close", kind: "workflow", allowed_actions: actions}]};
    controls[`${a.api_base}/api/workflow-access/subjects`] = {items: [{subject: "ada@example.com"}]};
  }
  const app = createApp({ mode, agents, theme: {...theme, revision: "builtin"}, consoleFixture: management, controls });
  const vite = dev ? await require('./app-dev-server.cjs').attachDev(app) : null;
  return { app, management, theme, async close() { await vite?.close(); await app.close(); } };
}
module.exports = { designFixture };
if (require.main === module) {
  designFixture({ dev: true, mode: 'local' }).then(async fixture => {
    console.log(`Synthetic five-hub app preview: http://127.0.0.1:${await fixture.app.listen(Number(process.argv[2] || 4197))}/`);
  });
}
