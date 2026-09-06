/**
 * Line-level diff, shared by everything in the console that shows a before
 * and an after: the policy YAML preview and the file edits an agent asks
 * permission to make. No third-party diff library.
 */

export type DiffLineType = 'context' | 'added' | 'removed';

export interface DiffLine {
  type: DiffLineType;
  text: string;
}

/**
 * Above this many lines on either side the LCS matrix costs more than the
 * answer is worth, and a diff that big is read as "the file changed" anyway.
 * Past it, the two texts are reported as a wholesale replacement.
 */
const LCS_LINE_LIMIT = 2000;

function lcsMatrix(a: readonly string[], b: readonly string[]): number[][] {
  const rows = a.length;
  const cols = b.length;
  const matrix: number[][] = Array.from({ length: rows + 1 }, () =>
    Array(cols + 1).fill(0)
  );
  for (let i = rows - 1; i >= 0; i--) {
    for (let j = cols - 1; j >= 0; j--) {
      matrix[i][j] =
        a[i] === b[j]
          ? matrix[i + 1][j + 1] + 1
          : Math.max(matrix[i + 1][j], matrix[i][j + 1]);
    }
  }
  return matrix;
}

/** Split text into lines, dropping the empty line a trailing newline adds. */
export function toDiffLines(text: string): string[] {
  const lines = text.replace(/\r\n/g, '\n').split('\n');
  if (lines.length > 1 && lines[lines.length - 1] === '') lines.pop();
  return lines;
}

/**
 * Diff two line arrays into one ordered run of context, removed and added
 * lines. Removals come before the additions that replace them, which is the
 * order a reviewer reads them in.
 */
export function diffLines(
  before: readonly string[],
  after: readonly string[]
): DiffLine[] {
  if (before.length > LCS_LINE_LIMIT || after.length > LCS_LINE_LIMIT) {
    return [
      ...before.map((text): DiffLine => ({ type: 'removed', text })),
      ...after.map((text): DiffLine => ({ type: 'added', text })),
    ];
  }

  const matrix = lcsMatrix(before, after);
  const result: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < before.length && j < after.length) {
    if (before[i] === after[j]) {
      result.push({ type: 'context', text: before[i] });
      i += 1;
      j += 1;
    } else if (matrix[i + 1][j] >= matrix[i][j + 1]) {
      result.push({ type: 'removed', text: before[i] });
      i += 1;
    } else {
      result.push({ type: 'added', text: after[j] });
      j += 1;
    }
  }
  while (i < before.length) {
    result.push({ type: 'removed', text: before[i] });
    i += 1;
  }
  while (j < after.length) {
    result.push({ type: 'added', text: after[j] });
    j += 1;
  }
  return result;
}
