import type { ReactiveController, ReactiveControllerHost } from 'lit';
import { LOCATION_CHANGED } from '../router';

/** Keep report-filtered lists in the existing view, including browser history. */
export class WeeklyReportMode implements ReactiveController {
  active = new URLSearchParams(window.location.search).has('kind');
  private readonly restore = (): void => {
    const previous = this.active;
    this.active = new URLSearchParams(window.location.search).has('kind');
    if (previous && !this.active) this.onExit?.();
    this.host.requestUpdate();
  };

  constructor(
    private readonly host: ReactiveControllerHost,
    private readonly onExit?: () => void
  ) {
    host.addController(this);
  }

  hostConnected(): void {
    this.restore();
    window.addEventListener('popstate', this.restore);
    window.addEventListener(LOCATION_CHANGED, this.restore);
  }

  hostDisconnected(): void {
    window.removeEventListener('popstate', this.restore);
    window.removeEventListener(LOCATION_CHANGED, this.restore);
  }
}
