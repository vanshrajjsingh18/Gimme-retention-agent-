import { useState } from 'react';

import { api } from '../api/client';
import {
  Badge,
  Card,
  ErrorState,
  LoadingState,
  Modal,
  PageHeader,
  SectionTitle,
  Spinner,
  notify,
} from '../components/ui';
import { useMutation, useQuery } from '../hooks/useApi';
import type { Integration } from '../types';
import { formatDateTime, humanize } from '../utils/format';

interface WhatsAppProfile {
  key: string;
  label: string;
  base_url: string;
  required_credentials: string[];
}

const CREDENTIAL_LABELS: Record<string, string> = {
  tenant_id: 'Directory (tenant) ID',
  client_id: 'Application (client) ID',
  client_secret: 'Client secret',
  sender_address: 'Sender mailbox address',
  auth_token: 'Auth token',
  sender: 'Sender number or name',
  access_token: 'Access token',
  phone_number_id: 'Phone number ID',
  account_sid: 'Account SID',
  from_number: 'From number',
  api_key: 'API key',
};

const SETUP_NOTES: Record<string, string> = {
  outlook:
    'Requires a Microsoft Entra app registration with the application permission Mail.Send, granted admin consent.',
  tnz: 'Requires a TNZ Group account with REST API access enabled.',
  whatsapp:
    'Pick the provider you have an account with. All of them accept the same message shape behind this adapter.',
};

export default function IntegrationsPage() {
  const { data, loading, error, refetch } = useQuery<Integration[]>('/api/v1/integrations');
  const { data: llm } = useQuery<{
    provider: string;
    model: string;
    status: string;
    mode: string;
    message: string;
  }>('/api/v1/integrations/llm');
  const [editing, setEditing] = useState<Integration | null>(null);

  if (loading) return <LoadingState label="Loading integrations…" />;
  if (error) return <ErrorState message={error} onRetry={refetch} />;

  return (
    <>
      <PageHeader
        title="Integrations"
        description="Message providers and the language model. Everything works in mock mode without credentials."
      />

      {llm && (
        <Card title="Language model" className="mb-4">
          <div className="flex flex-wrap items-center gap-2">
            <Badge
              className={
                llm.mode === 'mock'
                  ? 'bg-amber-50 text-amber-800 ring-amber-200'
                  : llm.status === 'OK'
                    ? 'bg-emerald-50 text-emerald-700 ring-emerald-200'
                    : 'bg-red-50 text-red-700 ring-red-200'
              }
            >
              {llm.mode === 'mock' ? 'Mock' : llm.status}
            </Badge>
            <span className="text-sm font-medium text-slate-800">{llm.provider}</span>
            <span className="text-sm text-slate-500">{llm.model}</span>
          </div>
          <p className="mt-2 text-sm text-slate-600">{llm.message}</p>
          <p className="mt-3 border-t border-slate-100 pt-3 text-xs text-slate-500">
            Configured through environment variables, not this screen, so a key is never handled by
            the browser. Set <code className="font-mono">LLM_PROVIDER=openai</code>,{' '}
            <code className="font-mono">LLM_API_KEY</code>,{' '}
            <code className="font-mono">LLM_BASE_URL</code> and{' '}
            <code className="font-mono">LLM_MODEL</code> in your <code className="font-mono">.env</code>,
            then restart the backend. Any OpenAI-compatible endpoint works.
          </p>
        </Card>
      )}

      <div className="grid gap-4 lg:grid-cols-3">
        {(data ?? []).map((integration) => (
          <Card
            key={integration.id}
            title={integration.display_name}
            description={`${integration.channel} · ${integration.provider}`}
            actions={
              <Badge
                className={
                  integration.mode === 'mock'
                    ? 'bg-amber-50 text-amber-800 ring-amber-200'
                    : integration.status === 'OK'
                      ? 'bg-emerald-50 text-emerald-700 ring-emerald-200'
                      : integration.status === 'ERROR'
                        ? 'bg-red-50 text-red-700 ring-red-200'
                        : 'bg-slate-100 text-slate-600 ring-slate-200'
                }
              >
                {integration.mode === 'mock' ? 'Mock' : humanize(integration.status)}
              </Badge>
            }
          >
            <p className="text-sm text-slate-600">{integration.status_message}</p>

            {integration.required_credentials.length > 0 && (
              <div className="mt-3">
                <SectionTitle>Credentials</SectionTitle>
                <ul className="space-y-1">
                  {integration.required_credentials.map((key) => {
                    const stored = integration.credentials[key];
                    return (
                      <li key={key} className="flex items-center justify-between gap-2 text-xs">
                        <span className="text-slate-600">
                          {CREDENTIAL_LABELS[key] ?? humanize(key)}
                        </span>
                        {stored?.configured ? (
                          <span className="font-mono text-slate-400">{stored.hint}</span>
                        ) : (
                          <span className="text-slate-400">Not set</span>
                        )}
                      </li>
                    );
                  })}
                </ul>
              </div>
            )}

            {integration.last_checked_at && (
              <p className="mt-3 text-xs text-slate-400">
                Last checked {formatDateTime(integration.last_checked_at)}
              </p>
            )}

            <button
              type="button"
              className="btn-secondary mt-4 w-full"
              onClick={() => setEditing(integration)}
            >
              Configure
            </button>
          </Card>
        ))}
      </div>

      <Card title="Webhooks" className="mt-4">
        <p className="text-sm text-slate-600">
          Point your provider's delivery and engagement webhooks at these endpoints. Events are
          matched to a stored message by its provider message ID; anything unrecognised is ignored
          rather than creating stray records.
        </p>
        <ul className="mt-3 space-y-2">
          {['outlook', 'tnz', 'whatsapp'].map((provider) => (
            <li key={provider} className="flex flex-wrap items-center gap-2">
              <Badge className="bg-slate-100 text-slate-600 ring-slate-200">
                {humanize(provider)}
              </Badge>
              <code className="break-all rounded bg-slate-100 px-2 py-1 font-mono text-xs text-slate-700">
                POST {api.baseUrl}/api/v1/webhooks/{provider}
              </code>
            </li>
          ))}
        </ul>
      </Card>

      {editing && (
        <IntegrationModal
          integration={editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            refetch();
          }}
        />
      )}
    </>
  );
}

