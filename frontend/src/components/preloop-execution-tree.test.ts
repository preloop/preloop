import {
  expect,
  fixture,
  fixtureCleanup,
  html,
  waitUntil,
} from '@open-wc/testing';
import sinon from 'sinon';
import './preloop-execution-tree';
import type { PreloopExecutionTree } from './preloop-execution-tree';
import type { ExecutionTree, ExecutionTreeNode } from '../api';

/**
 * The same tree the backend rollup test builds
 * (backend/tests/test_flow_execution_tree.py): one parent at $0.11, three
 * children at $0.05 / $0.02 / $0.00 and a grandchild at $0.01, so both halves
 * of the feature agree on what this tree costs.
 */
const PARENT_COST = 0.11;
const CHILD_COSTS = [0.05, 0.02, 0.0];
const GRANDCHILD_COST = 0.01;
const SUBTREE_COST =
  CHILD_COSTS.reduce((total, cost) => total + cost, 0) + GRANDCHILD_COST;

const node = (
  overrides: Partial<ExecutionTreeNode> & { id: string }
): ExecutionTreeNode => ({
  flow_id: 'flow-child',
  flow_name: 'Delegated flow',
  status: 'SUCCEEDED',
  start_time: '2026-09-15T10:00:00Z',
  end_time: '2026-09-15T10:01:30Z',
  failure_category: null,
  parent_execution_id: 'parent-1',
  delegation_depth: 1,
  estimated_cost: 0,
  total_tokens: 0,
  ...overrides,
});

const delegatingTree = (): ExecutionTree => ({
  execution_id: 'parent-1',
  root_execution_id: 'parent-1',
  execution: node({
    id: 'parent-1',
    flow_id: 'flow-parent',
    flow_name: 'Delegating flow',
    status: 'RUNNING',
    end_time: null,
    parent_execution_id: null,
    delegation_depth: 0,
    estimated_cost: PARENT_COST,
    total_tokens: 4000,
    label: null,
  }),
  executions: [
    node({
      id: 'child-lint',
      label: 'lint the diff',
      estimated_cost: CHILD_COSTS[0],
      total_tokens: 1200,
    }),
    node({
      id: 'child-migrations',
      label: 'run the migrations',
      status: 'FAILED',
      failure_category: 'agent_error',
      estimated_cost: CHILD_COSTS[1],
      total_tokens: 300,
    }),
    node({
      id: 'child-note',
      label: 'draft the note',
      status: 'PENDING',
      end_time: null,
      estimated_cost: CHILD_COSTS[2],
    }),
    node({
      id: 'grandchild-fix',
      label: 'fix what the linter found',
      parent_execution_id: 'child-lint',
      delegation_depth: 2,
      estimated_cost: GRANDCHILD_COST,
      total_tokens: 100,
    }),
  ],
  rollup: {
    total: 4,
    by_status: { SUCCEEDED: 2, FAILED: 1, PENDING: 1 },
    completed: 3,
    total_tokens: 1600,
    total_estimated_cost: SUBTREE_COST,
    total_tool_calls: 10,
  },
  truncated: false,
});

const leafTree = (): ExecutionTree => ({
  execution_id: 'lonely-1',
  root_execution_id: 'lonely-1',
  execution: node({
    id: 'lonely-1',
    flow_id: 'flow-parent',
    flow_name: 'Ordinary flow',
    parent_execution_id: null,
    delegation_depth: 0,
    estimated_cost: 0.03,
  }),
  executions: [],
  rollup: {
    total: 0,
    by_status: {},
    completed: 0,
    total_tokens: 0,
    total_estimated_cost: 0,
    total_tool_calls: 0,
  },
  truncated: false,
});

