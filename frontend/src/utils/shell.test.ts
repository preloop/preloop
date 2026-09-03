import { expect } from '@open-wc/testing';

import { shellQuote } from './shell';

describe('shellQuote', () => {
  it('single-quotes a plain value', () => {
    expect(shellQuote('Hermes')).to.equal("'Hermes'");
    expect(shellQuote('My agent')).to.equal("'My agent'");
  });

  it('neutralises expansion, substitution and quote breakouts', () => {
    expect(shellQuote('a"; rm -rf /; #')).to.equal(`'a"; rm -rf /; #'`);
    expect(shellQuote('$(whoami)')).to.equal("'$(whoami)'");
    expect(shellQuote('`whoami`')).to.equal("'`whoami`'");
    expect(shellQuote('$HOME')).to.equal("'$HOME'");
  });

  it('closes, escapes and reopens around an embedded single quote', () => {
    expect(shellQuote("O'Brien")).to.equal(`'O'\\''Brien'`);
    // The classic breakout: the payload stays one argument.
    expect(shellQuote("'; rm -rf /; '")).to.equal(`''\\''; rm -rf /; '\\'''`);
  });

  it('quotes an empty or missing value rather than dropping the argument', () => {
    expect(shellQuote('')).to.equal("''");
    expect(shellQuote(null)).to.equal("''");
    expect(shellQuote(undefined)).to.equal("''");
  });
});
