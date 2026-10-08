import type { GrantLevel } from '../../../hierarchy-api';

/** Grant levels and the role each one maps to in a subaccount. */
export const GRANT_LEVELS: Record<GrantLevel, string> = {
  read: 'Read (viewer)',
  operate: 'Operate (executor)',
  admin: 'Admin',
};
