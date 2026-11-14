import { expect } from '@open-wc/testing';
import { render } from 'lit';
import './flow-execution-view';

/**
 * A run with no result, summary or error still shows what was (not) posted
 * on the pull request: the record is the reason the section exists.
 */
describe('flow-execution-view platform review record', () => {
  it('shows the record on a run that reported nothing else', async () => {
    const view = document.createElement('flow-execution-view') as any;
    const container = document.createElement('div');
    document.body.appendChild(container);
    render(
      view.renderOutputPanel({
        id: 'e1',
        flow_id: 'f1',
        status: 'FAILED',
        result: null,
        platform_review: {
          status: 'failed',
          reason: 'statuses_permission_missing',
          review: null,
          commit_status: null,
          attempts: 1,
          retry_scheduled: false,
        },
      }),
      container
    );
    expect(container.querySelector('.panel-empty')).to.equal(null);
    const section = container.querySelector('execution-platform-review');
    expect(section).to.exist;
    container.remove();
  });
});
