import { expect } from '@open-wc/testing';
import { attachFullscreen } from './fullscreen-host';

describe('attachFullscreen', () => {
  afterEach(() => {
    document.body.style.overflow = '';
  });

  it('locks body scroll and calls onEscape on Esc only', () => {
    const host = document.createElement('div');
    let calls = 0;
    const dispose = attachFullscreen(host, { onEscape: () => calls++ });
    expect(document.body.style.overflow).to.equal('hidden');
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'a' }));
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    expect(calls).to.equal(1);
    dispose();
    expect(document.body.style.overflow).to.equal('');
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    expect(calls).to.equal(1);
    dispose();
  });
});
