/**
 * A role's display name. Role names are stored lower case with underscores
 * (`team_admin`); people read "Team admin".
 *
 * @param name - The stored role name
 * @returns The name in sentence case, or "Role" when there is none
 */
export function roleLabel(name: string | null | undefined): string {
  const value = String(name || '').trim();
  if (!value) return 'Role';
  return value
    .replace(/_/g, ' ')
    .replace(/^\w/, (character) => character.toUpperCase());
}
