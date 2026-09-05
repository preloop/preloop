import type { Organization, Project } from '../types';

export interface TrackerScopeRule {
  scope_type: string;
  rule_type: string;
  identifier: string;
}

function resolveOrgLabel(
  identifier: string,
  organizations: Organization[]
): string {
  const org = organizations.find(
    (item) =>
      item.id === identifier ||
      item.identifier === identifier ||
      item.key === identifier
  );
  return org?.name || identifier;
}

function resolveProjectLabel(identifier: string, projects: Project[]): string {
  const project = projects.find(
    (item) =>
      item.id === identifier ||
      item.identifier === identifier ||
      item.key === identifier
  );
  return project?.name || identifier;
}

export function describeTrackerScope(
  rules: TrackerScopeRule[] | undefined,
  organizations: Organization[],
  projects: Project[]
): string {
  const synced = projects.length;

  if (!rules?.length) {
    return synced > 0
      ? `${synced} project${synced === 1 ? '' : 's'} synced from this tracker.`
      : 'No scope configured. Sync to discover groups and projects from your tracker.';
  }

  const orgIncludes = rules.filter(
    (rule) => rule.scope_type === 'ORGANIZATION' && rule.rule_type === 'INCLUDE'
  );
  const projectIncludes = rules.filter(
    (rule) => rule.scope_type === 'PROJECT' && rule.rule_type === 'INCLUDE'
  );
  const projectExcludes = rules.filter(
    (rule) => rule.scope_type === 'PROJECT' && rule.rule_type === 'EXCLUDE'
  );

  if (projectIncludes.length > 0) {
    const orgNames = orgIncludes.map((rule) =>
      resolveOrgLabel(rule.identifier, organizations)
    );
    const projectNames = projectIncludes.map((rule) =>
      resolveProjectLabel(rule.identifier, projects)
    );
    const orgPart = orgNames.length > 0 ? ` in ${orgNames.join(', ')}` : '';
    return `Scanning ${projectNames.length} selected project${
      projectNames.length === 1 ? '' : 's'
    }${orgPart} (${projectNames.slice(0, 3).join(', ')}${
      projectNames.length > 3 ? ', …' : ''
    }). ${synced} synced so far.`;
  }

  if (orgIncludes.length > 0) {
    const orgNames = orgIncludes.map((rule) =>
      resolveOrgLabel(rule.identifier, organizations)
    );
    if (projectExcludes.length > 0) {
      const excluded = projectExcludes.map((rule) =>
        resolveProjectLabel(rule.identifier, projects)
      );
      return `Scanning all projects in ${orgNames.join(', ')}, except ${excluded.length} excluded (${excluded.slice(0, 2).join(', ')}${
        excluded.length > 2 ? ', …' : ''
      }). ${synced} synced so far.`;
    }
    return `Scanning all projects in ${orgNames.join(', ')}. ${synced} synced so far.`;
  }

  return synced > 0
    ? `${synced} project${synced === 1 ? '' : 's'} synced.`
    : 'Sync to populate projects from your configured scope.';
}

export function groupProjectsByOrganization(
  organizations: Organization[],
  projects: Project[]
): Array<{ organization: Organization; projects: Project[] }> {
  const orgMap = new Map(organizations.map((org) => [org.id, org]));
  const grouped = new Map<string, Project[]>();

  for (const project of projects) {
    const existing = grouped.get(project.organization_id) || [];
    existing.push(project);
    grouped.set(project.organization_id, existing);
  }

  const groups: Array<{ organization: Organization; projects: Project[] }> = [];
  for (const [orgId, orgProjects] of grouped.entries()) {
    const organization = orgMap.get(orgId);
    if (!organization) continue;
    groups.push({
      organization,
      projects: orgProjects.sort((a, b) =>
        (a.name || '').localeCompare(b.name || '')
      ),
    });
  }

  return groups.sort((a, b) =>
    a.organization.name.localeCompare(b.organization.name)
  );
}
