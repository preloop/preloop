import { expect } from '@open-wc/testing';
import { unifiedYamlDiff, yamlDocumentsEqual } from './yaml-unified-diff';

describe('yaml-unified-diff', () => {
  it('treats trailing whitespace as equal', () => {
    expect(yamlDocumentsEqual('version: "1.0"\n', 'version: "1.0"')).to.be.true;
  });

  it('emits a unified diff with added and removed lines', () => {
    const before = ['version: "1.0"', 'tools:', '  - name: tool_a'].join('\n');
    const after = [
      'version: "1.0"',
      'tools:',
      '  - name: tool_a',
      'model_io:',
      '  - id: deny-pii',
    ].join('\n');
    const diff = unifiedYamlDiff(before, after);
    expect(diff).to.contain('--- a/policies.yaml');
    expect(diff).to.contain('+++ b/policies.yaml');
    expect(diff).to.contain('+model_io:');
    expect(diff).to.contain('+  - id: deny-pii');
    expect(diff).to.contain(' version: "1.0"');
  });
});
