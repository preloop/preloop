import { expect } from '@open-wc/testing';
import consoleFonts from './console-fonts.css?inline';
import consoleStyles from './console-styles.css?inline';
import mainCss from '../main.css?inline';

/**
 * D4: Talk tool rows, commands and the status line use JetBrains Mono, and
 * the face is self-hosted (DESIGN Typography: no font CDNs).
 */
describe('console mono face', () => {
  it('declares JetBrains Mono from our own origin only', () => {
    const urls = [...consoleFonts.matchAll(/url\(['"]?([^'")]+)/g)].map(
      (match) => match[1]
    );
    expect(urls).to.have.length(2);
    for (const url of urls) {
      expect(url).to.match(/^\/fonts\/jetbrains-mono\/[\w-]+\.woff2$/);
    }
    expect(consoleFonts).to.contain("font-family: 'JetBrains Mono'");
  });

  it('makes JetBrains Mono the --console-font-mono token everywhere', () => {
    const token = /--console-font-mono:\s*'JetBrains Mono', ui-monospace/;
    expect(consoleFonts).to.match(token);
    expect(consoleStyles).to.match(token);
    expect(mainCss).to.contain('console-fonts.css');
  });
});
