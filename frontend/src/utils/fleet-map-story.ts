import { FLOW_ICON_SRC } from './fleet-map-icons';
import type {
  FleetCodeActivity,
  FleetMapEdge,
  FleetMapGraph,
  FleetMapNode,
} from '../fleet-map-api';
import {
  object,
  safeEventText as text,
  type FleetMessage,
} from './fleet-map-motion';
import { parseUTCDate } from './date';

type Work = { node: FleetMapNode; at: number; expires?: number };
type Run = {
  flow: string;
  name: string;
  status: string;
  at: number;
  end?: number;
  start?: number;
  subject?: string;
};
/** A lit association and what its line says while lit ('' says nothing). */
type Link = { edge: FleetMapEdge; until: number; label: string };
const TERMINAL = new Set([
  'SUCCEEDED',
  'COMPLETED',
  'FAILED',
  'CANCELLED',
  'CANCELED',
  'STOPPED',
  'EXPIRED',
]);

/**
 * What the line from an actor to a work item says while lit: the action
 * that created it, by the event's own fields.
 */
export function codeLineLabel(
  action: string,
  ref: string,
  payload: Record<string, unknown>,
  node: Pick<FleetMapNode, 'work_kind' | 'review_state'>
): string {
  const kind = node.work_kind === 'issue' ? 'issue' : 'PR';
  const on = ref ? ` ${kind} ${ref}` : '';
  const labels = (value: unknown) =>
    Array.isArray(value)
      ? value
          .slice(0, 3)
          .map((item) => text(item, 40))
          .filter(Boolean)
          .join(', ')
      : '';
  switch (action) {
    case 'issue_opened':
    case 'opened':
      return `opened${on}`;
    case 'issue_labeled':
      return labels(payload.labels_added)
        ? `labelled ${labels(payload.labels_added)}`
        : 'labelled';
    case 'issue_unlabeled':
      return labels(payload.labels_removed)
        ? `unlabelled ${labels(payload.labels_removed)}`
        : 'unlabelled';
    case 'issue_updated':
    case 'pull_request_updated':
      return `updated${on}`;
    case 'reviewed': {
      const state = text(payload.review_state).replace(/_/g, ' ');
      return state ? `review: ${state}` : 'reviewed';
    }
    case 'commented':
      return `commented on${on}`;
    case 'merged':
      return `merged${on}`;
    default:
      return '';
  }
}

/** Labels after an update: removals first, then additions, at most six. */
export function nextLabels(
  current: string[] | undefined,
  added: unknown,
  removed: unknown
): string[] {
  const list = (value: unknown) =>
    Array.isArray(value)
      ? value.map((item) => text(item, 40)).filter(Boolean)
      : [];
  const gone = new Set(list(removed));
  const next = (current ?? []).filter((label) => !gone.has(label));
  for (const label of list(added)) if (!next.includes(label)) next.push(label);
  return next.slice(0, 6);
}

/** Only navigable object URLs, with credentials and tracking/query data removed. */
export function workUrl(value: unknown): string {
  try {
    const url = new URL(text(value, 2000));
    if (!['https:', 'http:'].includes(url.protocol)) return '';
    url.username = '';
    url.password = '';
    url.search = '';
    url.hash = '';
    return url.href;
  } catch {
    return '';
  }
}

const referenceFromUrl = (url: string): string => {
  const match = /\/(?:pull|pulls|merge_requests)\/(\d+)(?:\/|$)/.exec(url);
  return match
    ? `${url.includes('/merge_requests/') ? '!' : '#'}${match[1]}`
    : '';
};

/** A bounded view of observed work and runs, never a reconstructed history. */
export class FleetStory {
  private works = new Map<string, Work>();
  private runs = new Map<string, Run>();
  private links = new Map<string, Link>();
  private seen = new Set<string>();

