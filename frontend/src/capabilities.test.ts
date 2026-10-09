import { expect } from '@open-wc/testing';
import { editionOf } from './capabilities';

describe('backend edition contract', () => {
  for (const edition of ['oss', 'cloud', 'enterprise'] as const) {
    it(`uses explicit ${edition} despite installed plugins`, () => {
      expect(
        editionOf({
          edition,
          plugins: [{ name: 'billing', version: '1', description: '' }],
        })
      ).to.equal(edition);
    });
  }
  it('defaults older or missing payloads to OSS', () => {
    expect(editionOf(undefined)).to.equal('oss');
    expect(editionOf({ plugins: [] })).to.equal('oss');
  });
});
