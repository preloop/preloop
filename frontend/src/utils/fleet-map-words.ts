import type { FleetMapArrangement } from './fleet-map-spatial';

/**
 * The words the Fleet Map shows for its two arrangements, kept in one place
 * so a rename touches one file. Stored preferences and code keep the
 * original identifiers (`columns`, `spatial`).
 */
export const ARRANGEMENT_LABEL: Record<FleetMapArrangement, string> = {
  columns: 'Lanes',
  spatial: 'Canvas',
};

/** Icon-only segmented control: a lane per kind, or nodes free in space. */
export const ARRANGEMENT_ICON: Record<FleetMapArrangement, string> = {
  columns: 'layout-three-columns',
  spatial: 'bounding-box-circles',
};

export const ARRANGEMENTS: readonly FleetMapArrangement[] = [
  'columns',
  'spatial',
];

/**
 * Read an arrangement from a URL or harness flag. `lanes` and `canvas` are
 * the current words; `columns` and `spatial` stay accepted as aliases.
 */
export function parseArrangement(
  value: string | null | undefined
): FleetMapArrangement | undefined {
  const word = (value ?? '').trim().toLowerCase();
  if (word === 'lanes' || word === 'columns') return 'columns';
  if (word === 'canvas' || word === 'spatial') return 'spatial';
  return undefined;
}
