/**
 * Console client for the audit chain, signing keys, retention and evidence.
 *
 * Response shapes follow openapi.yaml. Binary downloads are a single request:
 * a long export is not retried.
 */
import { extractErrorMessage, fetchWithAuth } from './api';
import { filenameFromDisposition } from './utils/records-format';

export interface ChainCheckpoint {
  seq: number;
  chain_hash: string;
  row_count: number;
  checkpointed_at: string;
  signing_key_id: string | null;
  signature: string | null;
  signed_payload: Record<string, unknown>;
  digest: string;
  signature_document: Record<string, unknown> | null;
}

export interface ChainStatus {
  enabled: boolean;
  head_seq: number;
  head_hash: string;
  last_sealed_at: string | null;
  pruned_below_seq: number;
  sealed_rows: number;
  unsealed_rows: number;
  seal_lag_seconds: number;
  checkpoint_interval: number;
  latest_checkpoint: ChainCheckpoint | null;
  active_key_id: string | null;
}

export interface ChainBreak {
  kind: string;
  seq: number;
  row_id: string | null;
  detail: string;
}

export interface ChainVerifyResult {
  account_id: string;
  status: string;
  checked_rows: number;
  start_seq: number;
  end_seq: number;
  head_seq: number;
  pruned_below_seq: number;
  unsealed_rows: number;
  truncated: boolean;
  out_of_order_rows: number;
  first_break: ChainBreak | null;
  checkpoints_verified: number;
  checkpoint_failures: Record<string, unknown>[];
}

export interface ChainSegmentEntry {
  seq: number;
  row_id: string;
  prev_hash: string | null;
  row_hash: string | null;
  payload: Record<string, unknown>;
}

export interface ChainSegment {
  account_id: string;
  row_domain: string;
  after_seq: number;
  head_seq: number;
  pruned_below_seq: number;
  genesis_hash: string;
  entries: ChainSegmentEntry[];
  has_more: boolean;
  note: string;
}

export interface SigningKey {
  key_id: string;
  algorithm: string;
  public_key: string;
  active: boolean;
  created_at: string | null;
  retired_at: string | null;
}

export interface SigningKeyList {
  active_key_id: string | null;
  signature_schema: string;
  signed_bytes_format: string;
  keys: SigningKey[];
}

export interface SigningKeyRotateResult {
  retired: SigningKey | null;
  active: SigningKey;
}

export interface RetentionClass {
  record_class: string;
  label: string;
  days: number;
  source: string;
  floored: boolean;
}

export interface RetentionSettings {
  floor_days: number;
  default_days: number;
  max_days: number;
  classes: RetentionClass[];
  purge_enabled: boolean;
  purge_dry_run: boolean;
  purge_window_utc: string | null;
  evidence_payload_hours: number;
}

export interface PurgePreviewClass {
  record_class: string;
  label: string;
  retention_days: number;
  unlimited?: boolean;
  cutoff: string | null;
  purgeable: number;
}

export interface PurgePreview {
  account_id: string;
  purge_enabled: boolean;
  classes: PurgePreviewClass[];
  total: number;
}

export interface LegalHold {
  id: string;
  resource_type: string;
  resource_id: string;
  reason: string;
  placed_by_user_id: string | null;
  placed_at: string | null;
  released_by_user_id: string | null;
  released_at: string | null;
  release_reason: string | null;
  active: boolean;
  flagged?: Record<string, number> | null;
}

export interface EvidenceStatus {
  version?: number;
  kind?: string;
  status: string;
  transport?: string;
  execution_id?: string | null;
  artifact_id?: string | null;
  sha256?: string | null;
  digest?: string | null;
  size_bytes?: number | null;
  expanded_bytes?: number | null;
  created_at?: string | null;
  expires_at?: string | null;
  object_lock: boolean;
  legal_hold: boolean;
  integrity?: string | null;
  integrity_note?: string | null;
  integrity_verified?: boolean;
  error?: string | null;
  signature?: Record<string, unknown> | null;
  signing_key_id?: string | null;
}

export interface BinaryDownload {
  blob: Blob;
  filename: string;
  sizeBytes: number;
  headers: {
    signature: string | null;
    signingKeyId: string | null;
    signedAt: string | null;
    archiveSha256: string | null;
    membersDigest: string | null;
    manifestSha256: string | null;
    evidenceIntegrity: string | null;
    evidenceIntegrityState: string | null;
    evidenceSha256: string | null;
  };
}

