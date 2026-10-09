/** Captured resolved-model attribution; older rows remain unknown. */
export function billingAttribution(
  data: Record<string, unknown> | null | undefined
): string {
  if (data?.billing_path === 'allowance') return 'Billed to: allowance';
  if (data?.billing_path === 'your_key') {
    const name =
      typeof data.billing_model_name === 'string'
        ? data.billing_model_name
        : 'model';
    const id =
      typeof data.billing_model_id === 'string' ? data.billing_model_id : '';
    return `Billed to: your key (${name}${id ? ` · ${id}` : ''})`;
  }
  return 'Billing path not recorded';
}
