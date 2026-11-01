/** A transport cursor detects missing updates; it does not attest completion. */
export class ExecutionSequence {
  value: number | undefined;

  accept(seq: unknown): 'next' | 'gap' | 'stale' | 'legacy' {
    if (!Number.isSafeInteger(seq) || (seq as number) < 0) return 'legacy';
    const incoming = seq as number;
    if (this.value !== undefined && incoming <= this.value) return 'stale';
    const gap = this.value !== undefined && incoming !== this.value + 1;
    this.value = incoming;
    return gap ? 'gap' : 'next';
  }
}
