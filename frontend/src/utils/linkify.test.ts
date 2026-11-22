import { html, fixture, expect } from '@open-wc/testing';
import { linkify, safeHttpUrl } from './linkify';

async function render(text: string): Promise<HTMLElement> {
  return (await fixture(html`<div>${linkify(text)}</div>`)) as HTMLElement;
}

describe('linkify', () => {
  it('links every http(s) URL and keeps the text around it', async () => {
    const el = await render(
      'LinkedIn: https://www.linkedin.com/in/ada | Lead file: leads/ada.md | HubSpot: http://app.hubspot.com/c/1'
    );
    const links = [...el.querySelectorAll('a')];
    expect(links.map((a) => a.getAttribute('href'))).to.deep.equal([
      'https://www.linkedin.com/in/ada',
      'http://app.hubspot.com/c/1',
    ]);
    for (const a of links) {
      expect(a.getAttribute('target')).to.equal('_blank');
      expect(a.getAttribute('rel')).to.equal('noopener noreferrer');
    }
    expect(el.textContent?.replace(/\s+/g, ' ').trim()).to.equal(
      'LinkedIn: https://www.linkedin.com/in/ada | Lead file: leads/ada.md | HubSpot: http://app.hubspot.com/c/1'
    );
  });

  it('leaves trailing punctuation out of the link', async () => {
    const el = await render('See https://example.com/a. Or (https://example.com/b).');
    const hrefs = [...el.querySelectorAll('a')].map((a) => a.getAttribute('href'));
    expect(hrefs).to.deep.equal([
      'https://example.com/a',
      'https://example.com/b',
    ]);
  });

  it('escapes markup instead of injecting it', async () => {
    const el = await render(
      '<img src=x onerror="alert(1)"><b>bold</b> https://example.com/"><script>x</script>'
    );
    expect(el.querySelector('img')).to.equal(null);
    expect(el.querySelector('b')).to.equal(null);
    expect(el.querySelector('script')).to.equal(null);
    expect(el.textContent).to.contain('<b>bold</b>');
    const a = el.querySelector('a');
    expect(a?.getAttribute('href')).to.equal('https://example.com/');
  });

  it('never links javascript: or data: URLs', async () => {
    const el = await render(
      'javascript:alert(1) JAVASCRIPT:alert(1) data:text/html,<b>x</b> javascript://https://example.com'
    );
    const hrefs = [...el.querySelectorAll('a')].map((a) => a.getAttribute('href'));
    for (const href of hrefs) {
      expect(href).to.match(/^https?:\/\//);
    }
    expect(el.textContent).to.contain('javascript:alert(1)');
    expect(safeHttpUrl('javascript:alert(1)')).to.equal(null);
    expect(safeHttpUrl('data:text/html,x')).to.equal(null);
    expect(safeHttpUrl('https://example.com')).to.equal('https://example.com/');
  });

  it('returns plain text untouched when there is no URL', async () => {
    expect(linkify('no links here')).to.deep.equal(['no links here']);
    expect(linkify('')).to.deep.equal([]);
  });
});
