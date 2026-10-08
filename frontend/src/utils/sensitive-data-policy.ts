/**
 * Form model for the Sensitive data tab of the Policies page.
 *
 * The tab edits only the `sensitive_data` block of the policy YAML. It owns
 * the rules whose id starts with `console-` (one per action) and the
 * `reference_only` entries it renders; every other rule, entry and key is
 * carried through untouched so a YAML author's work survives a console save.
 */
import { Document, isScalar, parseDocument, visit } from 'yaml';

export type TypeAction = 'notify' | 'redact' | 'deny';
export type ScopeMode = 'all' | 'agents' | 'targets';
export type SensitiveTarget =
  'model.request' | 'model.response' | 'tool.args' | 'tool.result';
export type ApproverView = 'redacted' | 'original_until_decided';

export const ALL_TARGETS: SensitiveTarget[] = [
  'model.request',
  'model.response',
  'tool.args',
  'tool.result',
];

export const TARGET_LABELS: Record<SensitiveTarget, string> = {
  'model.request': 'Prompts',
  'model.response': 'Model replies',
  'tool.args': 'Tool inputs',
  'tool.result': 'Tool results',
};

export const ACTION_ORDER: TypeAction[] = ['deny', 'redact', 'notify'];

export const ACTION_LABELS: Record<TypeAction, string> = {
  notify: 'Detect only',
  redact: 'Redact in logs',
  deny: 'Block',
};

/** Rule ids the console writes; everything else belongs to YAML authors. */
export const CONSOLE_RULE_PREFIX = 'console-';

export interface CustomPatternForm {
  name: string;
  regex: string;
}

export interface KeywordListForm {
  name: string;
  terms: string[];
}

export interface Scope {
  agents: string[];
  tools: string[];
  servers: string[];
}

export interface ReferenceOnlyForm {
  id: string;
  scope: Scope;
  keepFields: string[];
  approverView: ApproverView;
}

export interface SensitiveDataForm {
  types: Record<string, TypeAction>;
  locales: string[];
  customPatterns: CustomPatternForm[];
  keywords: KeywordListForm[];
  scopeMode: ScopeMode;
  scope: Scope;
  on: SensitiveTarget[];
  referenceOnly: ReferenceOnlyForm[];
}

type Json = Record<string, any>;

function isObject(value: unknown): value is Json {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}

function clone<T>(value: T): T {
  return value === undefined ? value : JSON.parse(JSON.stringify(value));
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.map((item) => String(item)) : [];
}

function readScope(raw: unknown): Scope {
  const scope = isObject(raw) ? raw : {};
  return {
    agents: strings(scope.agents),
    tools: strings(scope.tools),
    servers: strings(scope.servers),
  };
}

function scopeMode(scope: Scope): ScopeMode {
  if (scope.agents.length) return 'agents';
  if (scope.tools.length || scope.servers.length) return 'targets';
  return 'all';
}

export function emptyForm(): SensitiveDataForm {
  return {
    types: {},
    locales: [],
    customPatterns: [],
    keywords: [],
    scopeMode: 'all',
    scope: { agents: [], tools: [], servers: [] },
    on: [...ALL_TARGETS],
    referenceOnly: [],
  };
}

function isConsoleRule(rule: unknown): rule is Json {
  return (
    isObject(rule) &&
    typeof rule.id === 'string' &&
    rule.id.startsWith(CONSOLE_RULE_PREFIX)
  );
}

