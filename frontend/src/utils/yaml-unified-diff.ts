/**
 * Unified-diff text for policy YAML, over the shared line diff.
 */
import { diffLines } from './line-diff';

export function normalizeYamlText(text: string): string {
  return text
    .replace(/\r\n/g, '\n')
    .replace(/[ \t]+$/gm, '')
    .trimEnd();
}

export function yamlDocumentsEqual(before: string, after: string): boolean {
  return normalizeYamlText(before) === normalizeYamlText(after);
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
  const signs = { context: ' ', removed: '-', added: '+' } as const;
  return [
    `--- a/${filename}`,
    `+++ b/${filename}`,
    ...diffLines(left, right).map((line) => `${signs[line.type]}${line.text}`),
  ].join('\n');
}
