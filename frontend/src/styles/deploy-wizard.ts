import { css } from 'lit';

/**
 * One stylesheet for both deploy surfaces: `preloop-deploy-wizard` (the
 * onboarding path) and `preloop-agent-deployer` (the "Deploy Governed Agent"
 * dialog, which the wizard also nests inside its deploy path). The two
 * components carried two copies of the same rules, so a fix to the option
 * cards or the command blocks had to be made twice and drifted anyway.
 *
 * The recipe follows DESIGN.md:
 *  - Depth stays at two. A dialog panel is already the raised surface, so the
 *    step body adds no fill, no border and no shadow of its own. The single
 *    exception the design allows is a copyable command block, which sits on
 *    --console-page because it is a literal thing to select.
 *  - Type comes from the console scale (15 title / 14 body / 13 meta / 11
 *    eyebrow), not from the marketing sizes the wizard used.
 *  - One primary action per step, right aligned in an action bar; the
 *    secondary (Back) is a text button on the left.
 *  - Nothing scrolls sideways: commands wrap instead of clipping.
 */
export const deployWizardStyles = css`
  :host {
    display: block;
    width: 100%;
  }

  .wizard-shell {
    box-sizing: border-box;
    width: 100%;
    max-width: 46rem;
    margin: 0 auto;
    display: flex;
    flex-direction: column;
    gap: var(--sl-spacing-large);
    color: var(--sl-color-neutral-800);
    font-size: var(--console-text-body);
    line-height: 1.5;
  }

  /* The flow form is a form of its own and wants the extra column width. */
  .wizard-shell.wide {
    max-width: 56rem;
  }

  /* ---------------------------------------------------------------
     Step header: where am I, of how many, and what am I doing here.
     --------------------------------------------------------------- */
  .wizard-header {
    display: flex;
    flex-direction: column;
    gap: var(--sl-spacing-2x-small);
    min-width: 0;
  }

  .wizard-step-count {
    align-items: center;
    color: var(--console-meta-color);
    display: flex;
    font-size: var(--console-text-eyebrow);
    font-weight: 600;
    gap: var(--sl-spacing-small);
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  /* The rail is the count made visible: one tick per step, filled up to the
     step you are on. It is only drawn when the total is known (a branch
     screen does not yet know how long its path is). */
  .wizard-step-rail {
    display: inline-flex;
    gap: 4px;
  }

  .wizard-step-rail span {
    background: var(--console-hairline);
    border-radius: 999px;
    display: block;
    height: 3px;
    width: 18px;
  }

  .wizard-step-rail span.done {
    background: var(--sl-color-primary-600);
  }

  .wizard-title {
    color: var(--sl-color-neutral-900);
    font-size: var(--console-text-card-title);
    font-weight: 600;
    line-height: 1.3;
    margin: 0;
  }

  .wizard-copy {
    color: var(--console-meta-color);
    font-size: var(--console-text-meta);
    line-height: 1.5;
    margin: 0;
    max-width: 46rem;
  }

  /* ---------------------------------------------------------------
     Choice cards. Same object as a console row: a hairline box, an icon
     column of a fixed width, a title, one line of meta, a chevron. No fill,
     so a card inside a dialog panel does not become a third surface.
     --------------------------------------------------------------- */
  .wizard-card-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(17rem, 1fr));
    gap: var(--sl-spacing-medium);
    width: 100%;
  }

  .wizard-option-button {
    align-items: start;
    background: transparent;
    border: 1px solid var(--console-hairline);
    border-radius: var(--console-card-radius, 0.5rem);
    box-sizing: border-box;
    color: inherit;
    column-gap: var(--sl-spacing-small);
    cursor: pointer;
    display: grid;
    font: inherit;
    grid-template-columns: 1.25rem minmax(0, 1fr) 1rem;
    height: 100%;
    padding: var(--sl-spacing-medium);
    text-align: left;
    transition:
      background-color 120ms ease-out,
      border-color 120ms ease-out;
    width: 100%;
  }

  .wizard-option-button:hover {
    background: var(--console-hover-tint);
    border-color: var(
      --console-button-border-hover,
      var(--sl-color-neutral-300)
    );
  }

  .wizard-option-button:focus-visible {
    outline: 2px solid var(--sl-color-primary-500);
    outline-offset: 2px;
  }

  .wizard-option-icon {
    color: var(--sl-color-primary-600);
    font-size: 1rem;
    line-height: 1.35;
  }

  .wizard-option-copy {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }

  .wizard-option-title {
    color: var(--sl-color-neutral-900);
    font-size: var(--console-text-body);
    font-weight: 600;
    line-height: 1.35;
  }

  .wizard-option-description {
    color: var(--console-meta-color);
    font-size: var(--console-text-meta);
    line-height: 1.45;
  }

  .wizard-option-arrow {
    align-self: center;
    color: var(--console-meta-color);
    font-size: 0.875rem;
  }

  /* ---------------------------------------------------------------
     Step body and forms. One field width per step, help under the field.
     --------------------------------------------------------------- */
  .wizard-section {
    display: flex;
    flex-direction: column;
    gap: var(--sl-spacing-medium);
    min-width: 0;
    width: 100%;
  }

  .wizard-form {
    box-sizing: border-box;
    display: flex;
    flex-direction: column;
    gap: var(--sl-spacing-medium);
    max-width: 32rem;
    width: 100%;
  }

  .wizard-form sl-input,
  .wizard-form sl-textarea,
  .wizard-form sl-select {
    width: 100%;
  }

  /* A field plus the one link that belongs to it (add a model, learn more). */
  .wizard-field {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .wizard-field .field-link {
    align-self: flex-start;
  }

  .wizard-field .field-link::part(base) {
    height: auto;
    padding: 0;
  }

  /* A review block is rows under a rule, not a filled box. */
  .wizard-summary {
    display: flex;
    flex-direction: column;
    gap: 0;
    max-width: 32rem;
  }

  .wizard-summary-title {
    color: var(--console-meta-color);
    font-size: var(--console-text-eyebrow);
    font-weight: 600;
    letter-spacing: 0.06em;
    padding-bottom: var(--sl-spacing-2x-small);
    text-transform: uppercase;
  }

  .wizard-summary-row {
    align-items: baseline;
    border-top: 1px solid var(--console-hairline);
    display: flex;
    gap: var(--sl-spacing-medium);
    justify-content: space-between;
    padding: var(--sl-spacing-x-small) 0;
  }

  .wizard-summary-key {
    color: var(--console-meta-color);
    flex: 0 0 auto;
    font-size: var(--console-text-meta);
  }

  .wizard-summary-value {
    min-width: 0;
    overflow-wrap: anywhere;
    text-align: right;
  }

  /* ---------------------------------------------------------------
     Notices. A tint and an icon, never a second card.
     --------------------------------------------------------------- */
  .notice {
    align-items: flex-start;
    border-radius: var(--sl-border-radius-medium);
    display: flex;
    font-size: var(--console-text-meta);
    gap: var(--sl-spacing-small);
    line-height: 1.45;
    padding: var(--sl-spacing-small) var(--sl-spacing-medium);
  }

  .notice sl-icon {
    flex: 0 0 auto;
    font-size: 1rem;
    margin-top: 1px;
  }

  .notice.warning {
    background: color-mix(
      in srgb,
      var(--sl-color-warning-500) 12%,
      transparent
    );
    color: var(--sl-color-warning-800);
  }

  .notice.danger {
    background: color-mix(in srgb, var(--sl-color-danger-500) 12%, transparent);
    color: var(--sl-color-danger-800);
  }

  .notice.info {
    background: color-mix(
      in srgb,
      var(--sl-color-primary-500) 12%,
      transparent
    );
    color: var(--sl-color-primary-800);
  }

  /* ---------------------------------------------------------------
     Command blocks. The one place a filled box is allowed inside a card,
     because it is a literal thing to select. It wraps: a command that runs
     off the right edge cannot be read or copied by eye.
     --------------------------------------------------------------- */
  .command-steps {
    display: flex;
    flex-direction: column;
    gap: var(--sl-spacing-medium);
    min-width: 0;
  }

  .command-step {
    display: flex;
    flex-direction: column;
    gap: var(--sl-spacing-2x-small);
    min-width: 0;
  }

  .command-label {
    align-items: baseline;
    color: var(--sl-color-neutral-900);
    display: flex;
    font-size: var(--console-text-body);
    font-weight: 600;
    gap: var(--sl-spacing-x-small);
  }

  .command-index {
    color: var(--console-meta-color);
    flex: 0 0 auto;
    font-variant-numeric: tabular-nums;
    font-weight: 600;
    min-width: 1rem;
  }

  .command-row {
    align-items: flex-start;
    display: flex;
    gap: var(--sl-spacing-x-small);
    min-width: 0;
  }

  .command-row + .command-row {
    margin-top: var(--sl-spacing-2x-small);
  }

  .command-code {
    background: var(--console-page);
    border-radius: var(--sl-border-radius-medium);
    box-sizing: border-box;
    color: var(--sl-color-neutral-800);
    flex: 1 1 auto;
    font-family: var(--sl-font-mono);
    font-size: var(--console-text-meta);
    line-height: 1.5;
    min-width: 0;
    overflow-wrap: anywhere;
    padding: var(--sl-spacing-x-small) var(--sl-spacing-small);
    white-space: pre-wrap;
    word-break: break-word;
  }

  .command-snippet {
    margin: 0;
    max-height: 15rem;
    overflow-y: auto;
  }

  .command-row sl-copy-button {
    flex: 0 0 auto;
    margin-top: 2px;
  }

  /* ---------------------------------------------------------------
     Action bar: one primary on the right, one text secondary on the left.
     --------------------------------------------------------------- */
  .wizard-actions {
    align-items: center;
    border-top: 1px solid var(--console-hairline);
    display: flex;
    gap: var(--sl-spacing-small);
    justify-content: flex-end;
    padding-top: var(--sl-spacing-medium);
  }

  .wizard-actions .wizard-back {
    margin-right: auto;
  }

  /* ---------------------------------------------------------------
     Live connection status.
     --------------------------------------------------------------- */
  .conn-status {
    display: flex;
    flex-direction: column;
    gap: var(--sl-spacing-2x-small);
    width: 100%;
  }

  .conn-waiting {
    align-items: center;
    color: var(--console-meta-color);
    display: flex;
    font-size: var(--console-text-meta);
    gap: var(--sl-spacing-small);
  }

  .conn-waiting sl-spinner {
    font-size: 0.875rem;
  }

  .conn-connected {
    align-items: center;
    color: var(--sl-color-success-700);
    display: flex;
    font-size: var(--console-text-meta);
    font-weight: 600;
    gap: var(--sl-spacing-x-small);
  }

  .cli-connected-line {
    color: var(--sl-color-success-700);
    font-size: var(--console-text-meta);
    font-weight: 600;
  }

  .cli-connected-link {
    color: var(--console-link-color);
    display: inline-block;
    font-size: var(--console-text-meta);
  }

  @media (max-width: 640px) {
    /* Phones get one full-width primary, with the secondary under it. The
       DOM order stays secondary-then-primary so the keyboard reaches the
       action it is most likely to want last. */
    .wizard-actions {
      flex-direction: column-reverse;
      align-items: stretch;
    }

    .wizard-actions .wizard-back {
      margin-right: 0;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .wizard-option-button {
      transition: none;
    }
  }
`;
