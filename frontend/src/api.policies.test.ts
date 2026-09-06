/**
 * Shape guards for the policy endpoints the console's Policies page reads.
 *
 * These reproduce the payloads the API actually returns (checked against
 * backend/preloop/api/endpoints/policies.py), not the payloads the console
 * used to assume. A wrapper object reaching Lit's repeat() directive is what
 * produced "TypeError: e is not iterable" on the Policies page.
 */
import { expect } from '@open-wc/testing';
import sinon from 'sinon';

import {
  listPolicyVersions,
  normalizePolicyDiff,
  normalizePolicyRollback,
  normalizePolicyVersions,
} from './api.js';

function json(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

/** Exactly what GET /api/v1/policies/versions answers with. */
const PROD_VERSIONS_PAYLOAD = {
  versions: [
    {
      id: '2c1d0f2e-0000-4000-8000-000000000001',
      version_number: 3,
      tag: 'production',
      description: 'Before the rollout',
      is_active: true,
      mcp_servers_count: 2,
      policies_count: 4,
      tools_count: 7,
      created_at: '2026-09-01T10:00:00+00:00',
      created_by_user_id: '9f1d0f2e-0000-4000-8000-000000000009',
    },
  ],
  total: 1,
};

describe('policy version and diff shapes', () => {
  describe('normalizePolicyVersions', () => {
    it('unwraps the {versions, total} object the API returns', () => {
      const versions = normalizePolicyVersions(PROD_VERSIONS_PAYLOAD);

      expect(versions).to.be.an('array').with.lengthOf(1);
      expect(versions[0].version_number).to.equal(3);
      expect(versions[0].tag).to.equal('production');
      expect(versions[0].is_active).to.equal(true);
    });

    it('lifts the flat counts into snapshot_summary', () => {
      const [version] = normalizePolicyVersions(PROD_VERSIONS_PAYLOAD);

      expect(version.snapshot_summary).to.deep.equal({
        mcp_servers_count: 2,
        tools_count: 7,
        policies_count: 4,
      });
    });

    it('keeps a nested snapshot_summary when one is present', () => {
      const [version] = normalizePolicyVersions([
        {
          id: 'v1',
          version_number: 1,
          is_active: false,
          snapshot_summary: {
            mcp_servers_count: 5,
            tools_count: 6,
            policies_count: 7,
          },
        },
      ]);

      expect(version.snapshot_summary.tools_count).to.equal(6);
    });

    it('answers with an array for a bare array, {items}, null, or junk', () => {
      expect(normalizePolicyVersions([])).to.deep.equal([]);
      expect(normalizePolicyVersions({ items: [] })).to.deep.equal([]);
      expect(normalizePolicyVersions(null)).to.deep.equal([]);
      expect(normalizePolicyVersions(undefined)).to.deep.equal([]);
      expect(normalizePolicyVersions('nope')).to.deep.equal([]);
      expect(normalizePolicyVersions({ versions: null })).to.deep.equal([]);
    });

    it('is iterable so repeat() cannot throw on it', () => {
      const versions = normalizePolicyVersions(PROD_VERSIONS_PAYLOAD);
      const ids: string[] = [];
      for (const version of versions) {
        ids.push(version.id);
      }
      expect(ids).to.have.lengthOf(1);
    });
  });

  describe('listPolicyVersions', () => {
    let fetchStub: sinon.SinonStub;

    beforeEach(() => {
      localStorage.setItem('accessToken', 'test-access-token');
      fetchStub = sinon.stub(window, 'fetch');
    });

    afterEach(() => {
      fetchStub.restore();
      localStorage.clear();
    });

    it('returns an array for the wrapped API payload', async () => {
      fetchStub.resolves(json(PROD_VERSIONS_PAYLOAD));

      const versions = await listPolicyVersions(50);

      expect(Array.isArray(versions)).to.equal(true);
      expect(versions).to.have.lengthOf(1);
      expect(String(fetchStub.firstCall.args[0])).to.contain(
        '/api/v1/policies/versions?limit=50'
      );
    });

    it('throws a readable error when the request fails', async () => {
      fetchStub.resolves(json({ detail: 'nope' }, 500));

      let message = '';
      try {
        await listPolicyVersions();
      } catch (error) {
        message = (error as Error).message;
      }
      expect(message).to.equal('Failed to fetch versions');
    });
  });

  describe('normalizePolicyDiff', () => {
    it('groups the flat change list the diff endpoint returns', () => {
      const diff = normalizePolicyDiff({
        has_changes: true,
        summary: '3 changes',
        changes: [
          { path: '$.tools[name=shell]', operation: 'add' },
          { path: '$.mcp_servers[name=github]', operation: 'remove' },
          { path: '$.model_io[id=deny-pii]', operation: 'modify' },
        ],
      });

      expect(diff?.changes.added).to.deep.equal([
        { type: 'added', category: 'Tool', name: 'shell', details: undefined },
      ]);
      expect(diff?.changes.removed[0].category).to.equal('MCP server');
      expect(diff?.changes.removed[0].name).to.equal('github');
      expect(diff?.changes.modified[0].category).to.equal('Model I/O rule');
      expect(diff?.changes.modified[0].name).to.equal('deny-pii');
      expect(diff?.has_changes).to.equal(true);
    });

    it('labels whole-section changes without a name', () => {
      const diff = normalizePolicyDiff({
        has_changes: true,
        summary: '1 change',
        changes: [{ path: '$.defaults', operation: 'modify' }],
      });

      expect(diff?.changes.modified[0].category).to.equal('Defaults');
      expect(diff?.changes.modified[0].name).to.equal('');
    });

    it('passes an already grouped diff through', () => {
      const diff = normalizePolicyDiff({
        has_changes: true,
        summary: '1 change',
        changes: {
          added: [{ type: 'added', category: 'tools', name: 'shell' }],
          removed: [],
          modified: [],
        },
      });

      expect(diff?.changes.added[0].name).to.equal('shell');
      expect(diff?.changes.removed).to.deep.equal([]);
      expect(diff?.changes.modified).to.deep.equal([]);
    });

    it('fills in the missing buckets so the dialog can read lengths', () => {
      const diff = normalizePolicyDiff({ has_changes: false, summary: 'none' });

      expect(diff?.changes.added).to.deep.equal([]);
      expect(diff?.changes.removed).to.deep.equal([]);
      expect(diff?.changes.modified).to.deep.equal([]);
      expect(diff?.has_changes).to.equal(false);
    });

    it('returns null for a non-object payload', () => {
      expect(normalizePolicyDiff(null)).to.equal(null);
      expect(normalizePolicyDiff('boom')).to.equal(null);
    });
  });

  describe('normalizePolicyRollback', () => {
    it('reads the {success, diff, error} shape the API returns', () => {
      const result = normalizePolicyRollback({
        success: true,
        error: null,
        diff: {
          has_changes: true,
          summary: '1 change',
          changes: [{ path: '$.tools[name=shell]', operation: 'add' }],
        },
      });

      expect(result.success).to.equal(true);
      expect(result.error).to.equal(null);
      expect(result.changes?.has_changes).to.equal(true);
      expect(result.changes?.changes.added[0].name).to.equal('shell');
    });

    it('still reads a diff sent as changes', () => {
      const result = normalizePolicyRollback({
        success: true,
        changes: {
          has_changes: true,
          summary: '1 change',
          changes: { added: [], removed: [], modified: [] },
        },
      });

      expect(result.changes?.has_changes).to.equal(true);
    });

    it('reports a failed rollback without a diff', () => {
      const result = normalizePolicyRollback({
        success: false,
        error: 'Version not found',
        diff: null,
      });

      expect(result.success).to.equal(false);
      expect(result.error).to.equal('Version not found');
      expect(result.changes).to.equal(null);
    });
  });
});