/** Build the form state from a stored `sensitive_data` block. */
export function blockToForm(block: unknown): SensitiveDataForm {
  const form = emptyForm();
  if (!isObject(block)) return form;
  const detectors = isObject(block.detectors) ? block.detectors : {};
  form.locales = strings(detectors.locales);
  form.customPatterns = (
    Array.isArray(detectors.custom_patterns) ? detectors.custom_patterns : []
  )
    .filter(isObject)
    .map((item) => ({
      name: String(item.name ?? ''),
      regex: String(item.regex ?? ''),
    }));
  form.keywords = (Array.isArray(detectors.keywords) ? detectors.keywords : [])
    .filter(isObject)
    .map((item) => ({
      name: String(item.name ?? ''),
      terms: strings(item.terms),
    }));

  const rules = (Array.isArray(block.rules) ? block.rules : []).filter(
    isConsoleRule
  );
  let shared: Json | undefined;
  for (const action of ACTION_ORDER) {
    const rule = rules.find(
      (item) => item.id === `${CONSOLE_RULE_PREFIX}${action}`
    );
    if (!rule || rule.enabled === false) continue;
    shared = shared ?? rule;
    for (const type of strings(rule.types)) {
      if (!(type in form.types)) form.types[type] = action;
    }
  }
  if (shared) {
    const on = strings(shared.on).filter((item): item is SensitiveTarget =>
      (ALL_TARGETS as string[]).includes(item)
    );
    form.on = on.length ? on : [...ALL_TARGETS];
    form.scope = readScope(shared.scope);
    form.scopeMode = scopeMode(form.scope);
  }

  form.referenceOnly = (
    Array.isArray(block.reference_only) ? block.reference_only : []
  )
    .filter(isObject)
    .map((item) => ({
      id: String(item.id ?? ''),
      scope: readScope(item.scope),
      keepFields: strings(item.keep_fields),
      approverView:
        item.approver_view === 'original_until_decided'
          ? 'original_until_decided'
          : 'redacted',
    }));
  return form;
}

function writeScope(target: Json, scope: Scope) {
  const out: Json = isObject(target.scope) ? { ...target.scope } : {};
  for (const key of ['agents', 'tools', 'servers'] as const) {
    if (scope[key].length) out[key] = [...scope[key]];
    else delete out[key];
  }
  if (Object.keys(out).length) target.scope = out;
  else delete target.scope;
}

function effectiveScope(form: SensitiveDataForm): Scope {
  if (form.scopeMode === 'agents') {
    return { agents: [...form.scope.agents], tools: [], servers: [] };
  }
  if (form.scopeMode === 'targets') {
    return {
      agents: [],
      tools: [...form.scope.tools],
      servers: [...form.scope.servers],
    };
  }
  return { agents: [], tools: [], servers: [] };
}

/** Merge entries by name so fields the form does not show survive. */
function mergeNamed<T extends { name: string }>(
  previous: unknown,
  items: T[],
  write: (target: Json, item: T) => void
): Json[] {
  const old = (Array.isArray(previous) ? previous : []).filter(isObject);
  return items
    .filter((item) => item.name.trim())
    .map((item) => {
      const base = clone(old.find((entry) => entry.name === item.name)) ?? {};
      base.name = item.name.trim();
      write(base, item);
      return base;
    });
}

/**
 * Apply the form to a stored block and return the new block, or `null` when
 * nothing is left (the key is then dropped from the document).
 */
