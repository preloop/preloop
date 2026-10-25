import { parseUTCDate } from './date';
import {
  finiteCount,
  object,
  safeEventText,
  type FleetMessage,
} from './fleet-map-motion';

export interface FleetRecentEvent {
  node: string;
  kind: FleetMessage['kind'];
  text: string;
  icon: string;
  at: number;
  blocked: boolean;
  input: number;
  output: number;
}

/** A bounded, in-memory tally of events this map has actually received. */
export class FleetEventLog {
  openedAt = Date.now();
  private events: FleetRecentEvent[] = [];
  private seen = new Set<string>();
  private completed = new Set<string>();
  truncated = false;

  record(messages: FleetMessage[], wire: unknown, now = Date.now()): boolean {
    const record = object(wire);
    const payload = object(record.payload);
    const request = safeEventText(payload.gateway_request_id);
    if (
      messages[0]?.kind === 'request' &&
      request &&
      this.completed.has(request)
    )
      return false;
    if (messages[0]?.kind === 'response' && request) {
      this.completed.add(request);
      if (this.completed.size > 4000)
        this.completed.delete(this.completed.values().next().value!);
    }
    const rawId = safeEventText(
      payload.activity_id ||
        payload.event_id ||
        record.event_id ||
        record.approval_request_id ||
        payload.correlation_id ||
        payload.api_usage_id ||
        payload.gateway_request_id ||
        payload.request_id
    );
    const kind = messages[0]?.kind;
    const id = rawId && `${kind}:${rawId}`;
    if (id && this.seen.has(id)) return false;
    if (id) {
      this.seen.add(id);
      if (this.seen.size > 4000)
        this.seen.delete(this.seen.values().next().value!);
    }
    const timestamp = safeEventText(
      payload.timestamp || payload.last_activity_at || record.timestamp
    );
    const parsed = timestamp ? parseUTCDate(timestamp).getTime() : now;
    const at = Number.isFinite(parsed) ? Math.min(now, parsed) : now;
    for (const message of messages) {
      this.events.push({
        node: message.actor ?? message.from,
        kind: message.kind,
        text: message.label,
        icon: message.icon ?? 'activity',
        at,
        blocked: ['blocked', 'refused', 'denied', 'declined'].includes(
          String(payload.status)
        ),
        input:
          message.kind === 'response'
            ? (finiteCount(payload.prompt_tokens) ?? 0)
            : 0,
        output:
          message.kind === 'response'
            ? (finiteCount(payload.completion_tokens) ?? 0)
            : 0,
      });
    }
    if (this.events.length > 2000) {
      this.truncated = true;
      this.events = this.events.slice(-2000);
    }
    return true;
  }

  forNode(
    node: string,
    minutes: number | null,
    now = Date.now()
  ): FleetRecentEvent[] {
    const since =
      minutes === null
        ? this.openedAt
        : Math.max(this.openedAt, now - minutes * 60_000);
    return this.events.filter(
      (event) => event.node === node && event.at >= since
    );
  }

  recent(
    node: string,
    minutes: number | null
  ): (FleetRecentEvent & { count: number })[] {
    const folded: (FleetRecentEvent & { count: number })[] = [];
    for (const event of this.forNode(node, minutes)) {
      const previous = folded[folded.length - 1];
      if (
        event.kind === 'tool' &&
        previous?.kind === 'tool' &&
        previous.text === event.text
      ) {
        previous.count++;
        previous.at = event.at;
      } else folded.push({ ...event, count: 1 });
    }
    return folded.slice(-10).reverse();
  }

  clear(): void {
    this.openedAt = Date.now();
    this.events = [];
    this.seen.clear();
    this.completed.clear();
    this.truncated = false;
  }
}
