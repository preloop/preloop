/** Human labels for provider subscription states, including future states. */
export function billingStatusLabel(status: string | null | undefined): string {
  if (!status) return 'Free';
  const labels: Record<string, string> = {
    trialing: 'Trial',
    active: 'Active',
    past_due: 'Payment overdue',
    pending_cancellation: 'Pending cancellation',
    canceled: 'Cancelled',
    unpaid: 'Payment required',
    incomplete: 'Payment pending',
    incomplete_expired: 'Payment expired',
  };
  if (labels[status]) return labels[status];
  const words = status.replace(/[_-]+/g, ' ');
  return words.charAt(0).toUpperCase() + words.slice(1);
}
