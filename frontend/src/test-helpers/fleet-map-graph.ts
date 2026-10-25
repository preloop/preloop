import type {
  FleetMapGraph,
  FleetMapNode,
  FleetMapEdge,
  FleetMapResourceType,
} from '../fleet-map-api';

/** Synthetic fleet using exactly the API's node, edge and impact fields. */
export function sampleFleetMap(): FleetMapGraph {
  const node = (
    id: string,
    type: FleetMapResourceType,
    label: string,
    extra: Partial<FleetMapNode> = {}
  ): FleetMapNode => ({
    id,
    type,
    resource_id: id.split(':')[1],
    label,
    detail: null,
    href: '/console/settings/device-fleet',
    status: null,
    owner_ids: [],
    provider_ids: [],
    gaps: [],
    finding_count: 0,
    actions: [],
    ...extra,
  });
  const edge = (
    source: string,
    target: string,
    kind: FleetMapEdge['kind'],
    provenance: FleetMapEdge['provenance'] = 'configured'
  ): FleetMapEdge => ({
    id: `${kind}:${source}:${target}:${provenance}`,
    source,
    target,
    kind,
    provenance,
    impact_source: target,
    impact_target: source,
    gaps: [],
  });
  return {
    schema_version: 1,
    truncated: false,
    limit: 500,
    clusters: [],
    unavailable: [],
    nodes: [
      node('user:maya', 'user', 'Maya Chen', {
        href: '/console/settings/users',
        provider_ids: ['provider:openai', 'provider:anthropic'],
      }),
      node('user:sam', 'user', 'Sam Ortiz', {
        provider_ids: ['provider:openai'],
      }),
      node('device:mbp', 'device', 'maya-laptop', {
        owner_ids: ['user:maya'],
        provider_ids: ['provider:openai', 'provider:anthropic'],
      }),
      node('device:build', 'device', 'build-01', {
        owner_ids: ['user:sam'],
        provider_ids: ['provider:openai'],
      }),
      node('runner:r2', 'runner', 'build-01 runner', {
        status: 'offline',
        gaps: ['offline_runner'],
        owner_ids: ['user:sam'],
      }),
      node('agent:claude', 'agent', 'Claude Code', {
        owner_ids: ['user:maya'],
        provider_ids: ['provider:anthropic'],
      }),
      node('agent:codex', 'agent', 'Codex', {
        owner_ids: ['user:maya'],
        provider_ids: ['provider:openai'],
      }),
      node('agent:cursor', 'agent', 'Cursor', {
        gaps: ['discovered_agent'],
        agent_state: 'discovered',
        owner_ids: ['user:sam'],
        provider_ids: ['provider:openai'],
      }),
      node('model:sonnet', 'model', 'claude-sonnet-5-5', {
        owner_ids: ['user:maya'],
        provider_ids: ['provider:anthropic'],
      }),
      node('model:gpt', 'model', 'gpt-6.1', {
        owner_ids: ['user:maya'],
        provider_ids: ['provider:openai'],
      }),
      node('account:prod', 'provider_account', 'OpenAI prod', {
        owner_ids: ['user:maya'],
        provider_ids: ['provider:openai'],
        parent_id: 'provider:openai',
        actions: [
          { label: 'Manage credentials', href: '/console/ai-models/example' },
        ],
      }),
      node('account:dev', 'provider_account', 'OpenAI dev', {
        owner_ids: ['user:sam'],
        provider_ids: ['provider:openai'],
        parent_id: 'provider:openai',
        gaps: ['not_via_gateway'],
      }),
      node('account:anthropic', 'provider_account', 'Anthropic team', {
        owner_ids: ['user:maya'],
        provider_ids: ['provider:anthropic'],
        parent_id: 'provider:anthropic',
      }),
      node('provider:openai', 'provider', 'OpenAI', {
        owner_ids: ['user:maya', 'user:sam'],
        provider_ids: ['provider:openai'],
      }),
      node('provider:anthropic', 'provider', 'Anthropic', {
        owner_ids: ['user:maya'],
        provider_ids: ['provider:anthropic'],
      }),
    ],
    edges: [
      edge('user:maya', 'device:mbp', 'owns', 'observed'),
      edge('user:sam', 'device:build', 'owns', 'observed'),
      edge('device:build', 'runner:r2', 'hosts', 'observed'),
      edge('device:mbp', 'agent:claude', 'hosts', 'observed'),
      edge('device:mbp', 'agent:codex', 'hosts', 'observed'),
      edge('device:build', 'agent:cursor', 'hosts', 'observed'),
      edge('model:sonnet', 'runner:r2', 'runs_on'),
      edge('agent:claude', 'model:sonnet', 'uses_model', 'observed'),
      edge('agent:codex', 'model:gpt', 'uses_model'),
      edge('agent:claude', 'account:anthropic', 'uses_account', 'observed'),
      edge('agent:codex', 'account:prod', 'uses_account', 'observed'),
      edge('agent:cursor', 'account:dev', 'uses_account', 'observed'),
      edge('account:prod', 'provider:openai', 'account_of_provider'),
      edge('account:dev', 'provider:openai', 'account_of_provider'),
      edge('account:anthropic', 'provider:anthropic', 'account_of_provider'),
    ],
  };
}
