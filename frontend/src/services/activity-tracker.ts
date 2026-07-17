/**
 * Activity Tracker
 *
 * Tracks user activity and sends it through the unified WebSocket connection:
 * - Page views
 * - User actions (clicks, form submits)
 * - Conversion events
 */

import {
  unifiedWebSocketManager,
  ConnectionState,
} from './unified-websocket-manager';
import { getVisitorId } from './visitor-id';
import { getAttribution } from './attribution';

export class ActivityTracker {
  private enabled = true;
  private messageQueue: any[] = [];
  private isProcessingQueue = false;

  constructor() {
    // When WebSocket connects, announce the session (each reconnect is a new
    // server-side session, so the hello re-sends every time), then flush any
    // queued messages.
    unifiedWebSocketManager.onStateChange((state) => {
      if (state === ConnectionState.CONNECTED) {
        this.sendSessionHello();
        this.flushQueue();
      }
    });
  }

  /**
   * Send the once-per-session device/attribution blob (`session_hello`).
   *
   * Carries the visitor id plus coarse device configuration (screen,
   * languages, timezone — no canvas/audio fingerprinting) and the session's
   * first-touch attribution. Persisted server-side into the session_start
   * event's data by the analytics pipeline.
   */
  private sendSessionHello(): void {
    if (!this.enabled) return;

    const message: Record<string, any> = {
      type: 'activity',
      event: 'session_hello',
      visitor_id: getVisitorId(),
      device: {
        screen_width: window.screen?.width,
        screen_height: window.screen?.height,
        pixel_ratio: window.devicePixelRatio,
        viewport_width: window.innerWidth,
        viewport_height: window.innerHeight,
        languages: Array.from(navigator.languages || []).slice(0, 8),
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
        platform: navigator.platform,
        touch: (navigator.maxTouchPoints || 0) > 0,
        device_memory: (navigator as any).deviceMemory,
      },
      timestamp: Date.now(),
    };
    const attribution = getAttribution();
    if (attribution) {
      message.attribution = attribution;
    }
    // Send directly (not queued): CONNECTED state guarantees an open socket,
    // and a stale hello must not replay into a later session.
    unifiedWebSocketManager.send(message);
  }

  /**
   * Send a message, queuing it if WebSocket isn't connected yet.
   */
  private sendOrQueue(message: any): void {
    const sent = unifiedWebSocketManager.send(message);

    if (!sent) {
      // Queue the message if it couldn't be sent
      this.messageQueue.push(message);
      console.debug('Queued activity message:', message.event);
    }
  }

  /**
   * Flush queued messages when WebSocket connects.
   */
  private flushQueue(): void {
    if (this.isProcessingQueue || this.messageQueue.length === 0) return;

    this.isProcessingQueue = true;
    console.debug(
      `Flushing ${this.messageQueue.length} queued activity messages`
    );

    while (this.messageQueue.length > 0) {
      const message = this.messageQueue.shift();
      if (message) {
        unifiedWebSocketManager.send(message);
      }
    }

    this.isProcessingQueue = false;
  }

  /**
   * Track a page view.
   *
   * @param path - URL path being viewed
   * @param metadata - Optional additional metadata
   */
  trackPageView(path: string, metadata?: Record<string, any>): void {
    if (!this.enabled) return;

    this.sendOrQueue({
      type: 'activity',
      event: 'page_view',
      path: path,
      referrer: document.referrer,
      metadata: metadata || {},
      timestamp: Date.now(),
    });
  }

  /**
   * Track a user action.
   *
   * @param action - Name of the action (e.g., 'click_signup_button')
   * @param metadata - Optional additional metadata
   */
  trackAction(action: string, metadata?: Record<string, any>): void {
    if (!this.enabled) return;

    this.sendOrQueue({
      type: 'activity',
      event: 'action',
      action: action,
      metadata: metadata || {},
      timestamp: Date.now(),
    });
  }

  /**
   * Track a conversion event.
   *
   * @param event - Conversion event name (e.g., 'Signup', 'Login')
   * @param value - Optional monetary value
   * @param metadata - Optional additional metadata (persisted to event_data)
   */
  trackConversion(
    event: string,
    value?: number,
    metadata?: Record<string, any>
  ): void {
    if (!this.enabled) return;

    this.sendOrQueue({
      type: 'activity',
      event: 'conversion',
      conversion_event: event,
      value: value,
      metadata: metadata || {},
      timestamp: Date.now(),
    });
  }

  /**
   * Enable activity tracking.
   */
  enable(): void {
    this.enabled = true;
  }

  /**
   * Disable activity tracking.
   */
  disable(): void {
    this.enabled = false;
  }

  /**
   * Check if tracking is enabled.
   */
  isEnabled(): boolean {
    return this.enabled;
  }

  /**
   * Initialize automatic tracking of clicks on elements with data-track attribute.
   */
  initializeAutoTracking(): void {
    // Track clicks on elements with data-track attribute
    document.addEventListener('click', (e: MouseEvent) => {
      const target = e.target as HTMLElement;
      const trackElement = target.closest('[data-track]') as HTMLElement;

      if (trackElement) {
        const action = trackElement.getAttribute('data-track');
        if (action) {
          this.trackAction(action, {
            element: trackElement.tagName.toLowerCase(),
            text: trackElement.textContent?.trim().substring(0, 100),
            href: trackElement.getAttribute('href'),
          });
        }
      }
    });

    // Track form submissions with data-track-form
    document.addEventListener('submit', (e: SubmitEvent) => {
      const form = e.target as HTMLFormElement;
      const action = form.getAttribute('data-track-form');

      if (action) {
        this.trackAction(action, {
          element: 'form',
          action: form.action,
          method: form.method,
        });
      }
    });
  }
}

// Global singleton instance
export const activityTracker = new ActivityTracker();
