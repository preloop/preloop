import { expect } from '@open-wc/testing';
import type { CSSResultGroup, CSSResult } from 'lit';

import { reducedMotionStyles, prefersReducedMotion } from './reduced-motion';

// Every component that ships a @keyframes animation must carry the global
// reduced-motion guard in its static styles (DESIGN.md D19).
import { LandingView } from '../views/public/landing-view';
import { AgentsView } from '../views/authed/agents-view';
import { AuditView } from '../views/authed/audit-view';
import { FlowExecutionsView } from '../views/authed/flow-executions-view';
import { FlowExecutionView } from '../views/authed/flow-execution-view';
import { DashboardView } from '../views/authed/dashboard-control-plane-view';

function flattenStyles(styles: CSSResultGroup | undefined): string {
  if (!styles) return '';
  if (Array.isArray(styles)) {
    return styles.map((entry) => flattenStyles(entry)).join('\n');
  }
  return String((styles as CSSResult).cssText ?? '');
}

describe('reduced-motion guard', () => {
  it('defines a prefers-reduced-motion media block that stops animation', () => {
    const text = reducedMotionStyles.cssText;
    expect(text).to.contain('@media (prefers-reduced-motion: reduce)');
    expect(text).to.contain('animation-duration: 0.01ms !important');
    expect(text).to.contain('transition-duration: 0.01ms !important');
  });

  it('prefersReducedMotion() reflects the matchMedia preference', () => {
    // In the test browser the preference is off by default; the helper must
    // return a boolean either way without throwing.
    expect(prefersReducedMotion()).to.be.a('boolean');
  });

  const animatedComponents: Array<[string, CSSResultGroup | undefined]> = [
    ['landing-view', LandingView.styles],
    ['agents-view', AgentsView.styles],
    ['audit-view', AuditView.styles],
    ['flow-executions-view', FlowExecutionsView.styles],
    ['flow-execution-view', FlowExecutionView.styles],
    ['dashboard-control-plane-view', DashboardView.styles],
  ];

  for (const [name, styles] of animatedComponents) {
    it(`${name} ships the reduced-motion guard alongside its animations`, () => {
      const text = flattenStyles(styles);
      expect(text, `${name} styles`).to.contain(
        '@media (prefers-reduced-motion: reduce)'
      );
    });
  }
});
