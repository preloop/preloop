import { expect } from '@open-wc/testing';

import { TALK_COMMANDS, matchCommands, slashQuery } from './talk-command-menu';

describe('talk-command-menu matching', () => {
  it('treats only a lone /word as a command being typed', () => {
    expect(slashQuery('/')).to.equal('');
    expect(slashQuery('/Res')).to.equal('res');
    expect(slashQuery('/new please')).to.equal(null);
    expect(slashQuery('hello /new')).to.equal(null);
    expect(slashQuery('/usr/bin')).to.equal(null);
  });

  it('filters by prefix within what is available', () => {
    expect(matchCommands('/re').map((c) => c.name)).to.deep.equal([
      'resume',
      'release',
    ]);
    expect(matchCommands('/deploy')).to.deep.equal([]);
    const noRelease = TALK_COMMANDS.filter((c) => c.name !== 'release');
    expect(matchCommands('/re', noRelease).map((c) => c.name)).to.deep.equal([
      'resume',
    ]);
  });
});
