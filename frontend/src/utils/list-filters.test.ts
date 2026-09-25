import { expect } from '@open-wc/testing';
import sinon from 'sinon';

import {
  DEFAULT_FLOW_EXECUTION_FILTERS,
  FLOW_EXECUTION_FILTERS_KEY,
  FLOW_EXECUTION_QUERY_MAX,
  clearFlowExecutionFilters,
  isDefaultFlowExecutionFilters,
  loadFlowExecutionFilters,
  saveFlowExecutionFilters,
  sanitizeFlowExecutionFilters,
} from './list-filters';

const stored = {
  status: 'SUCCEEDED',
  flow: 'flow-1',
  range: 'week',
  q: 'nightly',
};

describe('list-filters', () => {
  afterEach(() => {
    localStorage.removeItem(FLOW_EXECUTION_FILTERS_KEY);
    sinon.restore();
  });

  it('returns null when nothing is stored', () => {
    expect(loadFlowExecutionFilters()).to.equal(null);
  });

  it('loads a stored filter set', () => {
    localStorage.setItem(FLOW_EXECUTION_FILTERS_KEY, JSON.stringify(stored));
    expect(loadFlowExecutionFilters()).to.deep.equal(stored);
  });

  it('caps search text at 200 characters', () => {
    const q = 'x'.repeat(FLOW_EXECUTION_QUERY_MAX + 40);
    const filters = sanitizeFlowExecutionFilters({ ...stored, q });
    expect(filters?.q.length).to.equal(FLOW_EXECUTION_QUERY_MAX);
    saveFlowExecutionFilters({
      ...DEFAULT_FLOW_EXECUTION_FILTERS,
      q,
    });
    expect(loadFlowExecutionFilters()?.q.length).to.equal(
      FLOW_EXECUTION_QUERY_MAX
    );
  });

  it('ignores and clears a value that is not a filter object', () => {
    localStorage.setItem(FLOW_EXECUTION_FILTERS_KEY, '"SUCCEEDED"');
    expect(loadFlowExecutionFilters()).to.equal(null);
    expect(localStorage.getItem(FLOW_EXECUTION_FILTERS_KEY)).to.equal(null);

    localStorage.setItem(FLOW_EXECUTION_FILTERS_KEY, 'not-json');
    expect(loadFlowExecutionFilters()).to.equal(null);
    expect(localStorage.getItem(FLOW_EXECUTION_FILTERS_KEY)).to.equal(null);
  });

  it('drops unknown fields and rewrites storage', () => {
    localStorage.setItem(
      FLOW_EXECUTION_FILTERS_KEY,
      JSON.stringify({
        status: 'NOPE',
        flow: 12,
        range: 'forever',
        q: { bad: true },
        extra: true,
      })
    );
    expect(loadFlowExecutionFilters()).to.deep.equal(
      DEFAULT_FLOW_EXECUTION_FILTERS
    );
    expect(localStorage.getItem(FLOW_EXECUTION_FILTERS_KEY)).to.equal(null);
  });

  it('keeps valid fields when a sibling is garbage', () => {
    localStorage.setItem(
      FLOW_EXECUTION_FILTERS_KEY,
      JSON.stringify({ status: 'FAILED', range: 'nope', flow: 'flow-9' })
    );
    expect(loadFlowExecutionFilters()).to.deep.equal({
      status: 'FAILED',
      flow: 'flow-9',
      range: 'month',
      q: '',
    });
    const rewritten = JSON.parse(
      localStorage.getItem(FLOW_EXECUTION_FILTERS_KEY) || '{}'
    );
    expect(rewritten.range).to.equal('month');
    expect(rewritten.status).to.equal('FAILED');
  });

  it('removes the key when the saved set is the default', () => {
    saveFlowExecutionFilters(DEFAULT_FLOW_EXECUTION_FILTERS);
    expect(localStorage.getItem(FLOW_EXECUTION_FILTERS_KEY)).to.equal(null);
    expect(isDefaultFlowExecutionFilters(DEFAULT_FLOW_EXECUTION_FILTERS)).to.be
      .true;
  });

  it('writes and clears storage', () => {
    saveFlowExecutionFilters(stored);
    expect(
      JSON.parse(localStorage.getItem(FLOW_EXECUTION_FILTERS_KEY) || '{}')
    ).to.deep.equal(stored);
    clearFlowExecutionFilters();
    expect(localStorage.getItem(FLOW_EXECUTION_FILTERS_KEY)).to.equal(null);
  });

  it('does not throw when storage is unavailable', () => {
    const getItem = sinon.stub(window.localStorage, 'getItem').throws();
    const setItem = sinon.stub(window.localStorage, 'setItem').throws();
    const removeItem = sinon.stub(window.localStorage, 'removeItem').throws();
    try {
      expect(loadFlowExecutionFilters()).to.equal(null);
      expect(() => saveFlowExecutionFilters(stored)).not.to.throw();
      expect(() => clearFlowExecutionFilters()).not.to.throw();
    } finally {
      getItem.restore();
      setItem.restore();
      removeItem.restore();
    }
  });
});
