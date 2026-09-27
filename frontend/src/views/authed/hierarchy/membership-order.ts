import type { Membership } from '../../../hierarchy-api';

export interface MembershipGroup {
  root: Membership;
  /** Every descendant the person belongs to, depth first. */
  children: Membership[];
  /** Levels below the root, per child account id (1 for a direct child). */
  depth: ReadonlyMap<string, number>;
}

/**
 * Group memberships by tree (a parent with its subaccounts under it, at any
 * depth) and put the most recently used first. Recency comes from this
 * browser's own switch history, then the server's `last_used_at`. A
 * subaccount whose parent the person is not a member of heads its own group.
 */
export function groupMemberships(
  memberships: readonly Membership[],
  localOrder: readonly string[] = []
): MembershipGroup[] {
  const rank = (m: Membership): number => {
    const local = localOrder.indexOf(m.account_id);
    if (local !== -1) return local;
    const at = m.last_used_at ? Date.parse(m.last_used_at) : NaN;
    // Server recency sorts after local history, newest first.
    return Number.isFinite(at) ? localOrder.length + 1e13 - at : Infinity;
  };
  const byRecency = (a: Membership, b: Membership) =>
    rank(a) - rank(b) || a.account_name.localeCompare(b.account_name);

  const ids = new Set(memberships.map((m) => m.account_id));
  const childrenOf = new Map<string, Membership[]>();
  for (const m of memberships) {
    if (m.parent_account_id && ids.has(m.parent_account_id)) {
      const list = childrenOf.get(m.parent_account_id) ?? [];
      list.push(m);
      childrenOf.set(m.parent_account_id, list);
    }
  }
  const roots = memberships.filter(
    (m) => !m.parent_account_id || !ids.has(m.parent_account_id)
  );
  const groups = roots.map((root) => {
    const children: Membership[] = [];
    const depth = new Map<string, number>();
    const seen = new Set([root.account_id]);
    const walk = (parentId: string, level: number) => {
      for (const child of [...(childrenOf.get(parentId) ?? [])].sort(
        byRecency
      )) {
        if (seen.has(child.account_id)) continue; // a cycle is a server bug
        seen.add(child.account_id);
        children.push(child);
        depth.set(child.account_id, level);
        walk(child.account_id, level + 1);
      }
    };
    walk(root.account_id, 1);
    return { root, children, depth };
  });
  const groupRank = (g: MembershipGroup) =>
    Math.min(rank(g.root), ...g.children.map(rank));
  return groups.sort(
    (a, b) =>
      groupRank(a) - groupRank(b) ||
      a.root.account_name.localeCompare(b.root.account_name)
  );
}
