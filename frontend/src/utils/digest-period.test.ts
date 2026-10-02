import { expect } from '@open-wc/testing';
import { parseDigestLink, withoutDigestPeriod } from './digest-period';
import { safeLoginReturn, consumeLoginReturn } from './login-return';

const start = '2026-09-17T09:00:00.123456Z';
const end = '2026-09-24T09:00:00.123456Z';
const account = '00000000-0000-4000-8000-000000000001';
const query = `?account_id=${account}&start_date=${start}&end_date=${end}`;

describe('digest exact-window links', () => {
  it('retains microseconds and compares fractional timestamps exactly', () => {
    expect(parseDigestLink(query).period).to.deep.equal({
      startDate: start,
      endDate: end,
    });
    expect(
      parseDigestLink(
        '?start_date=2026-09-17T09:00:00.000001Z&end_date=2026-09-17T09:00:00.000002Z'
      ).period
    ).not.to.equal(null);
  });
  it('rejects invalid calendar dates, offsets, partial, reversed and duplicate ranges', () => {
    for (const search of [
      '?start_date=2026-02-30T00:00:00Z&end_date=2026-03-02T00:00:00Z',
      `?start_date=${start}&end_date=2026-09-24T09:00:00+00:00`,
      `?start_date=${end}&end_date=${start}`,
      `?start_date=${start}`,
      `?end_date=${end}`,
      `?start_date=${start}&start_date=${start}&end_date=${end}`,
      `?start_date=${start}&end_date=${end}&end_date=${end}`,
      '?start_date=2026-09-17T24:00:00Z&end_date=2026-09-25T00:00:00Z',
    ]) {
      expect(parseDigestLink(search).period, search).to.equal(null);
      expect(parseDigestLink(search).error, search).not.to.equal(null);
    }
  });
  it('blocks malformed and duplicate account contexts', () => {
    expect(parseDigestLink(query + `&account_id=${account}`).blocked).to.equal(
      true
    );
    expect(parseDigestLink('?account_id=garbage').blocked).to.equal(true);
    expect(
      parseDigestLink(`?start_date=${start}&end_date=${end}`).accountId
    ).to.equal(null);
  });
  it('removes digest context while preserving unrelated parameters and hash', () => {
    expect(
      withoutDigestPeriod('/console/cost' + query + '&panel=pricing#detail')
    ).to.equal('/console/cost?panel=pricing#detail');
  });
});

describe('safe login return', () => {
  it('retains local pathname and exact query through consumption', () => {
    const url = '/console/cost' + query;
    localStorage.setItem('loginRedirect', url);
    expect(consumeLoginReturn()).to.equal(url);
    expect(localStorage.getItem('loginRedirect')).to.equal(null);
  });
  it('rejects external, scheme-relative, backslash and control forms', () => {
    for (const value of [
      'https://example.com',
      '//example.com',
      '/\\example.com',
      '/%5cexample.com',
      '/%2fexample.com',
      '/cost\n',
      '/%00cost',
      'javascript:alert(1)',
    ]) {
      expect(safeLoginReturn(value), value).to.equal(null);
    }
  });
});