  private subject(raw: unknown, kindHint: string, now: number): string {
    const s = object(raw);
    const kind =
      s.kind === 'pull_request' || /pull_request|merge_request/.test(kindHint)
        ? 'pull_request'
        : s.kind === 'issue' || /issue/.test(kindHint)
          ? 'issue'
          : null;
    const url = workUrl(s.url);
    const ref = text(s.reference, 160);
    const repo = text(s.repo, 200);
    const key =
      text(s.key, 360) ||
      (repo && ref ? `${repo}${ref}` : '') ||
      url ||
      text(s.issue_id, 80);
    // A number without a repository/URL is ambiguous across trackers.
    if (!kind || !key) return '';
    const id = `work:${kind}:${key}`;
    // Prefer an existing identity for the same canonical URL or object key.
    const known = [...this.works].find(
      ([, w]) =>
        w.node.work_kind === kind &&
        ((url && w.node.href === url) || w.node.resource_id === key)
    );
    const identity = known?.[0] ?? id;
    const previous = this.works.get(identity);
    if (previous && previous.at > now) return identity;
    const label = `${kind === 'pull_request' ? 'PR ' : ''}${ref || text(s.key, 80) || (kind === 'issue' ? 'Issue' : 'PR')}${text(s.title, 160) ? `  ${text(s.title, 160)}` : ''}`;
    this.works.set(identity, {
      ...previous,
      at: now,
      node: {
        id: identity,
        type: 'work',
        label,
        status: 'Open',
        owner_ids: [],
        provider_ids: [],
        gaps: [],
        finding_count: 0,
        actions: [],
        ...previous?.node,
        resource_id: key,
        detail: repo || previous?.node.detail || null,
        href: url || previous?.node.href || '',
        ...(text(s.title) || !previous ? { label } : {}),
        work_kind: kind,
        work_reference: ref || previous?.node.work_reference,
        work_title: text(s.title, 160) || previous?.node.work_title,
        last_seen_at: new Date(now).toISOString(),
      },
    });
    return identity;
  }

  private link(source: string, target: string, now: number, label = ''): void {
    if (!source || !target || source === target) return;
    const id = `story:${source}>${target}`;
    this.links.set(id, {
      until: now + 6000,
      label,
      edge: {
        id,
        source,
        target,
        kind: 'caused',
        provenance: 'observed',
        impact_source: source,
        impact_target: target,
        gaps: [],
      },
    });
  }