async function fail(response: Response, fallback: string): Promise<never> {
  const body = await response.json().catch(() => null);
  throw new Error(extractErrorMessage(body, fallback));
}

export async function getAuditChainStatus(): Promise<ChainStatus> {
  const response = await fetchWithAuth('/api/v1/audit/chain/status');
  if (!response.ok) {
    return fail(response, 'Could not load audit chain status');
  }
  return response.json();
}

export async function verifyAuditChain(params: {
  startSeq?: number;
  endSeq?: number;
  maxRows?: number;
}): Promise<ChainVerifyResult> {
  const query = new URLSearchParams();
  if (params.startSeq != null) {
    query.set('start_seq', String(params.startSeq));
  }
  if (params.endSeq != null) {
    query.set('end_seq', String(params.endSeq));
  }
  if (params.maxRows != null) {
    query.set('max_rows', String(params.maxRows));
  }
  const suffix = query.toString() ? `?${query.toString()}` : '';
  const response = await fetchWithAuth(`/api/v1/audit/chain/verify${suffix}`);
  if (!response.ok) {
    return fail(response, 'Could not verify the audit chain');
  }
  return response.json();
}

export async function getAuditChainSegment(params?: {
  afterSeq?: number;
  limit?: number;
}): Promise<ChainSegment> {
  const query = new URLSearchParams();
  if (params?.afterSeq != null) {
    query.set('after_seq', String(params.afterSeq));
  }
  if (params?.limit != null) {
    query.set('limit', String(params.limit));
  }
  const suffix = query.toString() ? `?${query.toString()}` : '';
  const response = await fetchWithAuth(`/api/v1/audit/chain/segment${suffix}`);
  if (!response.ok) {
    return fail(response, 'Could not load the audit chain segment');
  }
  return response.json();
}

export async function listAuditChainCheckpoints(params?: {
  afterSeq?: number;
  limit?: number;
}): Promise<ChainCheckpoint[]> {
  const query = new URLSearchParams();
  if (params?.afterSeq != null) {
    query.set('after_seq', String(params.afterSeq));
  }
  if (params?.limit != null) {
    query.set('limit', String(params.limit));
  }
  const suffix = query.toString() ? `?${query.toString()}` : '';
  const response = await fetchWithAuth(
    `/api/v1/audit/chain/checkpoints${suffix}`
  );
  if (!response.ok) {
    return fail(response, 'Could not load checkpoints');
  }
  return response.json();
}

export async function listSigningKeys(): Promise<SigningKeyList> {
  const response = await fetchWithAuth('/api/v1/signing/keys');
  if (!response.ok) {
    return fail(response, 'Could not load signing keys');
  }
  return response.json();
}

export async function rotateSigningKey(): Promise<SigningKeyRotateResult> {
  const response = await fetchWithAuth('/api/v1/signing/keys/rotate', {
    method: 'POST',
  });
  if (!response.ok) {
    return fail(response, 'Could not rotate the signing key');
  }
  return response.json();
}

export async function getRetentionSettings(): Promise<RetentionSettings> {
  const response = await fetchWithAuth('/api/v1/retention/settings');
  if (!response.ok) {
    return fail(response, 'Could not load retention settings');
  }
  return response.json();
}

export async function updateRetentionSettings(
  classes: Record<string, number | null>
): Promise<RetentionSettings> {
  const response = await fetchWithAuth('/api/v1/retention/settings', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ classes }),
  });
  if (!response.ok) {
    return fail(response, 'Could not save retention settings');
  }
  return response.json();
}

export async function previewRetentionPurge(): Promise<PurgePreview> {
  const response = await fetchWithAuth('/api/v1/retention/purge-preview');
  if (!response.ok) {
    return fail(response, 'Could not preview the purge');
  }
  return response.json();
}

export async function listLegalHolds(params?: {
  activeOnly?: boolean;
  resourceType?: string;
  resourceId?: string;
  limit?: number;
}): Promise<LegalHold[]> {
  const query = new URLSearchParams();
  if (params?.activeOnly != null) {
    query.set('active_only', String(params.activeOnly));
  }
  if (params?.resourceType) {
    query.set('resource_type', params.resourceType);
  }
  if (params?.resourceId) {
    query.set('resource_id', params.resourceId);
  }
  if (params?.limit != null) {
    query.set('limit', String(params.limit));
  }
  const suffix = query.toString() ? `?${query.toString()}` : '';
  const response = await fetchWithAuth(`/api/v1/retention/holds${suffix}`);
  if (!response.ok) {
    return fail(response, 'Could not load legal holds');
  }
  return response.json();
}

