import { expect } from '@open-wc/testing';
import sinon from 'sinon';

import {
  DEFAULT_LIST_VIEW,
  LIST_TO_CARDS_BREAKPOINT,
  effectiveViewMode,
  isListViewMode,
  loadViewMode,
  saveViewMode,
  subscribeNarrowViewport,
} from './view-mode';

describe('view-mode', () => {
  const key = 'preloop.test.view_mode';

  afterEach(() => {
    localStorage.removeItem(key);
  });

  it('treats list and cards as valid modes', () => {
    expect(isListViewMode('list')).to.equal(true);
    expect(isListViewMode('cards')).to.equal(true);
    expect(isListViewMode('canvas')).to.equal(false);
    expect(isListViewMode(null)).to.equal(false);
  });

  it('loads a stored view when it is in the allowed set', () => {
    localStorage.setItem(key, 'cards');
    expect(loadViewMode(key)).to.equal('cards');
  });

  it('falls back to list when nothing is stored', () => {
    expect(loadViewMode(key)).to.equal(DEFAULT_LIST_VIEW);
    expect(loadViewMode(key)).to.equal('list');
  });

  it('falls back when the stored value is not allowed', () => {
    localStorage.setItem(key, 'carousel');
    expect(loadViewMode(key)).to.equal('list');
  });

  it('honours a page-specific allowed set and fallback', () => {
    localStorage.setItem(key, 'cards');
    expect(loadViewMode(key, ['list'], 'list')).to.equal('list');
    expect(loadViewMode(key, ['list', 'cards'], 'list')).to.equal('cards');
  });

  it('falls back when localStorage throws', () => {
    const stub = sinon.stub(window.localStorage, 'getItem').throws();
    try {
      expect(loadViewMode(key)).to.equal('list');
    } finally {
      stub.restore();
    }
  });

  it('saves the view for the next visit', () => {
    saveViewMode(key, 'cards');
    expect(localStorage.getItem(key)).to.equal('cards');
  });

  it('swallows save failures', () => {
    const stub = sinon.stub(window.localStorage, 'setItem').throws();
    try {
      expect(() => saveViewMode(key, 'cards')).not.to.throw();
    } finally {
      stub.restore();
    }
  });

  it('paints cards on a narrow viewport without changing the stored choice', () => {
    expect(effectiveViewMode('list', true)).to.equal('cards');
    expect(effectiveViewMode('list', false)).to.equal('list');
    expect(effectiveViewMode('cards', true)).to.equal('cards');
    expect(effectiveViewMode('cards', false)).to.equal('cards');
  });

  it('subscribes to the Flows list-to-cards breakpoint', () => {
    const listeners: Array<(event: MediaQueryListEvent) => void> = [];
    const matchMedia = sinon.stub(window, 'matchMedia').callsFake(
      (query: string) =>
        ({
          matches: query === LIST_TO_CARDS_BREAKPOINT,
          media: query,
          addEventListener: (
            _type: string,
            handler: (event: MediaQueryListEvent) => void
          ) => {
            listeners.push(handler);
          },
          removeEventListener: (
            _type: string,
            handler: (event: MediaQueryListEvent) => void
          ) => {
            const index = listeners.indexOf(handler);
            if (index >= 0) listeners.splice(index, 1);
          },
          addListener: () => undefined,
          removeListener: () => undefined,
          onchange: null,
          dispatchEvent: () => false,
        }) as unknown as MediaQueryList
    );

    try {
      const seen: boolean[] = [];
      const subscription = subscribeNarrowViewport((narrow) => {
        seen.push(narrow);
      });
      expect(subscription.matches).to.equal(true);
      expect(matchMedia).to.have.been.calledWith(LIST_TO_CARDS_BREAKPOINT);
      listeners[0]?.({ matches: false } as MediaQueryListEvent);
      expect(seen).to.deep.equal([false]);
      subscription.disconnect();
      expect(listeners).to.deep.equal([]);
    } finally {
      matchMedia.restore();
    }
  });
});
