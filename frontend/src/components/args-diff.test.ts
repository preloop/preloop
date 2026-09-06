/**
 * The diff view for file-edit arguments.
 *
 * An approval for an edit used to print the whole call as JSON, so the
 * decision rested on reading two escaped strings and spotting the difference
 * by eye. These tests pin what the diff shows, that the exact payload stays
 * one click away under Raw, and that calls with no before and after are left
 * exactly as they were.
 */
import { expect, fixture, html } from '@open-wc/testing';
import './args-diff.ts';
import { fileEditsFromArgs, type ArgsDiff } from './args-diff';

async function mount(
  args: Record<string, unknown>,
  raw = JSON.stringify(args, null, 2)
): Promise<ArgsDiff> {
  const el = await fixture<ArgsDiff>(
    html`<args-diff .args=${args} .raw=${raw}></args-diff>`
  );
  await el.updateComplete;
  return el;
}

function lines(el: ArgsDiff, type: 'added' | 'removed' | 'context'): string[] {
  return Array.from(el.shadowRoot!.querySelectorAll(`.line.${type} .text`)).map(
    (node) => node.textContent ?? ''
  );
}

describe('args-diff', () => {
  it('shows what leaves and what arrives for an old_string edit', async () => {
    const el = await mount({
      file_path: '/srv/app/config.py',
      old_string: 'DEBUG = True\nPORT = 8000\n',
      new_string: 'DEBUG = False\nPORT = 8000\n',
    });

    expect(lines(el, 'removed')).to.deep.equal(['DEBUG = True']);
    expect(lines(el, 'added')).to.deep.equal(['DEBUG = False']);
    // The unchanged line stays, so the change is read in its context.
    expect(lines(el, 'context')).to.deep.equal(['PORT = 8000']);
    expect(el.shadowRoot!.querySelector('.path')?.textContent).to.contain(
      '/srv/app/config.py'
    );
    // Colour and a sign carry the state on screen; the word carries it to a
    // screen reader, which would otherwise read "-" as "minus" or skip it.
    expect(
      Array.from(el.shadowRoot!.querySelectorAll('.line .state')).map((node) =>
        node.textContent?.trim()
      )
    ).to.deep.equal(['Removed:', 'Added:']);
  });

  it('keeps the raw arguments one click away', async () => {
    const raw = '{\n  "old_string": "a",\n  "new_string": "b"\n}';
    const el = await mount({ old_string: 'a', new_string: 'b' }, raw);

    expect(el.shadowRoot!.querySelector('[data-testid="args-diff"]')).to.exist;
    expect(el.shadowRoot!.querySelector('[data-testid="args-raw"]')).to.not
      .exist;

    const toggle = el.shadowRoot!.querySelector<HTMLElement>(
      '[data-testid="raw-toggle"]'
    );
    expect(toggle, 'raw toggle').to.exist;
    expect(toggle!.textContent?.trim()).to.equal('Show raw');

    toggle!.click();
    await el.updateComplete;

    const rawBlock = el.shadowRoot!.querySelector('[data-testid="args-raw"]');
    expect(rawBlock, 'raw block').to.exist;
    expect(rawBlock!.textContent).to.contain('"new_string": "b"');
    expect(el.shadowRoot!.querySelector('[data-testid="args-diff"]')).to.not
      .exist;
    // The toggle names where it goes next, so the way back is obvious.
    expect(
      el
        .shadowRoot!.querySelector('[data-testid="raw-toggle"]')!
        .textContent?.trim()
    ).to.equal('Show diff');

    el.shadowRoot!.querySelector<HTMLElement>(
      '[data-testid="raw-toggle"]'
    )!.click();
    await el.updateComplete;
    expect(el.shadowRoot!.querySelector('[data-testid="args-diff"]')).to.exist;
  });

  it('numbers the edits of a multi-edit call', async () => {
    const el = await mount({
      file_path: 'app.ts',
      edits: [
        { old_string: 'one', new_string: 'uno' },
        { old_string: 'two', new_string: 'dos' },
      ],
    });

    expect(lines(el, 'removed')).to.deep.equal(['one', 'two']);
    expect(lines(el, 'added')).to.deep.equal(['uno', 'dos']);
    const labels = Array.from(
      el.shadowRoot!.querySelectorAll('.edit-label')
    ).map((node) => node.textContent?.replace(/\s+/g, ' ').trim());
    expect(labels).to.deep.equal(['Edit 1 of 2', 'Edit 2 of 2']);
  });

  it('reads a whole-file write as a diff against nothing', async () => {
    const el = await mount({ file_path: 'notes.md', content: 'hello\nworld' });

    expect(lines(el, 'added')).to.deep.equal(['hello', 'world']);
    expect(lines(el, 'removed')).to.deep.equal([]);
  });

  it('falls back to the raw arguments when there is no diff to show', async () => {
    const raw = '{\n  "command": "ls -la"\n}';
    const el = await mount({ command: 'ls -la' }, raw);

    expect(el.shadowRoot!.querySelector('[data-testid="args-diff"]')).to.not
      .exist;
    expect(
      el.shadowRoot!.querySelector('[data-testid="args-raw"]')!.textContent
    ).to.contain('ls -la');
    // Nothing to toggle between, so no toggle.
    expect(el.shadowRoot!.querySelector('[data-testid="raw-toggle"]')).to.not
      .exist;
  });

  it('reads the shapes agents actually send, and no others', () => {
    expect(
      fileEditsFromArgs({ old_string: 'a', new_string: 'b' })
    ).to.have.lengthOf(1);
    // MCP filesystem edits use camelCase for the same two fields.
    expect(
      fileEditsFromArgs({ edits: [{ oldText: 'a', newText: 'b' }] })
    ).to.have.lengthOf(1);
    // An edit that changes nothing is not a change.
    expect(
      fileEditsFromArgs({ old_string: 'a', new_string: 'a' })
    ).to.deep.equal([]);
    // Content without a file path could be any string argument at all.
    expect(fileEditsFromArgs({ content: 'hello' })).to.deep.equal([]);
    expect(fileEditsFromArgs(null)).to.deep.equal([]);
  });
});