function IntegrationModal({
  integration,
  onClose,
  onSaved,
}: {
  integration: Integration;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [mode, setMode] = useState<'mock' | 'live'>(integration.mode);
  const [credentials, setCredentials] = useState<Record<string, string>>({});
  const [profile, setProfile] = useState<string>(
    String(integration.config?.profile ?? 'meta_cloud'),
  );
  const [testTo, setTestTo] = useState('');

  const { data: profiles } = useQuery<{ profiles: WhatsAppProfile[] }>(
    integration.channel === 'WHATSAPP' ? '/api/v1/integrations/whatsapp-profiles' : null,
  );

  const save = useMutation(async () =>
    api.patch<Integration>(`/api/v1/integrations/${integration.id}`, {
      mode,
      credentials,
      config: integration.channel === 'WHATSAPP' ? { ...integration.config, profile } : undefined,
    }),
  );

  const testConnection = useMutation(async () =>
    api.post<{ status: string; message: string; is_mock: boolean }>(
      `/api/v1/integrations/${integration.id}/test-connection`,
    ),
  );

  const testMessage = useMutation(async () =>
    api.post<{ success: boolean; is_mock: boolean; error: string | null }>(
      `/api/v1/integrations/${integration.id}/test-message`,
      { to: testTo },
    ),
  );

  const activeProfile = profiles?.profiles.find((p) => p.key === profile);
  const requiredKeys =
    integration.channel === 'WHATSAPP' && activeProfile
      ? activeProfile.required_credentials
      : integration.required_credentials;

  return (
    <Modal
      open
      title={integration.display_name}
      description={SETUP_NOTES[integration.provider]}
      onClose={onClose}
      size="md"
      footer={
        <>
          <button type="button" className="btn-secondary" onClick={onClose}>
            Close
          </button>
          <button
            type="button"
            className="btn-primary"
            disabled={save.loading}
            onClick={async () => {
              const result = await save.run();
              if (result) {
                notify('Integration updated.');
                onSaved();
              }
            }}
          >
            {save.loading && <Spinner className="h-4 w-4 text-white" />}
            Save
          </button>
        </>
      }
    >
      <div className="space-y-4">
        <div>
          <p className="label">Mode</p>
          <div className="inline-flex overflow-hidden rounded-lg border border-slate-300">
            {(['mock', 'live'] as const).map((m) => (
              <button
                key={m}
                type="button"
                onClick={() => setMode(m)}
                aria-pressed={mode === m}
                className={`px-3 py-1.5 text-xs font-medium transition-colors ${
                  mode === m ? 'bg-brand-600 text-white' : 'bg-white text-slate-600 hover:bg-slate-50'
                }`}
              >
                {m === 'mock' ? 'Mock' : 'Live'}
              </button>
            ))}
          </div>
          <p className="mt-1.5 text-xs text-slate-500">
            {mode === 'mock'
              ? 'Messages are recorded locally with realistic simulated delivery and engagement. Nothing leaves this machine.'
              : 'Messages are sent through the real provider. If any required credential is missing, the system falls back to mock rather than silently dropping messages.'}
          </p>
        </div>

        {integration.channel === 'WHATSAPP' && profiles && (
          <div>
            <label className="label" htmlFor="wa-profile">
              Provider
            </label>
            <select
              id="wa-profile"
              className="input"
              value={profile}
              onChange={(e) => setProfile(e.target.value)}
            >
              {profiles.profiles.map((p) => (
                <option key={p.key} value={p.key}>
                  {p.label}
                </option>
              ))}
            </select>
            {activeProfile && (
              <p className="mt-1 text-xs text-slate-500">Sends to {activeProfile.base_url}</p>
            )}
          </div>
        )}

        {mode === 'live' && (
          <div>
            <SectionTitle>Credentials</SectionTitle>
            <div className="space-y-3">
              {requiredKeys.map((key) => {
                const stored = integration.credentials[key];
                return (
                  <div key={key}>
                    <label className="label" htmlFor={`cred-${key}`}>
                      {CREDENTIAL_LABELS[key] ?? humanize(key)}
                    </label>
                    <input
                      id={`cred-${key}`}
                      type={key.includes('secret') || key.includes('token') || key.includes('key')
                        ? 'password'
                        : 'text'}
                      className="input"
                      placeholder={
                        stored?.configured ? `Stored (${stored.hint}) — leave blank to keep` : ''
                      }
                      value={credentials[key] ?? ''}
                      onChange={(e) =>
                        setCredentials({ ...credentials, [key]: e.target.value })
                      }
                      autoComplete="off"
                    />
                  </div>
                );
              })}
            </div>
            <p className="mt-2 text-xs text-slate-500">
              Stored values are never returned to the browser — only a masked hint. Leaving a field
              blank keeps the existing secret.
            </p>
          </div>
        )}

        <div className="border-t border-slate-200 pt-4">
          <SectionTitle>Test</SectionTitle>
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              className="btn-secondary"
              onClick={async () => {
                const result = await testConnection.run();
                if (result) notify(result.message, result.status === 'OK' ? 'success' : 'error');
              }}
              disabled={testConnection.loading}
            >
              {testConnection.loading && <Spinner className="h-4 w-4" />}
              Test connection
            </button>
          </div>

          {testConnection.error && (
            <p className="mt-2 text-xs text-red-700">{testConnection.error}</p>
          )}

          <div className="mt-3 flex gap-2">
            <input
              className="input"
              placeholder={
                integration.channel === 'EMAIL' ? 'you@gimmedelivery.co.nz' : '+64211234567'
              }
              value={testTo}
              onChange={(e) => setTestTo(e.target.value)}
              aria-label="Test recipient"
            />
            <button
              type="button"
              className="btn-secondary shrink-0"
              disabled={testTo.trim().length < 3 || testMessage.loading}
              onClick={async () => {
                const result = await testMessage.run();
                if (result) {
                  notify(
                    result.success
                      ? `Test message sent${result.is_mock ? ' (simulated)' : ''}.`
                      : `Failed: ${result.error}`,
                    result.success ? 'success' : 'error',
                  );
                }
              }}
            >
              {testMessage.loading && <Spinner className="h-4 w-4" />}
              Send test
            </button>
          </div>
        </div>

        {save.error && (
          <p className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
            {save.error}
          </p>
        )}
      </div>
    </Modal>
  );
}
