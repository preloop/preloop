import DOMPurify from 'dompurify';
import { marked, Renderer } from 'marked';

const SEVERITY_ORDER = ['critical', 'high', 'medium', 'low', 'unknown'];

export interface ReportHeading {
  id: string;
  text: string;
  level: number;
}

export interface FindingRow {
  id: string;
  lens: string;
  severity: string;
  title: string;
  evidence: string;
  status: string;
  pkg: string;
  cvss: string;
  kev: string;
  fix: string;
  vex: string;
}

export interface RegisterRow {
  module: string;
  lens: string;
  status: string;
  note: string;
}

export function findingsSummaryLabel(summary: unknown): string | null {
  if (!summary || typeof summary !== 'object') return null;
  const counts = (summary as { counts_by_severity?: unknown })
    .counts_by_severity;
  if (!counts || typeof counts !== 'object') return null;
  const record = counts as Record<string, unknown>;
  const extra = Object.keys(record).filter(
    (key) => !SEVERITY_ORDER.includes(key)
  );
  let total = 0;
  const parts: string[] = [];
  for (const key of [...SEVERITY_ORDER, ...extra]) {
    const count = Number(record[key]);
    if (!Number.isFinite(count) || count <= 0) continue;
    total += count;
    parts.push(`${count} ${key}`);
  }
  if (total === 0) return '0 findings';
  const noun = total === 1 ? 'finding' : 'findings';
  return parts.length
    ? `${total} ${noun}: ${parts.join(', ')}`
    : `${total} ${noun}`;
}

export function severityVariant(severity: string): string {
  const value = severity.toLowerCase();
  if (value === 'critical' || value === 'high') return 'danger';
  if (value === 'medium') return 'warning';
  if (value === 'low') return 'success';
  return 'neutral';
}

function textOf(value: unknown): string {
  if (value == null) return '';
  if (typeof value === 'boolean') return value ? 'yes' : 'no';
  return String(value);
}

function findingTitle(row: Record<string, unknown>): string {
  const title = textOf(row.title || row.summary);
  if (title) return title;
  const recommendation = textOf(row.recommendation);
  if (recommendation) {
    return recommendation.length > 180
      ? `${recommendation.slice(0, 177)}...`
      : recommendation;
  }
  return textOf(row.id);
}

function findingEvidence(row: Record<string, unknown>): string {
  const file = textOf(row.file);
  const line = textOf(row.line);
  if (file && line) return `${file}:${line}`;
  if (file) return file;
  return textOf(row.evidence || row.location);
}

function findingStatus(row: Record<string, unknown>): string {
  if (row.waived === true) return 'waived';
  return textOf(row.status || row.waiver_status);
}

export function isCraFindings(rows: FindingRow[]): boolean {
  return rows.some((row) => row.pkg || row.cvss || row.kev || row.vex);
}

export function parseFindings(payload: unknown): FindingRow[] {
  const list = Array.isArray(payload)
    ? payload
    : payload &&
        typeof payload === 'object' &&
        Array.isArray((payload as { findings?: unknown }).findings)
      ? (payload as { findings: unknown[] }).findings
      : [];
  return list
    .filter(
      (item): item is Record<string, unknown> =>
        !!item && typeof item === 'object'
    )
    .map((row) => ({
      id: textOf(row.id),
      lens: textOf(row.lens || row.category),
      severity: textOf(row.severity) || 'unknown',
      title: findingTitle(row),
      evidence: findingEvidence(row),
      status: findingStatus(row),
      pkg: textOf(row.pkg || row.package),
      cvss: textOf(row.cvss),
      kev: row.kev == null ? '' : textOf(row.kev),
      fix: textOf(row.fix || row.fix_version),
      vex: textOf(row.vex_status),
    }));
}

const REGISTER_RANK: Record<string, number> = { gap: 0, partial: 1 };

export function parseRegister(result: unknown): RegisterRow[] {
  const items =
    result &&
    typeof result === 'object' &&
    (result as { register?: { items?: unknown } }).register &&
    Array.isArray((result as { register: { items: unknown[] } }).register.items)
      ? (result as { register: { items: unknown[] } }).register.items
      : [];
  const rows = items
    .filter(
      (item): item is Record<string, unknown> =>
        !!item && typeof item === 'object'
    )
    .map((item) => ({
      module: textOf(item.module),
      lens: textOf(item.lens),
      status: textOf(item.status) || 'unknown',
      note: textOf(item.note),
    }));
  return rows.sort((left, right) => {
    const rank =
      (REGISTER_RANK[left.status] ?? 2) - (REGISTER_RANK[right.status] ?? 2);
    if (rank !== 0) return rank;
    return (
      left.module.localeCompare(right.module) ||
      left.lens.localeCompare(right.lens)
    );
  });
}

export function severityRank(severity: string): number {
  const index = SEVERITY_ORDER.indexOf(severity.toLowerCase());
  return index === -1 ? SEVERITY_ORDER.length : index;
}

export interface RenderedReport {
  html: string;
  headings: ReportHeading[];
}

function headingId(text: string, used: Map<string, number>): string {
  let id =
    text
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-|-$/g, '') || 'section';
  const seen = (used.get(id) || 0) + 1;
  used.set(id, seen);
  if (seen > 1) id = `${id}-${seen}`;
  return id;
}

function plainHeading(text: string): string {
  return text.replace(/[`*_]+/g, '').trim();
}

export function renderReportMarkdown(source: string): RenderedReport {
  const used = new Map<string, number>();
  const headings: ReportHeading[] = [];
  const renderer = new Renderer();
  renderer.heading = function ({ tokens, text, depth }) {
    const inner = this.parser.parseInline(tokens);
    const label = plainHeading(text);
    const id = headingId(label, used);
    headings.push({ id, text: label, level: depth });
    return `<h${depth} id="${id}">${inner}</h${depth}>`;
  };
  const parsed = marked.parse(source, {
    async: false,
    gfm: true,
    renderer,
  });
  return {
    html: DOMPurify.sanitize(typeof parsed === 'string' ? parsed : '', {
      ADD_ATTR: ['id'],
    }),
    headings,
  };
}

export function sameEvidencePack(
  current: {
    status?: string;
    sha256?: string | null;
    legal_hold?: boolean;
    integrity?: string | null;
    integrity_note?: string | null;
    error?: string | null;
  } | null,
  next: {
    status?: string;
    sha256?: string | null;
    legal_hold?: boolean;
    integrity?: string | null;
    integrity_note?: string | null;
    error?: string | null;
  } | null
): boolean {
  if (!current || !next) return current === next;
  return (
    current.status === next.status &&
    (current.sha256 || '') === (next.sha256 || '') &&
    Boolean(current.legal_hold) === Boolean(next.legal_hold) &&
    (current.integrity || '') === (next.integrity || '') &&
    (current.integrity_note || '') === (next.integrity_note || '') &&
    (current.error || '') === (next.error || '')
  );
}
