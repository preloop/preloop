import { expect, fixture, html } from '@open-wc/testing';
import { normalizeObservedSession } from './session-observer';
import {
  paneClosesActivity,
  terminalSessionChip,
  terminalLocation,
} from './terminal-location';
import '../components/session-list-panel';

const location = {
  multiplexer: 'herdr',
  device_id: 'device-1',
  workspace_id: 'workspace-1',
  workspace_label: 'Example workspace',
  tab_id: 'tab-1',
  tab_label: 'Development',
  pane_id: 'pane-1',
  terminal_title: 'Agent pane',
  state: 'blocked',
  observed_at: '2026-11-26T12:00:00Z',
};
const session = (overrides: Record<string, unknown> = {}) =>
  normalizeObservedSession({
    id: 'session-1',
    terminal_location: location,
    ...overrides,
  });

describe('terminal session metadata', () => {
  it('fills missing state with a native dialog without inventing an approval', () => {
    expect(terminalSessionChip(session()).label).to.equal(
      'Example workspace / Development · Waiting at a dialog in the terminal'
    );
    expect(terminalSessionChip(session()).variant).to.equal('warning');
  });

  it('keeps authoritative work and approval signals above terminal state', () => {
    for (const [turn, label] of [
      ['working', 'Working'],
      ['waiting_for_approval', 'Needs approval'],
    ]) {
      expect(terminalSessionChip(session({ turn_state: turn })).label).to.equal(
        `Example workspace / Development · ${label}`
      );
    }
  });

  it('uses pending approvals even before a turn hook arrives', () => {
    expect(
      terminalSessionChip(session({ pending_approval_count: 1 })).label
    ).to.contain('Needs approval');
  });

  it('recognizes closed pane state without a timestamp', () => {
    expect(
      session({ terminal_location: { ...location, state: 'closed' } }).status
    ).to.equal('ended');
  });

  it('ends live presence when a pane closes even with a stale active flag', () => {
    const closed = session({
      is_active_now: true,
      activity_status: 'active_now',
      terminal_location: { ...location, pane_closed_at: location.observed_at },
    });
    expect(closed.status).to.equal('ended');
    expect(terminalSessionChip(closed).label).to.contain('Pane closed');
  });

  it('leaves sessions with no adapter unchanged', () => {
    expect(terminalLocation({})).to.equal(null);
    expect(
      terminalSessionChip(session({ terminal_location: null })).label
    ).to.equal('Idle');
  });

  it('does not change baseline chips or activity when no adapter is present', () => {
    const row = session({ terminal_location: null, pending_approval_count: 1 });
    expect(terminalSessionChip(row).label).to.equal('Idle');
    expect(paneClosesActivity(row)).to.equal(false);
  });

  it('ends inferred transcript activity for closed panes but preserves own signals', () => {
    const closed = { terminal_location: { ...location, state: 'closed' } };
    expect(paneClosesActivity(session(closed))).to.equal(true);
    for (const turn_state of ['working', 'waiting_for_approval']) {
      expect(session({ ...closed, turn_state }).status).to.equal('active_now');
      expect(paneClosesActivity(session({ ...closed, turn_state }))).to.equal(
        false
      );
    }
    expect(
      paneClosesActivity(session({ ...closed, pending_approval_count: 1 }))
    ).to.equal(false);
  });

  it('renders untrusted labels as bounded text in one chip', async () => {
    const row = session({
      terminal_location: {
        ...location,
        workspace_label: '<img src=x onerror=alert(1)>',
        tab_label: 'x'.repeat(600),
      },
    });
    const panel = await fixture(
      html`<session-list-panel .sessions=${[row]}></session-list-panel>`
    );
    const chip = panel.shadowRoot!.querySelector('sl-badge')!;
    expect(chip.textContent).to.contain('<img src=x onerror=alert(1)>');
    expect(chip.textContent).not.to.contain('x'.repeat(257));
    expect(chip.querySelector('img')).to.equal(null);
    expect(chip.getAttribute('title')).to.contain('Agent pane');
  });
});
