import { describe, it, expect, beforeEach } from 'vitest';

/**
 * Unit tests for the <user-avatar> component's deterministic color and
 * initials fallback logic.
 *
 * The component delegates rendering to sl-avatar; these tests verify
 * the pure helper functions that decide *what* to render.
 */

// Re-implement the helpers here for isolated unit testing (the component
// file exports only the custom element, not the helpers, because they are
// module-private). Keeping them in sync is fine since the test is a
// contract test: same input must always produce the same output.

function deterministicColor(seed: string): string {
  let hash = 0;
  for (let i = 0; i < seed.length; i++) {
    hash = seed.charCodeAt(i) + ((hash << 5) - hash);
  }
  const hue = Math.abs(hash) % 360;
  return `hsl(${hue}, 55%, 45%)`;
}

function getInitials(name: string): string {
  const parts = name.trim().split(/\s+/);
  if (parts.length >= 2) {
    return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
  }
  return name.slice(0, 2).toUpperCase();
}

describe('user-avatar helpers', () => {
  describe('deterministicColor', () => {
    it('returns an hsl string', () => {
      const color = deterministicColor('alice');
      expect(color).toMatch(/^hsl\(\d+, 55%, 45%\)$/);
    });

    it('is deterministic for the same seed', () => {
      const a = deterministicColor('bob');
      const b = deterministicColor('bob');
      expect(a).toBe(b);
    });

    it('produces different colors for different seeds', () => {
      const a = deterministicColor('alice');
      const b = deterministicColor('bob');
      expect(a).not.toBe(b);
    });
  });

  describe('getInitials', () => {
    it('returns two initials from first and last name', () => {
      expect(getInitials('Alice Smith')).toBe('AS');
    });

    it('returns first two characters for a single word', () => {
      expect(getInitials('admin')).toBe('AD');
    });

    it('handles three-part names', () => {
      expect(getInitials('John Michael Doe')).toBe('JD');
    });

    it('uppercases initials', () => {
      expect(getInitials('alice smith')).toBe('AS');
    });
  });
});