export function formToBlock(
  form: SensitiveDataForm,
  original: unknown
): Json | null {
  const block: Json = isObject(original) ? clone(original) : {};

  const detectors: Json = isObject(block.detectors) ? block.detectors : {};
  if (form.locales.length) detectors.locales = [...form.locales];
  else delete detectors.locales;
  const patterns = mergeNamed(
    detectors.custom_patterns,
    form.customPatterns,
    (target, item) => {
      target.regex = item.regex;
    }
  );
  if (patterns.length) detectors.custom_patterns = patterns;
  else delete detectors.custom_patterns;
  const keywords = mergeNamed(
    detectors.keywords,
    form.keywords.map((item) => ({
      ...item,
      terms: item.terms.map((term) => term.trim()).filter(Boolean),
    })),
    (target, item) => {
      target.terms = item.terms;
    }
  );
  if (keywords.length) detectors.keywords = keywords;
  else delete detectors.keywords;
  if (Object.keys(detectors).length) block.detectors = detectors;
  else delete block.detectors;

  const previousRules = (Array.isArray(block.rules) ? block.rules : []).filter(
    (rule: unknown) => isObject(rule)
  ) as Json[];
  const kept = previousRules.filter((rule) => !isConsoleRule(rule));
  const scope = effectiveScope(form);
  const consoleRules: Json[] = [];
  for (const action of ACTION_ORDER) {
    const types = Object.keys(form.types).filter(
      (type) => form.types[type] === action
    );
    if (!types.length) continue;
    const id = `${CONSOLE_RULE_PREFIX}${action}`;
    const rule: Json = clone(previousRules.find((item) => item.id === id)) ?? {
      id,
    };
    rule.id = id;
    delete rule.enabled;
    rule.on = [...form.on];
    rule.types = types;
    rule.action = action;
    writeScope(rule, scope);
    if (action !== 'redact') delete rule.redact_upstream;
    consoleRules.push(rule);
  }
  const rules = [...consoleRules, ...kept];
  if (rules.length) block.rules = rules;
  else delete block.rules;

  const previousRefs = (
    Array.isArray(block.reference_only) ? block.reference_only : []
  ).filter(isObject);
  const refs = form.referenceOnly
    .filter((entry) => entry.id.trim())
    .map((entry) => {
      const base: Json =
        clone(previousRefs.find((item) => item.id === entry.id)) ?? {};
      base.id = entry.id.trim();
      writeScope(base, entry.scope);
      const fields = entry.keepFields.map((f) => f.trim()).filter(Boolean);
      if (fields.length) base.keep_fields = fields;
      else delete base.keep_fields;
      // `redacted` is the server default and the export leaves it out;
      // write it only when it was written before, so a save is a no-op.
      if (entry.approverView === 'redacted' && !('approver_view' in base)) {
        delete base.approver_view;
      } else {
        base.approver_view = entry.approverView;
      }
      return base;
    });
  if (refs.length) block.reference_only = refs;
  else delete block.reference_only;

  return Object.keys(block).length ? block : null;
}

/**
 * A custom pattern or keyword list was renamed: move its action to the new
 * name so the console rule never lists a type the document no longer
 * defines. A cleared name drops the action.
 */
export function rekeyType(
  form: SensitiveDataForm,
  previous: string,
  next: string
) {
  const from = previous.trim();
  const to = next.trim();
  if (from === to) return;
  const action = form.types[from];
  delete form.types[from];
  if (action && to) form.types[to] = action;
}

/** Read the `sensitive_data` block out of a full policy document. */
export function readSensitiveData(policyYaml: string): unknown {
  if (!policyYaml.trim()) return undefined;
  const doc = parseDocument(policyYaml);
  if (doc.errors.length) throw new Error(doc.errors[0].message);
  const value = doc.toJS();
  return isObject(value) ? value.sensitive_data : undefined;
}

/**
 * Replace only the `sensitive_data` key of the policy document. Every
 * other key, comment and ordering is left as the export produced it.
 */
export function withSensitiveData(
  policyYaml: string,
  block: Json | null
): string {
  const doc = parseDocument(policyYaml.trim() ? policyYaml : '{}');
  if (doc.errors.length) throw new Error(doc.errors[0].message);
  if (block === null) {
    doc.delete('sensitive_data');
  } else {
    doc.set('sensitive_data', doc.createNode(block));
  }
  quoteYaml11Booleans(doc);
  return doc.toString();
}

/** YAML of the block alone, for the read-only preview. */
export function blockYaml(block: Json | null): string {
  if (!block) return '';
  const doc = new Document({ sensitive_data: block });
  quoteYaml11Booleans(doc);
  return doc.toString();
}

/**
 * The server reads policy YAML as YAML 1.1, where a bare `on`, `off`, `yes`
 * or `no` is a boolean. The rule key `on` must stay a string, so quote
 * every such scalar the console writes.
 */
