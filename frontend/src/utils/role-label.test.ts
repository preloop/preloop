import { expect } from '@open-wc/testing';

import { roleLabel } from './role-label';

describe('roleLabel', () => {
  it('turns a stored role name into sentence case', () => {
    expect(roleLabel('owner')).to.equal('Owner');
    expect(roleLabel('team_admin')).to.equal('Team admin');
  });

  it('falls back to "Role" when there is no name', () => {
    expect(roleLabel('')).to.equal('Role');
    expect(roleLabel(null)).to.equal('Role');
    expect(roleLabel(undefined)).to.equal('Role');
  });
});
