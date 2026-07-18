/**
 * Message Router with Pub/Sub pattern for WebSocket messages.
 *
 * Allows components to subscribe to specific message topics and
 * automatically routes incoming messages to interested subscribers.
 */

export interface Subscription {
  topic: string;
  filter?: (message: any) => boolean;
  callback: (message: any) => void;
}

export class MessageRouter {
  private subscriptions: Map<string, Set<Subscription>> = new Map();

  /**
   * Subscribe to a specific topic with optional filter.
   *
   * @param topic - Message topic to subscribe to (e.g., 'flow_executions', 'approvals')
   * @param callback - Function called when matching message arrives
   * @param filter - Optional filter function to further refine which messages are delivered
   * @returns Unsubscribe function
   */
  subscribe(
    topic: string,
    callback: (message: any) => void,
    filter?: (message: any) => boolean
  ): () => void {
    const subscription: Subscription = { topic, callback, filter };

    if (!this.subscriptions.has(topic)) {
      this.subscriptions.set(topic, new Set());
    }

    this.subscriptions.get(topic)!.add(subscription);

    // Return unsubscribe function
    return () => {
      this.subscriptions.get(topic)?.delete(subscription);
    };
  }

  /**
   * Route a message to all interested subscribers.
   *
   * @param message - The message to route
   */
  route(message: any): void {
    const topic = this.extractTopic(message);

    if (!topic) {
      console.warn(
        '[MessageRouter] Message without identifiable topic:',
        message
      );
      return;
    }

    // Debug logging for device registration and approval events
    if (topic === 'device_registered' || topic === 'approvals') {
      console.log(`[MessageRouter] Routing ${message.type} to topic=${topic}`);
    }

    // Notify topic-specific subscribers
    const topicSubscribers = this.subscriptions.get(topic) || new Set();
    if (topic === 'device_registered' || topic === 'approvals') {
      console.log(
        `[MessageRouter] ${topicSubscribers.size} subscriber(s) for topic ${topic}`
      );
    }

    topicSubscribers.forEach((sub) => {
      if (!sub.filter || sub.filter(message)) {
        try {
          sub.callback(message);
        } catch (error) {
          console.error(
            '[MessageRouter] Error in callback for topic',
            topic,
            error
          );
        }
      }
    });

    // Notify wildcard subscribers (topic: '*')
    const wildcardSubscribers = this.subscriptions.get('*') || new Set();
    wildcardSubscribers.forEach((sub) => {
      if (!sub.filter || sub.filter(message)) {
        try {
          sub.callback(message);
        } catch (error) {
          console.error('Error in wildcard subscription callback:', error);
        }
      }
    });
  }

  /**
   * Extract topic from message based on message type.
   *
   * @param message - Message to extract topic from
   * @returns Topic string or null if unable to determine
   */
  private extractTopic(message: any): string | null {
    if (typeof message.topic === 'string' && message.topic.length > 0) {
      return message.topic;
    }

    const messageType = message.type;

    if (!messageType) {
      return null;
    }

    // Approval-related messages
    if (messageType.startsWith('approval_')) {
      return 'approvals';
    }

    if (
      messageType === 'runtime_session_created' ||
      messageType === 'runtime_session_updated' ||
      messageType === 'runtime_session_ended'
    ) {
      return 'runtime_sessions';
    }

    if (
      messageType === 'managed_agent_created' ||
      messageType === 'managed_agent_updated' ||
      messageType === 'managed_agent_suspended' ||
      messageType === 'managed_agent_resumed' ||
      messageType === 'managed_agent_decommissioned'
    ) {
      return 'managed_agents';
    }

    if (messageType === 'budget_health_updated') {
      return 'budget_health';
    }

    if (messageType === 'audit_event') {
      return 'audit';
    }

    // Flow execution messages
    if (
      messageType === 'execution_started' ||
      messageType === 'status_update' ||
      messageType === 'agent_log_line' ||
      messageType === 'execution_completed' ||
      messageType === 'execution_failed' ||
      messageType === 'model_gateway_call' ||
      messageType === 'tool_call' ||
      messageType === 'mcp_call' ||
      messageType === 'tool_calls_update' ||
      messageType === 'token_usage_update' ||
      messageType === 'budget_update' ||
      messageType === 'model_output' ||
      messageType === 'agent_started' ||
      messageType === 'agent_stopped' ||
      messageType === 'connected'
    ) {
      return 'flow_executions';
    }

    // Activity updates (for admin)
    if (messageType === 'activity_update') {
      return 'activity';
    }

    // System messages
    if (
      messageType === 'ping' ||
      messageType === 'pong' ||
      messageType === 'handshake' ||
      messageType === 'authenticated'
    ) {
      return 'system';
    }

    // Default: use message type as topic
    return messageType;
  }

  /**
   * Get count of active subscriptions for a topic.
   *
   * @param topic - Topic to count subscriptions for
   * @returns Number of active subscriptions
   */
  getSubscriptionCount(topic: string): number {
    return this.subscriptions.get(topic)?.size || 0;
  }

  /**
   * List topics with at least one active subscription.
   */
  listTopics(): string[] {
    return [...this.subscriptions.entries()]
      .filter(([, subscriptions]) => subscriptions.size > 0)
      .map(([topic]) => topic);
  }

  /**
   * Clear all subscriptions for a topic.
   *
   * @param topic - Topic to clear
   */
  clearTopic(topic: string): void {
    this.subscriptions.delete(topic);
  }

  /**
   * Clear all subscriptions.
   */
  clearAll(): void {
    this.subscriptions.clear();
  }
}