const YAML11_BOOLEANS = /^(?:y|n|yes|no|on|off)$/i;

function quoteYaml11Booleans(doc: Document) {
  visit(doc, {
    Scalar(_key, node) {
      if (
        isScalar(node) &&
        typeof node.value === 'string' &&
        YAML11_BOOLEANS.test(node.value)
      ) {
        node.type = 'QUOTE_DOUBLE';
      }
    },
  });
}

/**
 * JSONPath subset accepted by reference-only logging. Same pattern as
 * KEEP_FIELD_RE in the server's policy schema: dotted keys, [n] and [*],
 * rooted at $ (tool arguments) or $result (tool result).
 */
const JSON_PATH_RE =
  /^\$(?:result)?(?:\.[A-Za-z_][A-Za-z0-9_-]*|\[\d+\]|\[\*\])+$/;

/** Server cap on keep_fields per reference-only entry. */
export const MAX_KEEP_FIELDS = 32;

export function isValidJsonPath(path: string): boolean {
  return JSON_PATH_RE.test(path.trim());
}

/**
 * Server TYPE_NAME_RE: one leading letter, then up to 63 more, so 1-64
 * characters. Custom pattern and keyword names cannot shadow a built-in.
 */
const TYPE_NAME_RE = /^[a-z][a-z0-9_]{0,63}$/;

/** Problems the user must fix before Save, keyed for inline display. */
export function formErrors(
  form: SensitiveDataForm,
  builtinIds: Iterable<string> = []
): Record<string, string> {
  const reserved = new Set(builtinIds);
  const errors: Record<string, string> = {};
  form.referenceOnly.forEach((entry, index) => {
    if (!entry.id.trim()) {
      errors[`ref-${index}-id`] = 'Give this entry a name.';
    }
    if (
      !entry.scope.tools.length &&
      !entry.scope.servers.length &&
      !entry.scope.agents.length
    ) {
      errors[`ref-${index}-scope`] =
        'Pick at least one tool, server or agent, otherwise every call is reference only.';
    }
    if (entry.keepFields.filter((f) => f.trim()).length > MAX_KEEP_FIELDS) {
      errors[`ref-${index}-fields`] =
        `Keep at most ${MAX_KEEP_FIELDS} fields per entry.`;
    }
    entry.keepFields.forEach((field, fieldIndex) => {
      if (field.trim() && !isValidJsonPath(field)) {
        errors[`ref-${index}-field-${fieldIndex}`] =
          'Use a path like $.consent_id, $.items[0].id or $result.consent_id.';
      }
    });
  });
  const names = new Set<string>();
  [...form.customPatterns, ...form.keywords].forEach((item, index) => {
    const name = item.name.trim();
    if (!name) return;
    if (!TYPE_NAME_RE.test(name)) {
      errors[`name-${index}-${name}`] =
        `"${name}" must be 1-64 lower case letters, digits and underscores.`;
    } else if (reserved.has(name)) {
      errors[`name-${index}-${name}`] =
        `"${name}" is a built-in type; pick another name.`;
    } else if (names.has(name)) {
      errors[`name-${index}-${name}`] = `"${name}" is used twice.`;
    }
    names.add(name);
  });
  if (Object.keys(form.types).length && !form.on.length) {
    errors.on = 'Pick at least one place to check.';
  }
  if (form.scopeMode === 'agents' && !form.scope.agents.length) {
    errors.scope = 'Pick at least one agent.';
  }
  if (
    form.scopeMode === 'targets' &&
    !form.scope.tools.length &&
    !form.scope.servers.length
  ) {
    errors.scope = 'Pick at least one tool or server.';
  }
  return errors;
}

function list(items: string[]): string {
  if (items.length <= 1) return items.join('');
  return `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`;
}

