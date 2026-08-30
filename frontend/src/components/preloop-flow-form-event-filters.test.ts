import { expect } from '@open-wc/testing';

/**
 * Tests for the restored Event Filters (Optional) section in the
 * Create/Edit Flow form component (preloop-flow-form.ts).
 *
 * Uses the same source-fetch pattern as the tracker-unlock tests.
 */

let source: string;

before(async () => {
  const res = await fetch(
    new URL('./preloop-flow-form.ts', import.meta.url).href
  );
  expect(res.ok).to.be.true;
  source = await res.text();
});

describe('PreloopFlowForm event filters section', () => {
  it('declares filtersExpanded state property', () => {
    expect(source).to.include('filtersExpanded');
  });

  it('contains renderEventFilters method', () => {
    expect(source).to.include('renderEventFilters()');
  });

  it('renders Add Filters button when collapsed', () => {
    expect(source).to.include('Add Filters');
  });

  it('renders Hide Filters button when expanded', () => {
    expect(source).to.include('Hide Filters');
  });

  it('renders Event Filters (Optional) heading', () => {
    expect(source).to.include('Event Filters (Optional)');
  });

  it('renders author/Created By filter input', () => {
    expect(source).to.include('Created By (username)');
    expect(source).to.include('trigger_config.author');
  });

  it('renders assignee filter input', () => {
    expect(source).to.include('Assigned To (username)');
    expect(source).to.include('trigger_config.assignee');
  });

  it('renders reviewer filter gated on MR/PR events', () => {
    expect(source).to.include('trigger_config.reviewer');
    expect(source).to.include('Reviewer (username)');
    expect(source).to.include('Requested Reviewer (username)');
  });

  it('renders labels filter as comma-separated input', () => {
    expect(source).to.include('Labels (comma-separated)');
    expect(source).to.include('trigger_config.labels');
  });

  it('renders milestone filter for non-Jira trackers', () => {
    expect(source).to.include('trigger_config.milestone');
  });

  it('renders priority select for Jira trackers', () => {
    expect(source).to.include('trigger_config.priority');
    expect(source).to.include('Any Priority');
  });

  it('renders issue type filter for Jira trackers', () => {
    expect(source).to.include('trigger_config.issue_type');
  });

  it('renders merged and draft checkbox filters for PR/MR events', () => {
    expect(source).to.include('trigger_config.merged');
    expect(source).to.include('trigger_config.draft');
    expect(source).to.include('Only when marked as ready (not draft)');
  });

  it('renders GitLab-specific detailed_merge_status and state filters', () => {
    expect(source).to.include('trigger_config.detailed_merge_status');
    expect(source).to.include('Only when approved');
    expect(source).to.include('Merge Status');
  });

  it('renders GitHub-specific state and mergeable_state filters', () => {
    expect(source).to.include('Pull Request State');
    expect(source).to.include('trigger_config.mergeable_state');
    expect(source).to.include('Mergeable State');
  });

  it('renders filter semantics help alert', () => {
    expect(source).to.include('How filters work:');
    expect(source).to.include('ALL conditions must');
  });

  it('includes trigger_config in the submit payload via conditional spread', () => {
    // The bundler may compile `!== undefined` to `!== void 0`
    const hasTriggerConfigGuard =
      source.includes('trigger_config !== undefined') ||
      source.includes('trigger_config !== void 0');
    expect(hasTriggerConfigGuard).to.be.true;
    expect(source).to.include('trigger_config: this.flow.trigger_config');
  });

  it('calls renderEventFilters gated on trigger_event_source', () => {
    expect(source).to.include(
      'this.flow.trigger_event_source ? this.renderEventFilters()'
    );
  });
});
