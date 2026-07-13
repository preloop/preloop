/**
 * Ensures a datetime string from the backend is treated as UTC.
 *
 * Backend datetime strings may not include timezone information.
 * This function ensures they are correctly parsed as UTC by:
 * 1. Checking if timezone info is already present
 * 2. Converting space-separated format to ISO format (YYYY-MM-DD HH:MM:SS -> YYYY-MM-DDTHH:MM:SS)
 * 3. Appending 'Z' to indicate UTC if no timezone info exists
 *
 * @param dateTimeString - Datetime string from backend (e.g., "2025-11-21 12:00:00" or "2025-11-21T12:00:00Z")
 * @returns Date object with correct UTC interpretation
 */
export function parseUTCDate(dateTimeString: string): Date {
  if (!dateTimeString) {
    return new Date();
  }

  let utcDateString = dateTimeString.trim();

  // Check if timezone info is already present
  const hasTimezone =
    utcDateString.endsWith('Z') ||
    utcDateString.includes('+') ||
    utcDateString.includes('-', 10); // Check for +/- after position 10 (after "YYYY-MM-DD")

  if (!hasTimezone) {
    // Replace space with 'T' for ISO format
    utcDateString = utcDateString.replace(' ', 'T');

    // Append 'Z' to indicate UTC
    utcDateString += 'Z';
  }

  return new Date(utcDateString);
}

/**
 * Formats a datetime string for display in the user's local timezone.
 *
 * @param dateTimeString - Datetime string from backend
 * @returns Formatted string in user's local timezone (e.g., "12:30:45 PM")
 */
export function formatLocalTime(dateTimeString: string): string {
  return parseUTCDate(dateTimeString).toLocaleTimeString();
}

/**
 * Formats a datetime string for display in the user's local timezone with date.
 *
 * @param dateTimeString - Datetime string from backend
 * @returns Formatted string in user's local timezone (e.g., "11/21/2025, 12:30:45 PM")
 */
export function formatLocalDateTime(dateTimeString: string): string {
  return parseUTCDate(dateTimeString).toLocaleString();
}

/**
 * Formats a datetime string explicitly as UTC.
 *
 * @param dateTimeString - Datetime string from backend
 * @returns Formatted string as "YYYY-MM-DD HH:MM:SS UTC"
 */
export function formatUTCDateTime(dateTimeString: string): string {
  const date = parseUTCDate(dateTimeString);

  const year = date.getUTCFullYear();
  const month = String(date.getUTCMonth() + 1).padStart(2, '0');
  const day = String(date.getUTCDate()).padStart(2, '0');
  const hours = String(date.getUTCHours()).padStart(2, '0');
  const minutes = String(date.getUTCMinutes()).padStart(2, '0');
  const seconds = String(date.getUTCSeconds()).padStart(2, '0');

  return `${year}-${month}-${day} ${hours}:${minutes}:${seconds} UTC`;
}

/**
 * Calculates relative time from now (e.g., "2m ago", "3h ago", "5d ago").
 *
 * @param dateTimeString - Datetime string from backend
 * @returns Relative time string
 */
export function formatRelativeTime(
  dateTimeString: string,
  now: Date = new Date()
): string {
  const date = parseUTCDate(dateTimeString);
  const diffMs = now.getTime() - date.getTime();
  const diffMins = Math.floor(diffMs / 60000);
  const diffHours = Math.floor(diffMs / 3600000);
  const diffDays = Math.floor(diffMs / 86400000);

  if (diffMins < 1) return 'just now';
  if (diffMins < 60) return `${diffMins}m ago`;
  if (diffHours < 24) return `${diffHours}h ago`;
  if (diffDays < 7) return `${diffDays}d ago`;
  return date.toLocaleDateString();
}

/**
 * Calculates relative time until a future datetime (e.g., "in 2m", "in 3h").
 *
 * @param dateTimeString - Datetime string from backend
 * @returns Relative future time string
 */
export function formatFutureRelativeTime(
  dateTimeString: string,
  now: Date = new Date()
): string {
  const date = parseUTCDate(dateTimeString);
  const diffMs = date.getTime() - now.getTime();
  const diffMins = Math.ceil(diffMs / 60000);
  const diffHours = Math.ceil(diffMs / 3600000);
  const diffDays = Math.ceil(diffMs / 86400000);

  if (diffMins <= 0) return 'expired';
  if (diffMins < 60) return `in ${diffMins}m`;
  if (diffHours < 24) return `in ${diffHours}h`;
  if (diffDays < 7) return `in ${diffDays}d`;
  return date.toLocaleDateString();
}

/**
 * Calculates duration between two datetime strings.
 *
 * @param startTimeString - Start datetime string from backend
 * @param endTimeString - End datetime string from backend
 * @returns Duration string (e.g., "2h 15m", "45m 30s", "25s")
 */
export function calculateDuration(
  startTimeString: string,
  endTimeString: string
): string {
  const start = parseUTCDate(startTimeString);
  const end = parseUTCDate(endTimeString);
  const durationMs = end.getTime() - start.getTime();
  const seconds = Math.floor(durationMs / 1000);
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);

  if (hours > 0) return `${hours}h ${minutes % 60}m`;
  if (minutes > 0) return `${minutes}m ${seconds % 60}s`;
  return `${seconds}s`;
}