describe('Execution tree', () => {
  let fetchStub: sinon.SinonStub;
  let answer: ExecutionTree;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-token');
    answer = delegatingTree();
    fetchStub = sinon
      .stub(window, 'fetch')
      .callsFake(async () => new Response(JSON.stringify(answer)));
  });

  afterEach(() => {
    fixtureCleanup();
    sinon.restore();
    localStorage.clear();
  });

  const mount = async (executionId = 'parent-1') => {
    const element = await fixture<PreloopExecutionTree>(
      html`<preloop-execution-tree
        execution-id=${executionId}
      ></preloop-execution-tree>`
    );
    await waitUntil(() => !(element as any).loading);
    await element.updateComplete;
    return element;
  };

  const find = (element: PreloopExecutionTree, testid: string) =>
    element.shadowRoot!.querySelector(`[data-testid="${testid}"]`);

  const rows = (element: PreloopExecutionTree) =>
    Array.from(
      element.shadowRoot!.querySelectorAll('[data-testid="execution-tree-row"]')
    );

  const text = (element: PreloopExecutionTree, testid: string) =>
    (find(element, testid)?.textContent || '').replace(/\s+/g, ' ').trim();

  it('asks the server once, for the execution it was given', async () => {
    await mount();
    // Shoelace fetches its own icons through the same stub; only the tree
    // reads are this component's business.
    const reads = fetchStub
      .getCalls()
      .map((call) => String(call.args[0]))
      .filter((url) => url.endsWith('/tree'));
    expect(reads).to.eql(['/api/v1/flows/executions/parent-1/tree']);
  });

  it('renders nothing for a run with no children', async () => {
    answer = leafTree();
    const element = await mount('lonely-1');

    // Most runs delegate nothing; a line saying so only pushed the error
    // and the tabs further down the execution page.
    expect(find(element, 'execution-tree-empty')).to.not.exist;
    expect(element.shadowRoot!.textContent?.trim()).to.equal('');
    expect(find(element, 'execution-tree')).to.not.exist;
    expect(rows(element)).to.have.length(0);
  });

  it('renders one row per child with flow, label, state, duration and cost', async () => {
    const element = await mount();

    const visible = rows(element);
    expect(visible).to.have.length(3);
    const first = visible[0];
    expect(first.querySelector('a')!.getAttribute('href')).to.equal(
      '/console/flows/executions/child-lint'
    );
    expect(first.textContent).to.contain('Delegated flow');
    expect(first.textContent).to.contain('lint the diff');
    expect(first.querySelector('sl-badge')!.textContent!.trim()).to.equal(
      'Succeeded'
    );
    // 10:00:00 to 10:01:30 of the fixture.
    expect(first.textContent).to.contain('1m 30s');
    expect(first.textContent).to.contain('$0.05');
    expect(visible.map((row) => row.getAttribute('data-execution-id'))).to.eql([
      'child-lint',
      'child-migrations',
      'child-note',
    ]);
  });

  it('shows a grandchild only once its parent row is expanded', async () => {
    const element = await mount();

    const ids = () =>
      rows(element).map((row) => row.getAttribute('data-execution-id'));
    expect(ids()).to.not.contain('grandchild-fix');

    const toggle = element.shadowRoot!.querySelector(
      '[data-testid="execution-tree-toggle"][data-execution-id="child-lint"]'
    ) as HTMLElement;
    expect(toggle, 'only a row with children offers a toggle').to.exist;
    expect(
      element.shadowRoot!.querySelector(
        '[data-testid="execution-tree-toggle"][data-execution-id="child-note"]'
      )
    ).to.not.exist;

    toggle.click();
    await element.updateComplete;
    expect(ids()).to.contain('grandchild-fix');
    const grandchild = rows(element).find(
      (row) => row.getAttribute('data-execution-id') === 'grandchild-fix'
    )!;
    expect(grandchild.getAttribute('data-depth')).to.equal('1');
    expect(grandchild.textContent).to.contain('fix what the linter found');

    toggle.click();
    await element.updateComplete;
    expect(ids()).to.not.contain('grandchild-fix');
  });

  it('totals the subtree cost from the same fixture the backend rollup uses', async () => {
    const element = await mount();

    expect(text(element, 'execution-tree-subtree-cost')).to.equal(
      '$0.08 subtree cost'
    );
    expect(SUBTREE_COST).to.equal(0.08);
    expect(text(element, 'execution-tree-launched')).to.equal('4 launched');
    expect(text(element, 'execution-tree-succeeded')).to.equal('2 succeeded');
    expect(text(element, 'execution-tree-failed')).to.equal('1 failed');
    expect(text(element, 'execution-tree-refused')).to.equal('0 refused');
    expect(text(element, 'execution-tree-subtree-tokens')).to.equal(
      '1.6K tokens'
    );
  });

  it('shows the run own cost apart from the subtree total', async () => {
    const element = await mount();

    expect(text(element, 'execution-tree-own-cost')).to.equal('$0.11 this run');
    expect(text(element, 'execution-tree-subtree-cost')).to.equal(
      '$0.08 subtree cost'
    );
    // The two are never added: $0.19 appears nowhere.
    expect(find(element, 'execution-tree')!.textContent).to.not.contain(
      '$0.19'
    );
  });

  it('shows the failure category of a failed child', async () => {
    const element = await mount();

    const failed = rows(element).find(
      (row) => row.getAttribute('data-status') === 'FAILED'
    )!;
    const chip = failed.querySelector('[data-failure-category]')!;
    expect(chip.getAttribute('data-failure-category')).to.equal('agent_error');
    expect(chip.textContent!.trim().toLowerCase()).to.equal('agent error');
  });

  it('makes a refused child look nothing like a failed one', async () => {
    answer.executions[2] = node({
      id: 'child-refused',
      label: 'delete the branch',
      status: 'REFUSED',
      end_time: null,
      estimated_cost: 0,
    });
    answer.rollup.by_status = { SUCCEEDED: 2, FAILED: 1, REFUSED: 1 };
    const element = await mount();

    const refused = rows(element).find(
      (row) => row.getAttribute('data-execution-id') === 'child-refused'
    )!;
    const failed = rows(element).find(
      (row) => row.getAttribute('data-status') === 'FAILED'
    )!;
    expect(refused.hasAttribute('data-refused')).to.equal(true);
    expect(failed.hasAttribute('data-refused')).to.equal(false);
    expect(refused.classList.contains('refused')).to.equal(true);
    expect(
      refused
        .querySelector('[data-testid="execution-tree-refused-badge"]')!
        .textContent!.trim()
    ).to.equal('Refused');
    expect(failed.querySelector('[data-testid="execution-tree-refused-badge"]'))
      .to.not.exist;
    // Nothing ran, so nothing is charged and no failure is claimed.
    expect(
      refused
        .querySelector('[data-testid="execution-tree-row-cost"]')!
        .textContent!.trim()
    ).to.equal('—');
    expect(refused.querySelector('[data-failure-category]')).to.not.exist;
    expect(text(element, 'execution-tree-refused')).to.equal('1 refused');
    expect(text(element, 'execution-tree-failed')).to.equal('1 failed');
  });

  it('says so when the tree is larger than one read', async () => {
    answer.truncated = true;
    const element = await mount();
    expect(find(element, 'execution-tree-truncated')).to.exist;
  });

  it('renders a lineage that points back at itself without hanging', async () => {
    // Corrupt parent ids are the only way to get here; a browser tab that
    // never paints is a worse answer than a tree that stops.
    answer.executions = [
      node({ id: 'child-a', parent_execution_id: 'parent-1', label: 'a' }),
      node({ id: 'child-b', parent_execution_id: 'child-a', label: 'b' }),
      node({ id: 'child-a-again', parent_execution_id: 'child-b', label: 'a' }),
    ];
    answer.executions[2].id = 'child-a';
    const element = await mount();
    (element as any).expanded = new Set(['child-a', 'child-b']);
    await element.updateComplete;

    expect(rows(element).length).to.be.greaterThan(0);
    expect(rows(element).length).to.be.lessThan(200);
  });

  it('stays quiet and out of the way when the read fails', async () => {
    fetchStub.callsFake(async () => new Response('nope', { status: 500 }));
    const element = await mount();

    expect(find(element, 'execution-tree')).to.not.exist;
    expect(find(element, 'execution-tree-error')).to.exist;
  });
});
