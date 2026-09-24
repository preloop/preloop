import { html, fixture, expect } from '@open-wc/testing';

import './repository-chip';
import type { RepositoryChip } from './repository-chip';

async function chipOf(toolArgs: Record<string, unknown> | null) {
  const element = (await fixture(
    html`<repository-chip .toolArgs=${toolArgs}></repository-chip>`
  )) as RepositoryChip;
  await element.updateComplete;
  return element;
}

function textOf(element: RepositoryChip): string {
  return element.shadowRoot!.textContent!.replace(/\s+/g, ' ').trim();
}

describe('repository-chip', () => {
  it('renders owner/repo and the relative path from the marker', async () => {
    const element = await chipOf({
      command: 'ls',
      _preloop_repository: {
        remote: 'github.com/example/repo',
        toplevel: '/home/dev/repo',
        relative_path: 'pkg/sub',
        source: 'hook_cwd',
      },
    });

    expect(textOf(element)).to.equal('example/repo · pkg/sub');
    expect(
      element.shadowRoot!.querySelector('[data-testid="repository-chip"]')
    ).to.not.equal(null);
  });

  it('drops the host but keeps nested group paths', async () => {
    const element = await chipOf({
      _preloop_repository: {
        remote: 'gitlab.com/group/sub/repo',
        source: 'hook_cwd',
      },
    });

    expect(textOf(element)).to.equal('group/sub/repo');
    expect(element.shadowRoot!.querySelector('.relative')).to.equal(null);
  });

  it('says "no remote" when the work tree has no origin', async () => {
    const element = await chipOf({
      _preloop_repository: {
        remote: '',
        toplevel: '/home/dev/repo',
        no_remote: true,
        source: 'hook_cwd',
      },
    });

    expect(textOf(element)).to.equal('no remote');
  });

  it('renders nothing without the marker', async () => {
    const element = await chipOf({ command: 'ls', _preloop_source: 'cursor' });
    expect(element.shadowRoot!.querySelector('.chip')).to.equal(null);
    expect(textOf(element)).to.equal('');
  });

  it('renders nothing for a malformed marker', async () => {
    const arrayMarker = await chipOf({ _preloop_repository: ['nope'] });
    expect(arrayMarker.shadowRoot!.querySelector('.chip')).to.equal(null);

    const emptyMarker = await chipOf({ _preloop_repository: {} });
    expect(emptyMarker.shadowRoot!.querySelector('.chip')).to.equal(null);
  });
});
