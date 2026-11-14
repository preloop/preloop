/** The sentence a model delete confirmation states about the flows it orphans. */
export function consumersSentence(count: number): string {
  return count === 1
    ? 'Used by 1 flow. Deleting it leaves that flow without a model.'
    : `Used by ${count} flows. Deleting it leaves them without a model.`;
}

/** The sentence for disabled flows, which do not block a delete. */
export function disabledSentence(count: number): string {
  return `${count} disabled flow${count === 1 ? '' : 's'} also select${count === 1 ? 's' : ''} it and will lose ${count === 1 ? 'its' : 'their'} model; ${count === 1 ? 'it does' : 'they do'} not block the delete.`;
}

/** Flows that can run, so a delete would break them now. */
export function enabledFlows<T extends { is_enabled?: boolean | null }>(
  flows: T[]
): T[] {
  // An older server sends no state; treat its flows as enabled.
  return flows.filter((flow) => flow.is_enabled !== false);
}

/** "2 enabled flows · 1 disabled" for the Used by header. */
export function flowCountsLabel(
  flows: { is_enabled?: boolean | null }[]
): string {
  const enabled = enabledFlows(flows).length;
  const disabled = flows.length - enabled;
  const head = `${enabled} enabled flow${enabled === 1 ? '' : 's'}`;
  return disabled ? `${head} · ${disabled} disabled` : head;
}

/**
 * What the server's 409 says, from its own counts: every enabled flow
 * counts, though the caller may see only some of them.
 */
export function refusalSentence(flowCount: number, shown: number): string {
  const head = consumersSentence(flowCount);
  return shown < flowCount ? `${head} You can see ${shown} of them.` : head;
}
