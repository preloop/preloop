/**
 * Unified WebSocket Manager
 *
 * Provides a single persistent WebSocket connection with:
 * - Automatic reconnection with exponential backoff
 * - Message routing via pub/sub pattern
 * - Activity tracking
 * - Support for authenticated and anonymous users
 * - Connection state management
 */

import { MessageRouter } from './message-router';
import { getVisitorId } from './visitor-id';

export enum ConnectionState {
  DISCONNECTED = 'disconnected',
  CONNECTING = 'connecting',
  CONNECTED = 'connected',
  RECONNECTING = 'reconnecting',
  FAILED = 'failed',
}

// Reconnection configuration
const INITIAL_RETRY_DELAY = 1000; // 1 second
const MAX_RETRY_DELAY = 30000; // 30 seconds
const MAX_RETRIES = Infinity; // Never give up

export class UnifiedWebSocketManager {
  private ws: WebSocket | null = null;
  private state: ConnectionState = ConnectionState.DISCONNECTED;
  private stateListeners: Set<(state: ConnectionState) => void> = new Set();
  private router: MessageRouter;
  private retryCount = 0;
  private retryTimeout: number | null = null;
  private heartbeatInterval: number | null = null;
  private sessionId: string | null = null;

  private authChangeHandler = (): void => {
    if (this.state === ConnectionState.CONNECTED) {
      const token = localStorage.getItem('accessToken');
      if (token) {
        this.send({ type: 'authenticate', token });
        console.log('Sent authentication message (auth-change)');
      }
    }
  };

  constructor() {
    this.router = new MessageRouter();
    if (typeof window !== 'undefined') {
      window.addEventListener('auth-change', this.authChangeHandler);
    }
  }

  /**
   * Get current connection state.
   */
  getState(): ConnectionState {
    return this.state;
  }

  /**
   * Register a listener for connection state changes.
   *
   * @param callback - Function called when state changes
   * @returns Unsubscribe function
   */
  onStateChange(callback: (state: ConnectionState) => void): () => void {
    this.stateListeners.add(callback);
    return () => this.stateListeners.delete(callback);
  }

  /**
   * Subscribe to messages on a specific topic.
   *
   * @param topic - Topic to subscribe to
   * @param callback - Function called when message arrives
   * @param filter - Optional filter function
   * @returns Unsubscribe function
   */
  subscribe(
    topic: string,
    callback: (message: any) => void,
    filter?: (message: any) => boolean
  ): () => void {
    const previousCount = this.router.getSubscriptionCount(topic);
    const unsubscribe = this.router.subscribe(topic, callback, filter);

    if (previousCount === 0) {
      this.sendServerSubscription(topic, 'subscribe');
    }

    return () => {
      unsubscribe();
      if (this.router.getSubscriptionCount(topic) === 0) {
        this.sendServerSubscription(topic, 'unsubscribe');
      }
    };
  }

  /**
   * Connect to the unified WebSocket endpoint.
   *
   * Uses message-based authentication: connects first, then sends auth message.
   * This avoids exposing tokens in URLs (more secure than query params).
   */
  async connect(): Promise<void> {
    if (
      this.state === ConnectionState.CONNECTING ||
      this.state === ConnectionState.CONNECTED
    ) {
      console.log('WebSocket already connecting or connected');
      return;
    }

    this.setState(ConnectionState.CONNECTING);

    try {
      // Determine protocol
      const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
      const host = window.location.host;

      // Build URL with anonymous identifiers only (no token in URL)
      const params = new URLSearchParams();
      const fingerprint = await this.getBrowserFingerprint();
      params.append('fingerprint', fingerprint);
      params.append('visitor_id', getVisitorId());

      const url = `${protocol}://${host}/api/v1/ws/unified?${params.toString()}`;

      // Create WebSocket connection
      this.ws = new WebSocket(url);

      // Setup event handlers
      this.ws.onopen = this.handleOpen.bind(this);
      this.ws.onmessage = this.handleMessage.bind(this);
      this.ws.onclose = this.handleClose.bind(this);
      this.ws.onerror = this.handleError.bind(this);
    } catch (error) {
      console.error('Failed to create WebSocket connection:', error);
      this.scheduleReconnect();
    }
  }

