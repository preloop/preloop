import { expect } from '@open-wc/testing';

import {
  describeTrackerScope,
  groupProjectsByOrganization,
} from './tracker-scope';
import type { Organization, Project } from '../types';

describe('describeTrackerScope', () => {
  const orgs: Organization[] = [
    {
      id: 'org-1',
      name: 'Platform Team',
      identifier: '3',
      tracker_id: 'tracker-1',
    },
  ];

  const projects: Project[] = [
    {
      id: 'proj-1',
      name: 'API Service',
      identifier: '23',
      organization_id: 'org-1',
    },
    {
      id: 'proj-2',
      name: 'Web App',
      identifier: '22',
      organization_id: 'org-1',
    },
  ];

  it('describes an empty rule set without an em dash', () => {
    const summary = describeTrackerScope(undefined, orgs, []);
    expect(summary).to.equal(
      'No scope configured. Sync to discover groups and projects from your tracker.'
    );
    expect(summary).to.not.include('\u2014');
  });

  it('describes org-wide scope with human-readable group names', () => {
    const summary = describeTrackerScope(
      [
        {
          scope_type: 'ORGANIZATION',
          rule_type: 'INCLUDE',
          identifier: '3',
        },
      ],
      orgs,
      projects
    );

    expect(summary).to.contain('Platform Team');
    expect(summary).to.contain('2 synced so far');
    expect(summary).to.not.contain('ORGANIZATION: 3');
  });

  it('describes selected projects using synced project names', () => {
    const summary = describeTrackerScope(
      [
        {
          scope_type: 'ORGANIZATION',
          rule_type: 'INCLUDE',
          identifier: '3',
        },
        {
          scope_type: 'PROJECT',
          rule_type: 'INCLUDE',
          identifier: '23',
        },
      ],
      orgs,
      projects
    );

    expect(summary).to.contain('API Service');
    expect(summary).to.contain('Platform Team');
  });
});

describe('groupProjectsByOrganization', () => {
  it('groups and sorts projects under their organization', () => {
    const orgs: Organization[] = [
      { id: 'org-b', name: 'Beta', tracker_id: 't1' },
      { id: 'org-a', name: 'Alpha', tracker_id: 't1' },
    ];
    const projects: Project[] = [
      { id: 'p2', name: 'Zeta', organization_id: 'org-a' },
      { id: 'p1', name: 'Alpha App', organization_id: 'org-a' },
      { id: 'p3', name: 'Beta App', organization_id: 'org-b' },
    ];

    const groups = groupProjectsByOrganization(orgs, projects);

    expect(groups.map((g) => g.organization.name)).to.deep.equal([
      'Alpha',
      'Beta',
    ]);
    expect(groups[0].projects.map((p) => p.name)).to.deep.equal([
      'Alpha App',
      'Zeta',
    ]);
  });
});