  record(topic: string, wire: unknown, now = Date.now()): FleetMessage[] {
    const r = object(wire),
      p = object(r.payload);
    const type = text(r.type);
    if (!(
      (topic === 'flow_executions' &&
        ['execution_started', 'status_update'].includes(type)) ||
      (topic === 'code_activity' && type === 'code_activity')
    ))
      return [];
    const stableId = text(p.event_id || r.event_id || p.correlation_id);
    const atText = text(p.timestamp || r.timestamp);
    const parsed = atText ? parseUTCDate(atText).getTime() : now;
    const at = Number.isFinite(parsed) ? Math.min(now, parsed) : now;
    const dedup =
      stableId ||
      (topic === 'flow_executions'
        ? `${text(r.execution_id)}:${text(p.status)}:${atText}`
        : '');
    if (dedup && this.seen.has(dedup)) return [];
    if (dedup) this.seen.add(dedup);
    let result: FleetMessage[] = [];
    if (topic === 'flow_executions') {
      const flowId = text(r.flow_id || p.flow_id),
        execution = text(r.execution_id || p.execution_id);
      const context = object(p.fleet_context);
      if (!flowId || !execution || !text(p.status)) return [];
      const flow = `flow:${flowId}`;
      const previous = this.runs.get(execution);
      // A delayed RUNNING update cannot resurrect a completed execution.
      if (
        previous &&
        (at < previous.at ||
          (previous.end && !TERMINAL.has(text(p.status).toUpperCase())))
      )
        return [];
      const status = text(p.status).toUpperCase();
      const name =
        text(context.flow_name || p.flow_name, 160) || previous?.name || 'Flow';
      const startText = text(p.start_time);
      const start = startText
        ? parseUTCDate(startText).getTime()
        : (previous?.start ?? at);
      this.runs.set(execution, {
        flow,
        name,
        status,
        at,
        start,
        ...(TERMINAL.has(status) ? { end: previous?.end ?? at } : {}),
      });
      const triggerKind = text(context.trigger_kind || p.trigger_kind);
      let subject =
        this.subject(context.subject ?? p.trigger_ref, triggerKind, at) ||
        previous?.subject ||
        '';
      const resumeUrl = workUrl(context.resume_pr_url);
      if (resumeUrl)
        subject = this.subject(
          {
            kind: 'pull_request',
            url: resumeUrl,
            reference: referenceFromUrl(resumeUrl),
          },
          'pull_request',
          at
        );
      this.runs.get(execution)!.subject = subject;
      // The work item started this run: say how, by the trigger's own kind.
      this.link(subject, flow, now, triggerKind.replace(/_/g, ' '));
      const parent = this.runs.get(
        text(
          context.parent_execution_id ||
            context.resume_from_execution_id ||
            context.retry_of_execution_id
        )
      );
      if (parent) this.link(parent.flow, flow, now);
      const node = this.works.get(subject)?.node;
      const elapsed = Number.isFinite(start)
        ? Math.max(0, Math.round((at - start) / 1000))
        : 0;
      const duration =
        elapsed >= 60
          ? `${Math.floor(elapsed / 60)}m ${elapsed % 60}s`
          : `${elapsed}s`;
      const state = TERMINAL.has(status)
        ? status === 'COMPLETED'
          ? 'succeeded'
          : status.toLowerCase()
        : previous
          ? 'running'
          : 'started';
      const content = TERMINAL.has(status) ? duration : (node?.label ?? '');
      result = [
        {
          from: flow,
          to: subject || flow,
          actor: flow,
          kind: 'code',
          icon: FLOW_ICON_SRC,
          label: `${name} ${state}${content ? `  ${content}` : ''}`,
        },
      ];
      // The controlled publication path already emits the bound PR URL.
      // Select only that reference, never the result body or output summary.
      const output = object(p.result);
      const prUrl = workUrl(output.pr_url);
      if (prUrl && TERMINAL.has(status)) {
        const prId = this.subject(
          {
            kind: 'pull_request',
            url: prUrl,
            reference: referenceFromUrl(prUrl),
          },
          'pull_request',
          at
        );
        const pr = this.works.get(prId)!;
        if (node?.work_kind === 'issue') {
          pr.node.parent_id = subject;
          node.status = 'PR open';
          this.link(subject, prId, now);
        }
        const prRef = referenceFromUrl(prUrl);
        this.link(flow, prId, now, prRef ? `opened PR ${prRef}` : 'opened PR');
        result.push({
          from: flow,
          to: prId,
          actor: flow,
          kind: 'code',
          icon: 'journal-code',
          label: `Published ${pr.node.label}`,
        });
      }
      const reported = (
        {
          approve: 'approved',
          request_changes: 'changes requested',
          comment: 'commented',
        } as Record<string, string>
      )[text(output.review_action)];
      if (output.review_posted === true && reported && TERMINAL.has(status)) {
        const reviewTarget = this.works.get(subject);
        if (reviewTarget?.node.work_kind === 'pull_request') {
          // This is the agent's structured report, not a forge confirmation.
          if (
            !reviewTarget.node.review_state ||
            reviewTarget.node.review_state.startsWith('Reported')
          )
            reviewTarget.node.review_state = `Reported ${reported}`;
          // The flow's own structured report, not a forge review state.
          this.link(flow, subject, now, `reported review: ${reported}`);
          result.push({
            from: flow,
            to: subject,
            actor: flow,
            kind: 'code',
            icon: 'eye',
            label: `Reported review ${referenceFromUrl(reviewTarget.node.href) || reviewTarget.node.label}  ${reported}`,
          });
        }
      }
    } else {
      const action = text(p.action);
      const allowed = [
        'issue_opened',
        'issue_updated',
        'issue_labeled',
        'issue_unlabeled',
        'pull_request_updated',
        'opened',
        'reviewed',
        'commented',
        'merged',
      ];
      if (!allowed.includes(action)) return [];
      const raw = object(p.subject);
      const kind =
        p.kind === 'issue' || action.startsWith('issue_')
          ? 'issue'
          : 'pull_request';
      const workKey = text(p.work_id).replace(
        /^work:(?:issue|pull_request):/,
        ''
      );
      const fallback =
        !Object.keys(raw).length && (workKey || text(p.pr) || workUrl(p.url))
          ? {
              kind,
              key: workKey || text(p.pr) || workUrl(p.url),
              reference: (workKey || text(p.pr)).match(/[#!]\d+$/)?.[0],
              title: p.title,
              url: p.url,
            }
          : raw;
      const subject = this.subject(fallback, kind, at);
      const issue = this.subject(p.related_issue, 'issue', at);
      const work = this.works.get(subject);
      if (work && work.at > at) return [];

      const flow = text(p.flow_id || r.flow_id),
        agent = text(p.managed_agent_id),
        user = text(p.actor_user_id);
      const actor = user
        ? `user:${user}`
        : flow
          ? `flow:${flow}`
          : agent
            ? `agent:${agent}`
            : '';
      if (work) {
        if (issue && issue !== subject) {
          work.node.parent_id = issue;
          const parent = this.works.get(issue);
          if (parent)
            parent.node.status = action === 'merged' ? 'Merged' : 'PR open';
          this.link(issue, subject, now);
        }
        const review = text(p.review_state);
        if (
          ['commented', 'changes_requested', 'approved', 'dismissed'].includes(
            review
          )
        )
          work.node.review_state = review;
        if (action === 'merged') {
          work.node.status = 'Merged';
          work.expires = at + 120_000;
          const parent = this.works.get(work.node.parent_id ?? '');
          if (parent) {
            parent.expires = work.expires;
            parent.node.status = 'Merged';
          }
        } else if (action === 'issue_labeled' || action === 'issue_updated')
          work.node.status = 'Updated';
        if (action === 'issue_labeled' || action === 'issue_unlabeled')
          work.node.labels = nextLabels(
            work.node.labels,
            p.labels_added,
            p.labels_removed
          );
        const ref =
          text(object(fallback).reference) ||
          referenceFromUrl(work.node.href) ||
          '';
        this.link(
          actor,
          subject,
          now,
          codeLineLabel(action, ref, p, work.node)
        );
        // Merge contributors are association evidence, never a human merger.
        if (Array.isArray(p.flow_ids))
          for (const id of p.flow_ids.slice(0, 5))
            if (text(id))
              this.link(
                `flow:${text(id)}`,
                subject,
                now,
                ref ? `contributed to PR ${ref}` : 'contributed'
              );
      }
      const verb: Record<string, string> = {
        issue_opened: 'Opened',
        issue_updated: 'Updated',
        issue_labeled: 'Labelled',
        issue_unlabeled: 'Unlabelled',
        pull_request_updated: 'Updated',
        opened: 'Opened',
        reviewed: 'Reviewed',
        commented: 'Commented on',
        merged: 'Merged',
      };
      const ref =
        text(object(fallback).reference) ||
        text(p.pr) ||
        (kind === 'issue' ? 'issue' : 'PR');
      const labels = Array.isArray(p.labels_added)
        ? p.labels_added
            .slice(0, 12)
            .map((x) => text(x, 80))
            .filter(Boolean)
            .join(', ')
        : '';
      const detail =
        action === 'reviewed'
          ? text(work?.node.review_state).replace(/_/g, ' ')
          : labels || text(object(fallback).title, 160);
      const label = `${verb[action]} ${ref}${detail ? `  ${detail}` : ''}`;
      if (actor || subject)
        result = [
          {
            from: actor || subject,
            to: subject || actor,
            actor: actor || subject,
            kind: 'code',
            icon: action === 'merged' ? 'check2-circle' : 'journal-code',
            label,
          },
        ];
    }
    this.trim();
    return result;
  }

  private trim(): void {
    while (this.works.size > 200)
      this.works.delete(this.works.keys().next().value!);
    while (this.runs.size > 400)
      this.runs.delete(this.runs.keys().next().value!);
    while (this.links.size > 800)
      this.links.delete(this.links.keys().next().value!);
    while (this.seen.size > 4000)
      this.seen.delete(this.seen.values().next().value!);
  }

  graph(
    base: FleetMapGraph,
    minutes: number | null,
    now = Date.now()
  ): FleetMapGraph {
    const since = minutes === null ? 0 : now - minutes * 60_000;
    const nodes = new Map(base.nodes.map((n) => [n.id, n]));
    for (const [id, w] of this.works) {
      if (w.at < since || (w.expires && now >= w.expires)) continue;
      nodes.set(id, w.node);
    }
    const flows = new Map<string, Run[]>();
    for (const run of this.runs.values()) {
      if (run.at < since || (run.end && now - run.end >= 60_000)) continue;
      flows.set(run.flow, [...(flows.get(run.flow) ?? []), run]);
    }
    // Live-run flows fade after their last run, unless the window's history
    // (the base graph, from code activity) still lists them.
    const listed = new Set(base.nodes.map((n) => n.id));
    const knownFlows = new Set([...this.runs.values()].map((r) => r.flow));
    for (const id of knownFlows)
      if (!flows.has(id) && !listed.has(id)) nodes.delete(id);
    for (const [id, runs] of flows) {
      const active = runs.filter((r) => !r.end).length;
      const latest = runs.reduce((a, b) => (a.at > b.at ? a : b));
      const status =
        active > 1 ? `${active} running` : active ? 'RUNNING' : latest.status;
      nodes.set(id, {
        id,
        type: 'flow',
        resource_id: id.slice(5),
        label: latest.name,
        href: `/console/flows/${encodeURIComponent(id.slice(5))}`,
        detail: 'Flow',
        owner_ids: [],
        provider_ids: [],
        gaps: [],
        finding_count: 0,
        actions: [],
        ...nodes.get(id),
        ...(nodes.has(id) ? {} : { label: latest.name }),
        status,
        last_seen_at: new Date(latest.at).toISOString(),
      });
    }
    const edges = [
      ...base.edges,
      ...[...this.links.values()].map((l) => l.edge),
    ].filter((e) => nodes.has(e.source) && nodes.has(e.target));
    return { ...base, nodes: [...nodes.values()], edges };
  }

  flowForExecution(id: string): string | undefined {
    return this.runs.get(id)?.flow.slice(5);
  }

  activeEdges(now = Date.now()): Set<string> {
    return new Set([...this.labels(now).keys()]);
  }

  /** Lit associations and what each line says while lit. */
  labels(now = Date.now()): Map<string, string> {
    return new Map(
      [...this.links]
        .filter(([, l]) => l.until > now)
        .map(([id, l]) => [id, l.label])
    );
  }

  /** Bumped by clear(), so session-scoped caches can follow a reset. */
  epoch = 0;

  /**
   * Seed the work items the window's history reports, so a freshly opened
   * map shows every issue and PR active in the window, not only those that
   * move while it is open. History is not traffic: no lit lines, no notes.
   * Newer live state is never overwritten. True when anything changed.
   */
  seedWork(activity: FleetCodeActivity | null | undefined): boolean {
    if (!activity) return false;
    const items = (activity.work ?? []).slice(0, 200).map((item) => ({
      ...item,
      at: parseUTCDate(item.last_at).getTime(),
    }));
    // Until the server lists work items, forge merges name their PR; a PR
    // the list already has is not added twice.
    const listed = new Set(
      items.flatMap((item) => [item.key, item.url ?? ''].filter(Boolean))
    );
    // A short "#51" matches any listed "repo#51"; a full key must match.
    const same = (pr: string) =>
      listed.has(pr) ||
      (!pr.includes('/') && [...listed].some((key) => key.endsWith(pr)));
    for (const event of activity.events ?? [])
      if (
        event.action === 'merged' &&
        (event.pr || event.url) &&
        !(event.url && listed.has(event.url)) &&
        !(event.pr && same(event.pr))
      )
        items.push({
          key: event.pr || event.url || '',
          kind: 'pull_request',
          reference: (event.pr ?? '').match(/[#!]\d+$/)?.[0] ?? '',
          repo: null,
          title: null,
          url: event.url ?? null,
          state: 'merged',
          last_action: 'merged',
          last_at: event.at,
          review_state: null,
          labels: [],
          related_issue: null,
          node_ids: [event.node_id],
          at: parseUTCDate(event.at).getTime(),
        });
    let changed = false;
    const ids = new Map<string, string>();
    // Issues first, so pull requests can attach under them.
    const ordered = [...items].sort(
      (a, b) => Number(a.kind !== 'issue') - Number(b.kind !== 'issue')
    );
    for (const item of ordered) {
      if (!Number.isFinite(item.at) || !item.key) continue;
      const id = this.subject(
        {
          kind: item.kind,
          key: item.key,
          reference: item.reference,
          repo: item.repo,
          title: item.title,
          url: item.href ?? item.url,
        },
        item.kind,
        item.at
      );
      const work = this.works.get(id);
      if (!work) continue;
      ids.set(`${item.kind}:${item.key}`, id);
      // Live state that is newer than this history entry wins.
      if (work.at > item.at) continue;
      work.node.status =
        item.state === 'merged'
          ? 'Merged'
          : item.state === 'closed'
            ? 'Closed'
            : item.state === 'open'
              ? 'Open'
              : null;
      if (item.review_state) work.node.review_state = item.review_state;
      if (item.labels?.length)
        work.node.labels = nextLabels([], item.labels, []);
      const issue =
        item.kind === 'pull_request' && item.related_issue
          ? ids.get(`issue:${item.related_issue}`)
          : undefined;
      const parent = issue ? this.works.get(issue) : undefined;
      if (activity.relations !== undefined) {
        // Explicit links distinguish mentions from resolutions. Keep the
        // objects separate rather than infer an issue state from its PR.
        delete work.node.parent_id;
      } else if (issue && parent) {
        work.node.parent_id = issue;
        if (parent.node.status !== 'Merged')
          parent.node.status = item.state === 'merged' ? 'Merged' : 'PR open';
      }
      changed = true;
    }
    if (changed) this.trim();
    return changed;
  }

  /** Used to expire runs and work without running an animation loop forever. */
  hasLiveActivity(minutes: number | null, now = Date.now()): boolean {
    const since = minutes === null ? 0 : now - minutes * 60_000;
    return (
      [...this.works.values()].some(
        (work) =>
          work.at >= since && (work.expires === undefined || now < work.expires)
      ) ||
      [...this.runs.values()].some(
        (run) =>
          run.at >= since && (run.end === undefined || now - run.end < 60_000)
      )
    );
  }
  clear(): void {
    this.epoch += 1;
    this.works.clear();
    this.runs.clear();
    this.links.clear();
    this.seen.clear();
  }
}
