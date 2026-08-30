import { expect } from '@open-wc/testing';

import { deterministicColor, getInitials } from './user-avatar.ts';

/**
 * Unit tests for the <user-avatar> component's deterministic color and
 * initials fallback logic.
 *
 * The component delegates rendering to sl-avatar; these tests verify
 * the pure helper functions that decide *what* to render, imported from
 * the module that ships so drift cannot go undetected.
 */

describe('user-avatar helpers', () => {
  describe('deterministicColor', () => {
    it('returns an hsl string', () => {
      const color = deterministicColor('alice');
      expect(color).to.match(/^hsl\(\d+, 55%, 45%\)$/);
    });

    it('is deterministic for the same seed', () => {
      const a = deterministicColor('bob');
      const b = deterministicColor('bob');
      expect(a).to.equal(b);
    });

    it('produces different colors for different seeds', () => {
      const a = deterministicColor('alice');
      const b = deterministicColor('bob');
      expect(a).to.not.equal(b);
    });
  });

  describe('getInitials', () => {
    it('returns two initials from first and last name', () => {
      expect(getInitials('Alice Smith')).to.equal('AS');
    });

    it('returns first two characters for a single word', () => {
      expect(getInitials('admin')).to.equal('AD');
    });

    it('handles three-part names', () => {
      expect(getInitials('John Michael Doe')).to.equal('JD');
    });

    it('uppercases initials', () => {
      expect(getInitials('alice smith')).to.equal('AS');
    });
  });
});
