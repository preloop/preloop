import { expect } from '@open-wc/testing';
import sinon from 'sinon';

import { randomUuid } from './random-uuid';

describe('randomUuid', () => {
  it('uses cryptographic random bytes when randomUUID is unavailable', () => {
    const stub = sinon.stub(crypto, 'randomUUID').value(undefined);
    try {
      const one = randomUuid();
      const two = randomUuid();
      expect(one).to.match(
        /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
      );
      expect(two).to.not.equal(one);
    } finally {
      stub.restore();
    }
  });
});
