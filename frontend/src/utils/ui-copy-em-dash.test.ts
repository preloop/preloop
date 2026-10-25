/**
 * No em dashes in UI copy (DESIGN.md copy rules). Three survived in rendered
 * strings: the Talk step summary, the push-test note on Notifications and
 * the Users edit dialog (U-10). The test reads the modules as the browser
 * receives them, where comments are already stripped and only rendered
 * strings remain.
 */
import { expect } from '@open-wc/testing';

const MODULES = [
  '/src/components/session-chat-view.ts',
  '/src/views/authed/notification-preferences-view.ts',
  '/src/views/authed/settings/user-management-view.ts',
];

/**
 * The module text without block, line and template (HTML) comments, where
 * an em dash is prose for developers rather than copy. Comments are cut by
 * position, not by a pattern replace, so nothing is left half-removed.
 */
function withoutComments(source: string): string {
  const cut = (text: string, open: string, close: string): string => {
    let out = '';
    let index = 0;
    for (;;) {
      const start = text.indexOf(open, index);
      if (start === -1) return out + text.slice(index);
      out += text.slice(index, start);
      const end = text.indexOf(close, start + open.length);
      if (end === -1) return out;
      index = end + close.length;
    }
  };
  return cut(cut(source, '/*', '*/'), '<!--', '-->')
    .split('\n')
    .filter((line) => !line.trim().startsWith('//'))
    .join('\n');
}

describe('UI copy has no em dashes (U-10)', () => {
  for (const path of MODULES) {
    it(`keeps em dashes out of ${path}`, async () => {
      const response = await fetch(path);
      expect(response.ok, path).to.be.true;
      const code = withoutComments(await response.text());
      expect(code).to.not.contain('—');
      expect(code).to.not.contain('&mdash;');
    });
  }
});

describe('one glyph for a missing value (U-11)', () => {
  for (const path of [
    '/src/views/authed/flow-executions-view.ts',
    '/src/views/authed/flow-execution-view.ts',
  ]) {
    it(`uses "-", never an em dash, in ${path}`, async () => {
      const code = withoutComments(await (await fetch(path)).text());
      expect(code).to.not.contain('—');
      expect(code).to.not.contain('\\u2014');
    });
  }
});
