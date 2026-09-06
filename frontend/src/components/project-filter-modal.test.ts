import { html, fixture, expect } from '@open-wc/testing';
import './project-filter-modal';
import type { ProjectFilterModal } from './project-filter-modal';

const organizations = [
  { id: 'org-1', name: 'Preloop', tracker_id: 'tracker-1' },
];

const projects = [
  { id: 'p1', name: 'Alpha', organization_id: 'org-1' },
  { id: 'p2', name: 'Orphan', organization_id: 'org-missing' },
];

describe('ProjectFilterModal', () => {
  async function mount() {
    const el = (await fixture(
      html`<project-filter-modal
        .isOpen=${true}
        .organizations=${organizations}
        .projects=${projects}
      ></project-filter-modal>`
    )) as ProjectFilterModal;
    await el.updateComplete;
    return el;
  }

  it('groups projects under the tracker account they came from', async () => {
    const el = await mount();
    const roots = [
      ...(el.shadowRoot?.querySelectorAll('sl-tree > sl-tree-item') || []),
    ];
    expect(roots.length).to.equal(2);
    expect(roots[0].textContent).to.contain('Preloop');
    expect(roots[0].textContent).to.contain('Alpha');
  });

  it('keeps a project whose account did not load selectable', async () => {
    const el = await mount();
    const orphan = el.shadowRoot?.querySelector(
      'sl-tree-item[data-project-id="p2"]'
    );
    expect(orphan).to.exist;
    expect(orphan?.textContent?.trim()).to.equal('Orphan');
  });

  it('labels its sections in sentence case', async () => {
    const el = await mount();
    const labels = [
      ...(el.shadowRoot?.querySelectorAll('.filter-label') || []),
    ].map((node) => node.textContent?.trim());
    expect(labels).to.include('Issue status');
    expect(labels).to.not.include('Issue Status');
  });
});
