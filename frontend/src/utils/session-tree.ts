/** Shared, cycle-safe ordering for session lists and the Talk picker. */
export interface SessionTreeItem {
  id: string;
  parentSessionId?: string | null;
  status: string;
}

export interface SessionTreeRow<T> {
  session: T;
  depth: number;
  children: number;
  runningChildren: number;
  expanded: boolean;
}

export function sessionTree<T extends SessionTreeItem>(
  sessions: T[],
  expanded: ReadonlySet<string>,
  reveal: ReadonlySet<string> = new Set()
): SessionTreeRow<T>[] {
  const byId = new Map(sessions.map((session) => [session.id, session]));
  const children = new Map<string, T[]>();
  for (const session of byId.values()) {
    if (session.parentSessionId && session.parentSessionId !== session.id) {
      const group = children.get(session.parentSessionId) ?? [];
      group.push(session);
      children.set(session.parentSessionId, group);
    }
  }
  const open = new Set(expanded);
  for (const id of reveal) {
    let parent = byId.get(id)?.parentSessionId;
    const visited = new Set<string>();
    while (parent && !visited.has(parent)) {
      visited.add(parent);
      open.add(parent);
      parent = byId.get(parent)?.parentSessionId;
    }
  }
  const rows: SessionTreeRow<T>[] = [];
  const visited = new Set<string>();
  const visit = (session: T, depth: number): void => {
    if (visited.has(session.id)) return;
    visited.add(session.id);
    const descendants = children.get(session.id) ?? [];
    rows.push({
      session,
      depth,
      children: descendants.length,
      runningChildren: descendants.filter(
        (child) => child.status === 'active_now' || child.status === 'working'
      ).length,
      expanded: open.has(session.id),
    });
    if (open.has(session.id))
      descendants.forEach((child) => visit(child, depth + 1));
    else {
      const hide = (child: T): void => {
        if (visited.has(child.id)) return;
        visited.add(child.id);
        (children.get(child.id) ?? []).forEach(hide);
      };
      descendants.forEach(hide);
    }
  };
  for (const session of byId.values()) {
    if (!session.parentSessionId || !byId.has(session.parentSessionId))
      visit(session, 0);
  }
  // Broken lineage cannot hide a session forever.
  for (const session of byId.values())
    if (!visited.has(session.id)) visit(session, 0);
  return rows;
}
