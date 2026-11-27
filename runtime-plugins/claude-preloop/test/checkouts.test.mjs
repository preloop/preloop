import assert from 'node:assert/strict';
import { test } from 'node:test';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { knownCheckouts, remoteIdentity } from '../dist/checkouts.js';

test('checkout identity strips credentials and keeps tracker host', () => {
  assert.equal(remoteIdentity('git@git.example.com:team/example.git'), 'git.example.com/team/example');
  assert.equal(remoteIdentity('https://user:example@git.example.com/team/example.git?query=example'), 'git.example.com/team/example');
  assert.equal(remoteIdentity('/tmp/local'), undefined);
  assert.equal(remoteIdentity('C:/work/example.git'), undefined);
  assert.equal(remoteIdentity(String.raw`C:\work\example.git`), undefined);
});

test('offers only existing git checkouts from known directories', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'talk-checkouts-'));
  try {
    const checkout = path.join(root, 'project');
    await fs.mkdir(checkout);
    execFileSync('git', ['init', checkout], { stdio: 'ignore' });
    execFileSync('git', ['-C', checkout, 'remote', 'add', 'origin', 'https://git.example.com/team/example.git']);
    const result = await knownCheckouts({workspace_root: checkout, runner_config_path: path.join(root, 'missing.json')}, [path.join(root, 'absent')]);
    assert.deepEqual(result, [{path: await fs.realpath(checkout), remote: 'git.example.com/team/example'}]);
  } finally { await fs.rm(root, {recursive: true, force: true}); }
});
