import { expect } from '@open-wc/testing';

import {
  pickDefaultModel,
  selectableModels,
  supportsServerSideGeneration,
} from './ai-model-selection';
import type { AIModel } from '../types';

function model(overrides: Partial<AIModel> & { id: string }): AIModel {
  return {
    name: `Model ${overrides.id}`,
    provider_name: 'openai',
    model_identifier: 'gpt-5.4',
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  };
}

const byok = (id: string, is_default = false) =>
  model({
    id,
    is_default,
    credential_type: 'api_key',
    has_api_key: true,
    supports_server_side_generation: true,
  });

const oauth = (id: string, is_default = false) =>
  model({
    id,
    is_default,
    provider_name: 'anthropic',
    credential_type: 'oauth_anthropic_claude_code',
    has_api_key: true,
    supports_server_side_generation: false,
  });

describe('supportsServerSideGeneration', () => {
  it('rejects principal-bound OAuth models', () => {
    expect(supportsServerSideGeneration(oauth('a'))).to.be.false;
  });

  it('accepts BYOK models', () => {
    expect(supportsServerSideGeneration(byok('a'))).to.be.true;
  });

  it('defaults to true when the backend omits the field', () => {
    expect(supportsServerSideGeneration(model({ id: 'a' }))).to.be.true;
  });
});

describe('selectableModels', () => {
  it('drops OAuth-backed models', () => {
    const result = selectableModels([oauth('a'), byok('b'), oauth('c')]);
    expect(result.map((m) => m.id)).to.deep.equal(['b']);
  });
});

describe('pickDefaultModel', () => {
  it('never auto-selects an OAuth model flagged as default', () => {
    const picked = pickDefaultModel([oauth('a', true), byok('b')]);
    expect(picked?.id).to.equal('b');
  });

  it('picks the first BYOK model when nothing is flagged', () => {
    const picked = pickDefaultModel([oauth('a'), byok('b'), byok('c')]);
    expect(picked?.id).to.equal('b');
  });

  it('honors an explicitly flagged BYOK default', () => {
    const picked = pickDefaultModel([byok('a'), byok('b', true)]);
    expect(picked?.id).to.equal('b');
  });

  it('returns null when only OAuth models exist', () => {
    // The Show HN persona: a Claude-Code-only user. Selecting anything here
    // would make their first Optimize click fail server-side.
    expect(pickDefaultModel([oauth('a', true), oauth('b')])).to.be.null;
  });

  it('returns null for an empty list', () => {
    expect(pickDefaultModel([])).to.be.null;
  });
});
