/** The sentence a model delete confirmation states about the flows it orphans. */
export function consumersSentence(count: number): string {
  return count === 1
    ? 'Used by 1 flow. Deleting it leaves that flow without a model.'
    : `Used by ${count} flows. Deleting it leaves them without a model.`;
}
