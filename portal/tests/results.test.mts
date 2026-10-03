import assert from 'node:assert/strict';
import { resultArtifacts, resultSummary, workflowCommand, shortRunId } from '../src/lib/results.ts';
const id = 'a123456789012345678';
const output = JSON.stringify({ title: 'Weekly report', artifact: { id, title: 'Weekly report', url: `https://hub.example.com/portal/artifacts/${id}` } });
assert.equal(resultSummary(output), 'Weekly report');
assert.deepEqual(resultArtifacts(output), [{ title: 'Weekly report', url: `/portal/artifacts/${id}` }]);
for (const url of ['javascript:alert(1)', '/portal/artifacts/wrong', 'https://evil.example/collect', `/portal/artifacts/${id}?token=secret`]) {
  const links = resultArtifacts(JSON.stringify({ id, url }));
  assert.ok(links.every((x) => x.url === `/portal/artifacts/${id}`));
}
assert.deepEqual(resultArtifacts("{'artifact': 'old Python repr'}"), []);
assert.equal(resultSummary('Plain answer'), 'Plain answer');
assert.equal(workflowCommand('cancel', "run';touch /tmp/unwanted"), "hubzoid schedule cancel '<hub-folder>' 'run'\\'';touch /tmp/unwanted'");
console.log('Workflow result and command checks passed');

assert.equal(shortRunId('manual:5b5e27d62e4144e09f588847ae01b0a8@hz-finance-eab762a0'), 'ae01b0a8');
assert.equal(shortRunId('manual:d24c19affcfd4d56940f0a55e49f1191@hz-finance-eab762a0'), 'e49f1191');
