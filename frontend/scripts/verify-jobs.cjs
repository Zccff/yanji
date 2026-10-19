// Exercise polling contracts without a running API or a test-runner dependency.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');

function load(fetch) {
  const source = fs.readFileSync(path.join(__dirname, '../lib/jobs.ts'), 'utf8');
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText;
  const exports = {};
  vm.runInNewContext(compiled, {
    exports, require: () => ({ API_BASE_URL: 'http://fixture' }), fetch,
    AbortSignal, DOMException, Date, clearTimeout,
    setTimeout: (callback) => setTimeout(callback, 1),
  });
  return exports;
}

function response(status, body) {
  return { ok: status === 200, status, json: async () => body };
}

(async () => {
  let calls = 0;
  const seen = [];
  const recovered = load(async () => {
    calls += 1;
    if (calls === 1) throw new TypeError('Network temporarily unavailable');
    if (calls === 2) return response(503);
    if (calls === 3) return response(429);
    return response(200, { status: calls === 4 ? 'running' : 'completed', result: {} });
  });
  const job = await recovered.waitForRepoJob('repo', 'job', { timeoutMs: 2000, onProgress: (item) => seen.push(item.status) });
  assert.equal(job.status, 'completed');
  assert.equal(calls, 5);
  assert.deepEqual(seen, ['running', 'completed']);

  const missing = load(async () => response(404));
  await assert.rejects(missing.waitForRepoJob('repo', 'job'), /任务或仓库已移除/);
  const failed = load(async () => response(200, { status: 'failed', result: {}, error_message: 'permission denied' }));
  await assert.rejects(failed.waitForRepoJob('repo', 'job'), /permission denied/);

  const controller = new AbortController();
  const cancelled = load(async () => { controller.abort(); return response(200, { status: 'completed', result: {} }); });
  await assert.rejects(cancelled.waitForRepoJob('repo', 'job', { signal: controller.signal, onProgress: () => assert.fail('Unmounted page received progress') }), { name: 'AbortError' });
  console.log('4 polling scenarios passed: transient recovery, missing task, terminal failure, abort.');
})().catch((error) => { console.error(error); process.exitCode = 1; });