  /**
   * Clean up global event listeners. Call when the manager is no longer needed
   * (e.g., during testing or when the class is instantiated multiple times).
   */
  destroy(): void {
    if (typeof window !== 'undefined') {
      window.removeEventListener('auth-change', this.authChangeHandler);
    }
    this.disconnect();
  }

  /**
   * Disconnect from WebSocket.
   *
   * @param code - Close code (1000 = normal closure)
   */
  disconnect(code: number = 1000): void {
    // Cancel any pending reconnection
    if (this.retryTimeout !== null) {
      clearTimeout(this.retryTimeout);
      this.retryTimeout = null;
    }

    // Stop heartbeat
    this.stopHeartbeat();

    // Close WebSocket
    if (this.ws) {
      this.ws.close(code);
      this.ws = null;
    }

    this.setState(ConnectionState.DISCONNECTED);
  }

  /**
   * Send a message through the WebSocket.
   *
   * @param data - Data to send
   * @returns true if sent successfully, false otherwise
   */
  send(data: any): boolean {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(data));
      return true;
    }

    console.warn('Cannot send message: WebSocket not connected');
    return false;
  }

  /**
   * Handle WebSocket open event.
   */
  private handleOpen(): void {
    console.log('Unified WebSocket connected');

    // Reset retry count on successful connection
    this.retryCount = 0;

    this.setState(ConnectionState.CONNECTED);

    // Authenticate via message if we have a token
    const token = localStorage.getItem('accessToken');
    if (token) {
      this.send({ type: 'authenticate', token });
      console.log('Sent authentication message');
    }

    this.syncServerSubscriptions();

    // Start heartbeat
    this.startHeartbeat();
  }

  /**
   * Handle incoming WebSocket message.
   */
  private handleMessage(event: MessageEvent): void {
    try {
      const message = JSON.parse(event.data);

      // Handle handshake
      if (message.type === 'handshake') {
        this.sessionId = message.session_id;
        console.log(
          `Session established: ${this.sessionId}, authenticated: ${message.authenticated}`
        );
        return;
      }

      // Handle authentication response
      if (message.type === 'authenticated') {
        console.log(`Authenticated as: ${message.user?.username}`);
        this.syncServerSubscriptions();
        // Route to subscribers so they can react (e.g., refresh data)
        this.router.route(message);
        return;
      }

      if (message.type === 'auth_error') {
        console.warn(`Authentication failed: ${message.error}`);
        // Do not clear the token here - let HTTP requests handle token refresh.
        // Once refreshed, they will dispatch 'auth-change' which this WS listens to.
        return;
      }

      // Handle subscription acknowledgements internally to prevent triggering redundant view updates
      if (message.type === 'subscription_ack') {
        console.log(
          `[UnifiedWebSocketManager] Subscribed to topic: ${message.topic}`
        );
        return;
      }

      // Handle ping
      if (message.type === 'ping') {
        this.send({ type: 'pong' });
        return;
      }

      // Route message to subscribers
      this.router.route(message);
    } catch (error) {
      console.error('Failed to process WebSocket message:', error);
    }
  }

  /**
   * Handle WebSocket close event.
   */
  private handleClose(event: CloseEvent): void {
    console.log(`WebSocket closed: code=${event.code}, reason=${event.reason}`);

    this.stopHeartbeat();
    this.ws = null;

    // Only reconnect if not a normal closure
    if (event.code !== 1000) {
      this.setState(ConnectionState.RECONNECTING);
      this.scheduleReconnect();
    } else {
      this.setState(ConnectionState.DISCONNECTED);
    }
  }

  /**
   * Handle WebSocket error event.
   */
  private handleError(event: Event): void {
    console.error('WebSocket error:', event);
    // Error will be followed by close event, which handles reconnection
  }

  /**
   * Schedule a reconnection attempt with exponential backoff.
   */
  private scheduleReconnect(): void {
    if (this.retryCount >= MAX_RETRIES) {
      console.error('Max reconnection attempts reached');
      this.setState(ConnectionState.FAILED);
      return;
    }

    const delay = this.calculateRetryDelay(this.retryCount);
    console.log(`Reconnecting in ${delay}ms (attempt ${this.retryCount + 1})`);

    this.retryTimeout = window.setTimeout(() => {
      this.retryCount++;
      this.connect();
    }, delay);
  }

  /**
   * Calculate retry delay with exponential backoff and jitter.
   */
  private calculateRetryDelay(retryCount: number): number {
    const exponentialDelay = Math.min(
      INITIAL_RETRY_DELAY * Math.pow(2, retryCount),
      MAX_RETRY_DELAY
    );

    // Add jitter (±25%)
    const jitter = exponentialDelay * 0.25 * (Math.random() - 0.5);

    return exponentialDelay + jitter;
  }

  /**
   * Start heartbeat to keep connection alive.
   */
  private startHeartbeat(): void {
    // Send pong every 30 seconds as keepalive
    this.heartbeatInterval = window.setInterval(() => {
      if (this.ws?.readyState === WebSocket.OPEN) {
        this.send({ type: 'pong' });
      }
    }, 30000);
  }

  /**
   * Stop heartbeat interval.
   */
  private stopHeartbeat(): void {
    if (this.heartbeatInterval !== null) {
      clearInterval(this.heartbeatInterval);
      this.heartbeatInterval = null;
    }
  }

  /**
   * Update connection state and notify listeners.
   */
  private setState(newState: ConnectionState): void {
    if (this.state !== newState) {
      this.state = newState;
      this.stateListeners.forEach((listener) => {
        try {
          listener(newState);
        } catch (error) {
          console.error('Error in state change listener:', error);
        }
      });
    }
  }

  private sendServerSubscription(
    topic: string,
    action: 'subscribe' | 'unsubscribe'
  ): void {
    if (topic === '*' || topic === 'system') {
      return;
    }
    this.send({ type: action, topic });
  }

  private syncServerSubscriptions(): void {
    for (const topic of this.router.listTopics()) {
      this.sendServerSubscription(topic, 'subscribe');
    }
  }

  /**
   * Get or generate browser fingerprint for anonymous user tracking.
   */
  private async getBrowserFingerprint(): Promise<string> {
    // Check if we already have a fingerprint
    let fingerprint = localStorage.getItem('browserFingerprint');

    if (!fingerprint) {
      // Generate fingerprint from browser characteristics
      const components = [
        navigator.userAgent,
        navigator.language,
        screen.colorDepth,
        screen.width,
        screen.height,
        new Date().getTimezoneOffset(),
        !!window.sessionStorage,
        !!window.localStorage,
      ];

      // Create hash from components
      const componentString = components.join('|');
      fingerprint = await this.simpleHash(componentString);

      // Store for future use
      localStorage.setItem('browserFingerprint', fingerprint);
    }

    return fingerprint;
  }

  /**
   * Simple hash function for browser fingerprint.
   */
  private async simpleHash(str: string): Promise<string> {
    const encoder = new TextEncoder();
    const data = encoder.encode(str);
    const hashBuffer = await crypto.subtle.digest('SHA-256', data);
    const hashArray = Array.from(new Uint8Array(hashBuffer));
    const hashHex = hashArray
      .map((b) => b.toString(16).padStart(2, '0'))
      .join('');
    return hashHex.substring(0, 32); // First 32 chars
  }
}

// Global singleton instance
export const unifiedWebSocketManager = new UnifiedWebSocketManager();
