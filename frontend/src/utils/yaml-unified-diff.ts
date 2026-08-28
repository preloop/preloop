/**
 * Line-level unified diff for policy YAML. No third-party diff library.
 */

export function normalizeYamlText(text: string): string {
  return text
    .replace(/\r\n/g, '\n')
    .replace(/[ \t]+$/gm, '')
    .trimEnd();
}

export function yamlDocumentsEqual(before: string, after: string): boolean {
  return normalizeYamlText(before) === normalizeYamlText(after);
}

function lcsMatrix(a: string[], b: string[]): number[][] {
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

export function unifiedYamlDiff(
  before: string,
  after: string,
  filename = 'policies.yaml'
): string {
  const left = normalizeYamlText(before).split('\n');
  const right = normalizeYamlText(after).split('\n');
  if (
    left.length === 1 &&
    left[0] === '' &&
    right.length === 1 &&
    right[0] === ''
  ) {
    return '';
  }
  const matrix = lcsMatrix(left, right);
  const lines: string[] = [`--- a/${filename}`, `+++ b/${filename}`];
  let i = 0;
  let j = 0;
  while (i < left.length && j < right.length) {
    if (left[i] === right[j]) {
      lines.push(` ${left[i]}`);
      i += 1;
      j += 1;
    } else if (matrix[i + 1][j] >= matrix[i][j + 1]) {
      lines.push(`-${left[i]}`);
      i += 1;
    } else {
      lines.push(`+${right[j]}`);
      j += 1;
    }
  }
  while (i < left.length) {
    lines.push(`-${left[i]}`);
    i += 1;
  }
  while (j < right.length) {
    lines.push(`+${right[j]}`);
    j += 1;
  }
  return lines.join('\n');
}
