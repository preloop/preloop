import { expect } from '@open-wc/testing';
import { ICON_ALIASES, safeIconName, withSafeIcon } from './icon-name';

describe('icon-name', () => {
  it('maps the stored names that 404 to shipped icons', () => {
    expect(safeIconName('broom')).to.equal('eraser');
    expect(safeIconName('beaker')).to.equal('clipboard-check');
    expect(safeIconName('checklist')).to.equal('list-check');
    expect(safeIconName('document-report')).to.equal('file-earmark-text');
    expect(safeIconName('gear')).to.equal('gear');
    expect(safeIconName(undefined)).to.equal(undefined);
  });

  it('rewrites only the icon of a flow or preset', () => {
    const flow = { id: 'f1', icon: 'beaker', name: 'QA' };
    expect(withSafeIcon(flow)).to.deep.equal({
      id: 'f1',
      icon: 'clipboard-check',
      name: 'QA',
    });
    const fine = { id: 'f2', icon: 'bug' };
    expect(withSafeIcon(fine)).to.equal(fine);
    expect(withSafeIcon(null)).to.equal(null);
  });

  it('points every alias at an icon the vendored set ships', async () => {
    for (const target of Object.values(ICON_ALIASES)) {
      const res = await fetch(
        `/node_modules/@shoelace-style/shoelace/dist/assets/icons/${target}.svg`
      );
      expect(res.ok, `${target}.svg ships`).to.be.true;
    }
    for (const missing of Object.keys(ICON_ALIASES)) {
      const res = await fetch(
        `/node_modules/@shoelace-style/shoelace/dist/assets/icons/${missing}.svg`
      );
      expect(res.ok, `${missing}.svg still missing upstream`).to.be.false;
    }
  });
});
