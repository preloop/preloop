import { css, html } from 'lit';

/**
 * The console's one trend chart, shared by the Overview Usage card and the
 * Budget health card so the two read as one system: a 100x40 viewBox
 * stretched to the card width, a primary line over a 12% area, a dashed
 * projection over a hatched area, a thin "now" marker and a legend that
 * says which part is data and which is a guess.
 */
export const SPARKLINE_WIDTH = 100;
export const SPARKLINE_HEIGHT = 40;

let sparklineSeq = 0;

/** Unique clip and hatch ids for one chart instance (ids cross no shadow root). */
export function sparklineIds(prefix: string): { clip: string; hatch: string } {
  sparklineSeq += 1;
  return {
    clip: `${prefix}-sparkline-clip-${sparklineSeq}`,
    hatch: `${prefix}-sparkline-hatch-${sparklineSeq}`,
  };
}

/** y for `value` on a chart whose top is `max`, with 2 units of headroom. */
export function sparklineY(value: number, max: number): number {
  return Number(
    (
      SPARKLINE_HEIGHT -
      (value / (max || 1)) * (SPARKLINE_HEIGHT - 4) -
      2
    ).toFixed(2)
  );
}

export const sparklineStyles = css`
  .sparkline {
    display: block;
    width: 100%;
    height: 40px;
  }

  /* Says which part of the chart is data and which is a guess. */
  .sparkline-legend {
    align-items: center;
    color: var(--console-meta-color);
    display: flex;
    flex-wrap: wrap;
    font-size: 0.75rem;
    gap: var(--sl-spacing-x-small);
    margin-top: var(--sl-spacing-2x-small);
    font-variant-numeric: tabular-nums;
  }

  .legend-item {
    align-items: center;
    display: inline-flex;
    gap: var(--sl-spacing-2x-small);
    white-space: nowrap;
  }

  .legend-total {
    flex-basis: 100%;
  }

  .legend-swatch {
    display: inline-block;
    height: 0;
    vertical-align: middle;
    width: 16px;
    border-top: 2px solid var(--sl-color-primary-500);
  }

  .legend-swatch.dashed {
    border-top-style: dashed;
    border-top-color: var(--sl-color-primary-400);
  }

  .legend-swatch.limit {
    border-top: 1px dashed var(--sl-color-neutral-500);
  }

  .legend-swatch.soft {
    border-top: 1px dashed var(--sl-color-warning-500);
  }

  /* One deliberate motion: the trend reveals itself once, left to right, on
     the paint that first has data. A clip rectangle grows across the chart
     and uncovers the line and the area together, so they always end at the
     same x. Under prefers-reduced-motion the chart is simply there. */
  .sparkline-reveal {
    transform-origin: 0 0;
  }

  @media (prefers-reduced-motion: no-preference) {
    .sparkline-reveal {
      animation: sparkline-reveal 300ms ease-out both;
    }
  }

  @keyframes sparkline-reveal {
    from {
      transform: scaleX(0);
    }
    to {
      transform: scaleX(1);
    }
  }
`;

/** One svg, one clip: the reveal uncovers data and projection together. */
export function renderSparklineSvg(
  label: string,
  shapes: unknown,
  ids: { clip: string; hatch: string },
  className = 'sparkline'
) {
  const width = SPARKLINE_WIDTH;
  const height = SPARKLINE_HEIGHT;
  return html`
    <svg
      class=${className}
      viewBox="0 0 ${width} ${height}"
      preserveAspectRatio="none"
      role="img"
      aria-label=${label}
    >
      <defs>
        <clipPath id=${ids.clip}>
          <!-- A little taller than the viewBox so the stroke is never
               clipped at the top or bottom edge. -->
          <rect
            class="sparkline-reveal"
            x="0"
            y="-4"
            width=${width}
            height=${height + 8}
          ></rect>
        </clipPath>
        <pattern
          id=${ids.hatch}
          patternUnits="userSpaceOnUse"
          width="2"
          height="4"
          patternTransform="rotate(45)"
        >
          <rect
            width="0.6"
            height="4"
            fill="var(--sl-color-primary-500)"
            opacity="0.35"
          ></rect>
        </pattern>
      </defs>
      <g class="sparkline-shapes" clip-path="url(#${ids.clip})">${shapes}</g>
    </svg>
  `;
}
