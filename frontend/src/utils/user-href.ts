/** The person page for one account member, wherever a user is named. */
export const userHref = (id: string): string =>
  `/console/settings/users/${encodeURIComponent(id)}`;
