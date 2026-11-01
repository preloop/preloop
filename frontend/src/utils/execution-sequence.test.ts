import { expect } from '@open-wc/testing';
import { ExecutionSequence } from './execution-sequence';

describe('execution sequence recovery', () => {
  it('detects dropped updates and suppresses reordered or duplicate events', () => {
    const cursor = new ExecutionSequence();
    expect(cursor.accept(7)).to.equal('next');
    expect(cursor.accept(8)).to.equal('next');
    expect(cursor.accept(10)).to.equal('gap');
    expect(cursor.accept(9)).to.equal('stale');
    expect(cursor.accept(10)).to.equal('stale');
    expect(cursor.value).to.equal(10);
    expect(cursor.accept(11)).to.equal('next');
  });

  it('accepts legacy publishers during rollout without corrupting the cursor', () => {
    const cursor = new ExecutionSequence();
    cursor.value = 12;
    for (const value of [undefined, null, '13', -1, 1.2, NaN]) {
      expect(cursor.accept(value)).to.equal('legacy');
      expect(cursor.value).to.equal(12);
    }
  });
});
