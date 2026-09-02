import { expect, fixture, html } from '@open-wc/testing';

import {
  executionSubjectText,
  executionSubjectUrl,
  isSubjectFallback,
  renderExecutionSubject,
  shortExecutionId,
} from './execution-subject';

describe('execution subjects', () => {
  describe('executionSubjectText', () => {
    it('prefers the subject the server derived from the trigger', () => {
      const text = executionSubjectText({
        id: 'dee1da93-6d1e-4c0e-9f3a-2b1d0c4e5f60',
        trigger_subject: 'spacecode/preloop-ios !17 · Merge Request Updated',
      });

      expect(text).to.equal(
        'spacecode/preloop-ios !17 · Merge Request Updated'
      );
    });

    it('reads the subject the detail endpoint returns inside the snapshot', () => {
      // The list endpoint projects two columns out of the JSONB; the detail
      // endpoint returns the snapshot itself. One renderer, both shapes.
      const exec = {
        id: 'dee1da93',
        trigger_event_details: {
          source: 'gitlab',
          _subject: {
            text: 'spacecode/preloop-ios !17 · Merge Request Updated',
            url: 'https://gitlab.com/spacecode/preloop-ios/-/merge_requests/17',
          },
        },
      };

      expect(executionSubjectText(exec)).to.equal(
        'spacecode/preloop-ios !17 · Merge Request Updated'
      );
      expect(executionSubjectUrl(exec)).to.equal(
        'https://gitlab.com/spacecode/preloop-ios/-/merge_requests/17'
      );
      expect(isSubjectFallback(exec)).to.be.false;
    });

    it('names the person for a run someone started by hand', () => {
      expect(
        executionSubjectText({
          id: 'abc12345-0000',
          trigger_event_details: { triggered_by: 'Jane Doe' },
        })
      ).to.equal('Manual run by Jane Doe');
    });

    it('still says a run was manual when nobody was recorded', () => {
      expect(
        executionSubjectText({
          id: 'abc12345-0000',
          trigger_event_details: { source: 'manual' },
        })
      ).to.equal('Manual run');
    });

    it('labels schedules and webhooks from the trigger snapshot', () => {
      expect(
        executionSubjectText({
          id: 'abc12345',
          trigger_event_details: { source: 'schedule' },
        })
      ).to.equal('Scheduled');
      expect(
        executionSubjectText({
          id: 'abc12345',
          trigger_event_details: { source: 'github' },
        })
      ).to.equal('Webhook');
    });

    it('falls back to a short id when the run predates subjects', () => {
      // Runs created before wave 4 carry neither field. A UUID prefix is a
      // poor label but it is the only thing that tells two rows apart.
      expect(
        executionSubjectText({ id: 'dee1da93-6d1e-4c0e-9f3a-2b1d0c4e5f60' })
      ).to.equal('dee1da93');
      expect(shortExecutionId('dee1da93-6d1e')).to.equal('dee1da93');
      expect(shortExecutionId(null)).to.equal('');
    });

    it('treats a blank subject as no subject', () => {
      const exec = { id: 'abc12345-0000', trigger_subject: '   ' };

      expect(executionSubjectText(exec)).to.equal('abc12345');
      expect(isSubjectFallback(exec)).to.be.true;
    });
  });

  describe('renderExecutionSubject', () => {
    it('links to the thing the subject names, in a new tab', async () => {
      const element = (await fixture(
        html`<div>
          ${renderExecutionSubject({
            id: 'dee1da93',
            trigger_subject: 'spacecode/preloop-ios !17',
            trigger_subject_url:
              'https://gitlab.com/spacecode/preloop-ios/-/merge_requests/17',
          })}
        </div>`
      )) as HTMLElement;

      const link = element.querySelector('a')!;
      expect(link.getAttribute('href')).to.equal(
        'https://gitlab.com/spacecode/preloop-ios/-/merge_requests/17'
      );
      expect(link.getAttribute('target')).to.equal('_blank');
      // Without noopener the opened page gets a handle on the console.
      expect(link.getAttribute('rel')).to.equal('noopener noreferrer');
      expect(link.textContent?.trim()).to.contain('spacecode/preloop-ios !17');
      // The icon says "this leaves the console" without spending words.
      expect(link.querySelector('sl-icon')?.getAttribute('name')).to.equal(
        'box-arrow-up-right'
      );
      expect(link.classList.contains('is-fallback')).to.be.false;
    });

    it('does not let the link click the row underneath it', async () => {
      let rowClicks = 0;
      const element = (await fixture(
        html`<div @click=${() => (rowClicks += 1)}>
          ${renderExecutionSubject({
            id: 'dee1da93',
            trigger_subject: 'spacecode/preloop-ios !17',
            trigger_subject_url: 'https://example.test/mr/17',
          })}
        </div>`
      )) as HTMLElement;

      const link = element.querySelector('a')!;
      link.dispatchEvent(new MouseEvent('click', { bubbles: true }));

      expect(rowClicks).to.equal(0);
    });

    it('renders plain text with no link when there is nothing to open', async () => {
      const element = (await fixture(
        html`<div>
          ${renderExecutionSubject({
            id: 'dee1da93',
            trigger_subject: 'Scheduled · Daily at 09:00 (Europe/Berlin)',
          })}
        </div>`
      )) as HTMLElement;

      expect(element.querySelector('a')).to.equal(null);
      const span = element.querySelector('span.execution-subject')!;
      expect(span.textContent).to.equal(
        'Scheduled · Daily at 09:00 (Europe/Berlin)'
      );
      expect(span.classList.contains('is-fallback')).to.be.false;
    });

    it('marks a fallback so it can be shown in the meta register', async () => {
      const element = (await fixture(
        html`<div>${renderExecutionSubject({ id: 'dee1da93-6d1e' })}</div>`
      )) as HTMLElement;

      const span = element.querySelector('span.execution-subject')!;
      expect(span.classList.contains('is-fallback')).to.be.true;
      expect(span.textContent).to.equal('dee1da93');
    });
  });
});
