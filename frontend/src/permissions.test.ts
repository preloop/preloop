import { expect } from '@open-wc/testing';
import {
  hasAnyPermission,
  hasPermission,
  humanizePermission,
  isRbacActive,
  PermissionError,
} from '../permissions';

suite('permissions helpers', () => {
  test('treats null/undefined permissions as RBAC inactive', () => {
    expect(isRbacActive(null)).to.equal(false);
    expect(isRbacActive(undefined)).to.equal(false);
    expect(hasPermission(null, 'view_cost')).to.equal(true);
    expect(hasAnyPermission(undefined, ['view_cost'])).to.equal(true);
  });

  test('enforces allow-list when RBAC is active', () => {
    const permissions = ['view_cost', 'view_agents'];
    expect(isRbacActive(permissions)).to.equal(true);
    expect(hasPermission(permissions, 'view_cost')).to.equal(true);
    expect(hasPermission(permissions, 'manage_budgets')).to.equal(false);
    expect(
      hasAnyPermission(permissions, ['manage_budgets', 'view_agents'])
    ).to.equal(true);
    expect(hasAnyPermission(permissions, ['manage_budgets'])).to.equal(false);
  });

  test('humanizes permission names', () => {
    expect(humanizePermission('view_audit_logs')).to.equal('View Audit Logs');
  });

  test('PermissionError carries required permission', () => {
    const error = new PermissionError('Denied', 'view_cost');
    expect(error.status).to.equal(403);
    expect(error.requiredPermission).to.equal('view_cost');
  });
});
