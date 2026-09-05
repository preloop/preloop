import { html, fixture, expect } from '@open-wc/testing';
import sinon from 'sinon';

import './issues-dependencies-view';
import type { IssuesDependenciesView } from './issues-dependencies-view';

const tick = (ms = 150) => new Promise((r) => setTimeout(r, ms));

/** Two projects whose short ids are what the issues area puts in a link. */
const PROJECTS = [
  { id: 'aaaaaaaa-1111-2222-3333-444444444444', name: 'First project' },
  { id: 'bbbbbbbb-5555-6666-7777-888888888888', name: 'Second project' },
];

function stubFetch() {
  return sinon
    .stub(window, 'fetch')
    .callsFake(async (input: RequestInfo | URL) => {
      const url = String(input);
      const json = (data: unknown) =>
        new Response(JSON.stringify(data), { status: 200 });
      if (url.includes('/api/v1/projects')) return json(PROJECTS);
      if (url.includes('/api/v1/search')) return json({ results: [] });
      if (url.includes('/api/v1/issue-dependencies'))
        return json({ dependencies: [] });
      return json({});
    });
}

async function renderAt(search: string): Promise<IssuesDependenciesView> {
  window.history.replaceState({}, '', `/console/issues/dependencies${search}`);
  const element = (await fixture(
    html`<issues-dependencies-view></issues-dependencies-view>`
  )) as IssuesDependenciesView;
  await tick();
  await element.updateComplete;
  return element;
}

describe('IssuesDependenciesView project selection', () => {
  let fetchStub: sinon.SinonStub;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    fetchStub = stubFetch();
  });

  afterEach(() => {
    fetchStub.restore();
    localStorage.clear();
    window.history.replaceState({}, '', window.location.pathname);
  });

  it('selects the project named by ?projects=', async () => {
    // Duplicates, Compliance and the tracker pages all address a project by
    // its short id in `?projects=`. This page read only `?project=<uuid>`, so
    // arriving from a tracker showed the first project in the account and
    // said nothing about the substitution.
    const element = await renderAt('?projects=bbbbbbbb');
    expect((element as any)._selectedProjectId).to.equal(PROJECTS[1].id);
  });

  it('takes the first listed project it recognises', async () => {
    const element = await renderAt('?projects=cccccccc,bbbbbbbb');
    expect((element as any)._selectedProjectId).to.equal(PROJECTS[1].id);
  });

  it('falls back to the first project when no id matches', async () => {
    const element = await renderAt('?projects=cccccccc');
    expect((element as any)._selectedProjectId).to.equal(PROJECTS[0].id);
  });

  it('still honours a full id in ?project=', async () => {
    const element = await renderAt(`?project=${PROJECTS[1].id}`);
    expect((element as any)._selectedProjectId).to.equal(PROJECTS[1].id);
  });

  it('defaults to the first project with no project in the URL', async () => {
    const element = await renderAt('');
    expect((element as any)._selectedProjectId).to.equal(PROJECTS[0].id);
  });
});
