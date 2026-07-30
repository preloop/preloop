/**
 * WebAuthn passkey ceremonies for the console.
 *
 * Registration (settings) and authentication (login page) against
 * /api/v1/auth/webauthn/. Keeps scope tight: single-device passkeys with
 * discoverable credentials.
 */

import { fetchWithAuth } from '../api';

export interface PasskeySummary {
  id: string;
  name: string;
  created_at: string;
  last_used_at?: string | null;
}

export function passkeysSupported(): boolean {
  return (
    typeof window !== 'undefined' &&
    'PublicKeyCredential' in window &&
    typeof navigator.credentials?.create === 'function'
  );
}

function base64urlToBuffer(value: string): ArrayBuffer {
  const padded = value.replace(/-/g, '+').replace(/_/g, '/');
  const binary = atob(padded + '='.repeat((4 - (padded.length % 4)) % 4));
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) {
    bytes[i] = binary.charCodeAt(i);
  }
  return bytes.buffer;
}

function bufferToBase64url(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let binary = '';
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=/g, '');
}

/* Convert the JSON options from the server into the binary shapes
 * navigator.credentials expects. */
function prepareCreationOptions(options: any): PublicKeyCredentialCreationOptions {
  return {
    ...options,
    challenge: base64urlToBuffer(options.challenge),
    user: {
      ...options.user,
      id: base64urlToBuffer(options.user.id),
    },
    excludeCredentials: (options.excludeCredentials || []).map((c: any) => ({
      ...c,
      id: base64urlToBuffer(c.id),
    })),
  };
}

function prepareRequestOptions(options: any): PublicKeyCredentialRequestOptions {
  return {
    ...options,
    challenge: base64urlToBuffer(options.challenge),
    allowCredentials: (options.allowCredentials || []).map((c: any) => ({
      ...c,
      id: base64urlToBuffer(c.id),
    })),
  };
}

function serializeAttestation(credential: PublicKeyCredential): any {
  const response = credential.response as AuthenticatorAttestationResponse;
  return {
    id: credential.id,
    rawId: bufferToBase64url(credential.rawId),
    type: credential.type,
    response: {
      clientDataJSON: bufferToBase64url(response.clientDataJSON),
      attestationObject: bufferToBase64url(response.attestationObject),
      transports:
        typeof response.getTransports === 'function'
          ? response.getTransports()
          : [],
    },
    clientExtensionResults: credential.getClientExtensionResults(),
  };
}

function serializeAssertion(credential: PublicKeyCredential): any {
  const response = credential.response as AuthenticatorAssertionResponse;
  return {
    id: credential.id,
    rawId: bufferToBase64url(credential.rawId),
    type: credential.type,
    response: {
      clientDataJSON: bufferToBase64url(response.clientDataJSON),
      authenticatorData: bufferToBase64url(response.authenticatorData),
      signature: bufferToBase64url(response.signature),
      userHandle: response.userHandle
        ? bufferToBase64url(response.userHandle)
        : null,
    },
    clientExtensionResults: credential.getClientExtensionResults(),
  };
}

/** Register a new passkey for the signed-in user (settings page). */
export async function registerPasskey(name?: string): Promise<PasskeySummary> {
  const optionsResponse = await fetchWithAuth(
    '/api/v1/auth/webauthn/register/options',
    { method: 'POST' }
  );
  if (!optionsResponse.ok) {
    throw new Error('Failed to start passkey registration');
  }
  const { options, challenge_token } = await optionsResponse.json();

  const credential = (await navigator.credentials.create({
    publicKey: prepareCreationOptions(options),
  })) as PublicKeyCredential | null;
  if (!credential) {
    throw new Error('Passkey registration was cancelled');
  }

  const verifyResponse = await fetchWithAuth(
    '/api/v1/auth/webauthn/register/verify',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        credential: serializeAttestation(credential),
        challenge_token,
        name: name || null,
      }),
    }
  );
  if (!verifyResponse.ok) {
    const errorData = await verifyResponse.json().catch(() => ({}));
    throw new Error(
      typeof errorData.detail === 'string'
        ? errorData.detail
        : 'Failed to verify passkey registration'
    );
  }
  return verifyResponse.json();
}

/**
 * Sign in with a passkey (login page, unauthenticated). Returns the token
 * payload; the caller stores tokens and navigates.
 */
export async function signInWithPasskey(): Promise<{
  access_token: string;
  refresh_token: string;
}> {
  const optionsResponse = await fetch(
    '/api/v1/auth/webauthn/authenticate/options',
    { method: 'POST' }
  );
  if (!optionsResponse.ok) {
    throw new Error('Failed to start passkey sign-in');
  }
  const { options, challenge_token } = await optionsResponse.json();

  const credential = (await navigator.credentials.get({
    publicKey: prepareRequestOptions(options),
  })) as PublicKeyCredential | null;
  if (!credential) {
    throw new Error('Passkey sign-in was cancelled');
  }

  const verifyResponse = await fetch(
    '/api/v1/auth/webauthn/authenticate/verify',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        credential: serializeAssertion(credential),
        challenge_token,
      }),
    }
  );
  if (!verifyResponse.ok) {
    const errorData = await verifyResponse.json().catch(() => ({}));
    throw new Error(
      typeof errorData.detail === 'string'
        ? errorData.detail
        : 'Passkey sign-in failed'
    );
  }
  return verifyResponse.json();
}

/** List the signed-in user's passkeys. */
export async function listPasskeys(): Promise<PasskeySummary[]> {
  const response = await fetchWithAuth('/api/v1/auth/webauthn/credentials');
  if (!response.ok) {
    throw new Error('Failed to fetch passkeys');
  }
  return response.json();
}

/** Remove a passkey by id. */
export async function deletePasskey(id: string): Promise<void> {
  const response = await fetchWithAuth(
    `/api/v1/auth/webauthn/credentials/${id}`,
    { method: 'DELETE' }
  );
  if (!response.ok) {
    throw new Error('Failed to delete passkey');
  }
}
