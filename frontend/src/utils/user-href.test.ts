import { expect } from '@open-wc/testing';
import { userHref } from './user-href';

describe('userHref', () => {
  it('points at the person page and encodes the id', () => {
    expect(userHref('user-1')).to.equal('/console/settings/users/user-1');
    expect(userHref('a/b c')).to.equal('/console/settings/users/a%2Fb%20c');
  });
});