/** Plain-language description of what the form will do. */
export function summarize(
  form: SensitiveDataForm,
  labels: Record<string, string> = {}
): string {
  const sentences: string[] = [];
  const where = form.on.map((item) => TARGET_LABELS[item].toLowerCase());
  const whereText =
    where.length === ALL_TARGETS.length ? '' : ` in ${list(where)}`;
  let scopeText = '';
  if (form.scopeMode === 'agents' && form.scope.agents.length) {
    scopeText = ` for ${list(form.scope.agents.map((id) => labels[id] ?? id))}`;
  } else if (form.scopeMode === 'targets') {
    const names = [...form.scope.tools, ...form.scope.servers];
    if (names.length) scopeText = ` on ${list(names)}`;
  }
  const name = (type: string) => labels[type] ?? type.replace(/_/g, ' ');
  for (const action of ACTION_ORDER) {
    const types = Object.keys(form.types).filter(
      (type) => form.types[type] === action
    );
    if (!types.length) continue;
    const subject = `${list(types.map(name))} matches${whereText}${scopeText}`;
    if (action === 'deny') {
      sentences.push(`${capitalize(subject)} are blocked.`);
    } else if (action === 'redact') {
      sentences.push(
        `${capitalize(subject)} are stored as ${list(
          types.map((type) => `[REDACTED:${type}]`)
        )}.`
      );
    } else {
      sentences.push(`${capitalize(subject)} are reported but left as is.`);
    }
  }
  for (const entry of form.referenceOnly) {
    const targets = [
      ...entry.scope.tools,
      ...entry.scope.servers,
      ...entry.scope.agents.map((id) => labels[id] ?? id),
    ];
    if (!targets.length) continue;
    const fields = entry.keepFields
      .map((field) => {
        const trimmed = field.trim();
        const fromResult = /^\$result[.[]/.test(trimmed);
        const path = trimmed.replace(/^\$(?:result)?\.?/, '');
        return fromResult && path ? `${path} (from the result)` : path;
      })
      .filter(Boolean);
    const keep = fields.length
      ? `only ${list(fields)} and a fingerprint`
      : 'only a fingerprint';
    sentences.push(`Calls to ${list(targets)} keep ${keep}.`);
  }
  if (!sentences.length) {
    return 'No sensitive data rules are set from this page.';
  }
  return sentences.join(' ');
}

function capitalize(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

export interface MatchSpan {
  type: string;
  start: number;
  end: number;
}

/**
 * What the stores keep for `text` under the form's actions: blocked calls
 * store nothing, redact types become `[REDACTED:<type>]`, detect-only types
 * stay as written.
 */
export function storedForm(
  text: string,
  matches: MatchSpan[],
  types: Record<string, TypeAction>
): { blocked: boolean; text: string } {
  if (matches.some((match) => types[match.type] === 'deny')) {
    return { blocked: true, text: '' };
  }
  const redact = matches
    .filter((match) => types[match.type] === 'redact')
    .sort((a, b) => a.start - b.start);
  let out = '';
  let cursor = 0;
  for (const match of redact) {
    if (match.start < cursor) continue;
    out += text.slice(cursor, match.start) + `[REDACTED:${match.type}]`;
    cursor = match.end;
  }
  return { blocked: false, text: out + text.slice(cursor) };
}

/** Split `text` into plain and matched segments for highlighting. */
export function segments(
  text: string,
  matches: MatchSpan[]
): { text: string; type?: string }[] {
  const out: { text: string; type?: string }[] = [];
  let cursor = 0;
  for (const match of [...matches].sort((a, b) => a.start - b.start)) {
    if (match.start < cursor) continue;
    if (match.start > cursor)
      out.push({ text: text.slice(cursor, match.start) });
    out.push({ text: text.slice(match.start, match.end), type: match.type });
    cursor = match.end;
  }
  if (cursor < text.length) out.push({ text: text.slice(cursor) });
  return out;
}
