/**
 * Client-side RBAC helpers.
 *
 * When `/api/v1/auth/users/me` returns `permissions: null` (or omits the field),
 * RBAC is inactive (OSS / DISABLE_RBAC) and access is unrestricted.
 * When it returns an array, that array is the allow-list.
 */

export type UserPermissions = string[] | null | undefined;

export class PermissionError extends Error {
  readonly status = 403;
  readonly requiredPermission?: string;

  constructor(message: string, requiredPermission?: string) {
    super(message);
    this.name = 'PermissionError';
    this.requiredPermission = requiredPermission;
  }
}

export function isRbacActive(permissions: UserPermissions): boolean {
  return Array.isArray(permissions);
}

export function hasPermission(
  permissions: UserPermissions,
  permission: string
): boolean {
  if (!isRbacActive(permissions)) {
    return true;
  }
  return permissions!.includes(permission);
}

export function hasAnyPermission(
  permissions: UserPermissions,
  required: string[]
): boolean {
  if (!isRbacActive(permissions)) {
    return true;
  }
  if (required.length === 0) {
    return true;
  }
  return required.some((permission) => permissions!.includes(permission));
}

/** Parse a FastAPI 403 body into a PermissionError when possible. */
export async function permissionErrorFromResponse(
  response: Response
): Promise<PermissionError> {
  let detail = 'You do not have permission to perform this action.';
  let requiredPermission: string | undefined;
  try {
    const body = await response.clone().json();
    if (typeof body?.detail === 'string' && body.detail.trim()) {
      detail = body.detail;
      const match = body.detail.match(/Required(?: one of)?: (.+)$/i);
      if (match) {
        requiredPermission = match[1];
      }
    }
  } catch {
    // ignore non-JSON bodies
  }
  return new PermissionError(detail, requiredPermission);
}

export function humanizePermission(permission: string): string {
  return permission
    .split(/[_:]/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ');
}
