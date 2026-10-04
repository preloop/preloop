import { expect } from '@open-wc/testing';

import {
  allowlistEntryMatchesModel,
  findModelForAllowedEntry,
  gatewayAliasCandidates,
  gatewayAliasForModel,
  normalizeAllowedModels,
  type AllowlistModel,
} from './model-allowlist';

const OPUS: AllowlistModel = {
  id: '11111111-1111-4111-8111-111111111111',
  name: 'Opus Example',
  provider_name: 'Anthropic',
  model_identifier: 'claude-opus-4-1',
};

const CONFIGURED: AllowlistModel = {
  id: 'model-configured',
  name: 'Configured Chat',
  provider_name: 'openai',
  model_identifier: 'gpt-example',
  meta_data: { gateway: { enabled: true, model_alias: 'team/fast-chat' } },
};

const OTHER_PROVIDER: AllowlistModel = {
  id: 'model-other',
  name: 'Other Opus',
  provider_name: 'vendor',
  model_identifier: 'claude-opus-4-1',
};

describe('model allowlist matching', () => {
  it('derives the alias the console writes', () => {
    expect(gatewayAliasForModel(OPUS)).to.equal('anthropic/claude-opus-4-1');
    expect(gatewayAliasForModel(CONFIGURED)).to.equal('team/fast-chat');
    expect(
      gatewayAliasForModel({ id: 'x', model_identifier: 'bare-model' })
    ).to.equal('openai/bare-model');
  });

  it('lists every spelling the gateway resolver accepts', () => {
    expect([...gatewayAliasCandidates(CONFIGURED)].sort()).to.deep.equal(
      [
        'team/fast-chat',
        'fast-chat',
        'gpt-example',
        'openai/gpt-example',
      ].sort()
    );
  });

  it('names a gateway model with no identifier by its provider', () => {
    const model: AllowlistModel = {
      id: 'x',
      provider_name: 'Acme',
      model_identifier: '',
      meta_data: { gateway: { enabled: true } },
    };
    expect([...gatewayAliasCandidates(model)]).to.deep.equal(['acme']);
    expect(gatewayAliasCandidates({ ...model, meta_data: null }).size).to.equal(
      0
    );
  });

  it('matches a bare model_identifier', () => {
    expect(allowlistEntryMatchesModel('claude-opus-4-1', OPUS)).to.equal(true);
    expect(allowlistEntryMatchesModel('gpt-example', CONFIGURED)).to.equal(
      true
    );
  });

  it('matches the configured meta_data.gateway.model_alias and its tail', () => {
    expect(allowlistEntryMatchesModel('team/fast-chat', CONFIGURED)).to.equal(
      true
    );
    expect(allowlistEntryMatchesModel('fast-chat', CONFIGURED)).to.equal(true);
  });

  it('keeps the default provider/model alias when an alias is configured', () => {
    expect(
      allowlistEntryMatchesModel('openai/gpt-example', CONFIGURED)
    ).to.equal(true);
  });

  it('matches provider/model with the provider lower-cased', () => {
    expect(
      allowlistEntryMatchesModel('anthropic/claude-opus-4-1', OPUS)
    ).to.equal(true);
    // Aliases are wire strings and compare exactly.
    expect(
      allowlistEntryMatchesModel('Anthropic/claude-opus-4-1', OPUS)
    ).to.equal(false);
  });

  it('matches the display name and id case-insensitively, trimmed', () => {
    expect(allowlistEntryMatchesModel('  opus example ', OPUS)).to.equal(true);
    expect(
      allowlistEntryMatchesModel(String(OPUS.id).toUpperCase(), OPUS)
    ).to.equal(true);
  });

  it('does not match another provider’s model', () => {
    expect(
      allowlistEntryMatchesModel('anthropic/claude-opus-4-1', OTHER_PROVIDER)
    ).to.equal(false);
    expect(allowlistEntryMatchesModel('vendor/claude-opus-4-1', OPUS)).to.equal(
      false
    );
    expect(allowlistEntryMatchesModel('team/fast-chat', OPUS)).to.equal(false);
    expect(allowlistEntryMatchesModel('Opus Example', OTHER_PROVIDER)).to.equal(
      false
    );
  });

  it('honours a shared bare tail for every model that answers to it', () => {
    // The gateway checks each model on its own, so a bare identifier is
    // allowed for both imports of the same upstream model.
    expect(allowlistEntryMatchesModel('claude-opus-4-1', OPUS)).to.equal(true);
    expect(
      allowlistEntryMatchesModel('claude-opus-4-1', OTHER_PROVIDER)
    ).to.equal(true);
  });

  it('ignores non-strings and blanks', () => {
    expect(allowlistEntryMatchesModel(null, OPUS)).to.equal(false);
    expect(allowlistEntryMatchesModel('   ', OPUS)).to.equal(false);
    expect(
      normalizeAllowedModels([' a ', null, 3, '', 'a', 'b'])
    ).to.deep.equal(['a', 'b']);
  });
});

describe('findModelForAllowedEntry', () => {
  const models = [OPUS, CONFIGURED, OTHER_PROVIDER];

  it('resolves exact alias, id and name', () => {
    expect(findModelForAllowedEntry('team/fast-chat', models)).to.equal(
      CONFIGURED
    );
    expect(findModelForAllowedEntry('MODEL-OTHER', models)).to.equal(
      OTHER_PROVIDER
    );
    expect(findModelForAllowedEntry('opus example', models)).to.equal(OPUS);
  });

  it('resolves a unique bare identifier or tail', () => {
    expect(findModelForAllowedEntry('gpt-example', models)).to.equal(
      CONFIGURED
    );
    expect(findModelForAllowedEntry('fast-chat', models)).to.equal(CONFIGURED);
    expect(findModelForAllowedEntry('openai/gpt-example', models)).to.equal(
      CONFIGURED
    );
  });

  it('leaves a spelling shared by two models unresolved', () => {
    expect(findModelForAllowedEntry('claude-opus-4-1', models)).to.equal(null);
  });

  it('leaves an unknown entry unresolved', () => {
    expect(findModelForAllowedEntry('nobody/nothing', models)).to.equal(null);
  });
});