export async function createLegalHold(body: {
  resource_type: string;
  resource_id: string;
  reason: string;
}): Promise<LegalHold> {
  const response = await fetchWithAuth('/api/v1/retention/holds', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    return fail(response, 'Could not place the legal hold');
  }
  return response.json();
}

export async function releaseLegalHold(
  holdId: string,
  reason: string
): Promise<LegalHold> {
  const response = await fetchWithAuth(
    `/api/v1/retention/holds/${holdId}/release`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason }),
    }
  );
  if (!response.ok) {
    return fail(response, 'Could not release the legal hold');
  }
  return response.json();
}

function readDownloadHeaders(response: Response): BinaryDownload['headers'] {
  return {
    signature: response.headers.get('X-Preloop-Signature'),
    signingKeyId: response.headers.get('X-Preloop-Signing-Key-Id'),
    signedAt: response.headers.get('X-Preloop-Signed-At'),
    archiveSha256: response.headers.get('X-Preloop-Archive-Sha256'),
    membersDigest: response.headers.get('X-Preloop-Members-Digest'),
    manifestSha256: response.headers.get('X-Preloop-Manifest-Sha256'),
    evidenceIntegrity: response.headers.get('X-Preloop-Evidence-Integrity'),
    evidenceIntegrityState: response.headers.get(
      'X-Preloop-Evidence-Integrity-State'
    ),
    evidenceSha256: response.headers.get('X-Preloop-Evidence-SHA256'),
  };
}

async function readBinary(
  response: Response,
  fallbackName: string,
  failure: string
): Promise<BinaryDownload> {
  if (!response.ok) {
    return fail(response, failure);
  }
  const blob = await response.blob();
  return {
    blob,
    filename: filenameFromDisposition(
      response.headers.get('Content-Disposition'),
      fallbackName
    ),
    sizeBytes: blob.size,
    headers: readDownloadHeaders(response),
  };
}

/** One request. Callers must not retry a failed or slow export. */
export async function createPeriodExport(
  start: string,
  end: string
): Promise<BinaryDownload> {
  const query = new URLSearchParams({ start, end });
  const response = await fetchWithAuth(
    `/api/v1/retention/exports?${query.toString()}`,
    { method: 'POST' }
  );
  return readBinary(
    response,
    'period-export.tar.gz',
    'Could not export the period'
  );
}

export async function getEvidenceStatus(
  executionId: string
): Promise<EvidenceStatus> {
  const response = await fetchWithAuth(
    `/api/v1/flows/executions/${executionId}/evidence-status`
  );
  if (!response.ok) {
    return fail(response, 'Could not load evidence status');
  }
  return response.json();
}

export async function downloadEvidence(
  executionId: string
): Promise<BinaryDownload> {
  const response = await fetchWithAuth(
    `/api/v1/flows/executions/${executionId}/evidence`
  );
  return readBinary(
    response,
    `evidence-${executionId}.tar.gz`,
    'Could not download the evidence pack'
  );
}

export interface EvidenceMember {
  path: string;
  size_bytes: number | null;
  sha256: string | null;
  content_type: string;
}

export interface EvidenceMemberList {
  execution_id: string | null;
  status: string;
  sha256: string | null;
  integrity: string | null;
  integrity_note: string | null;
  legal_hold: boolean;
  members: EvidenceMember[];
}

export async function listEvidenceMembers(
  executionId: string
): Promise<EvidenceMemberList> {
  const response = await fetchWithAuth(
    `/api/v1/flows/executions/${executionId}/evidence/members`
  );
  if (!response.ok) {
    return fail(response, 'Could not list evidence pack members');
  }
  return response.json();
}

export async function readEvidenceMember(
  executionId: string,
  path: string
): Promise<string> {
  const query = new URLSearchParams({ path });
  const response = await fetchWithAuth(
    `/api/v1/flows/executions/${executionId}/evidence/members?${query.toString()}`
  );
  if (!response.ok) {
    return fail(response, 'Could not read the evidence member');
  }
  return response.text();
}

export async function downloadEvidenceMember(
  executionId: string,
  path: string
): Promise<BinaryDownload> {
  const query = new URLSearchParams({ path });
  const response = await fetchWithAuth(
    `/api/v1/flows/executions/${executionId}/evidence/members?${query.toString()}`
  );
  const leaf = path.split('/').pop() || 'member';
  return readBinary(response, leaf, 'Could not download the evidence member');
}
