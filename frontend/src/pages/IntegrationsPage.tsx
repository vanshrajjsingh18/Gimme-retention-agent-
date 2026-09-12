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
  webhook_secret: 'Webhook secret',
};

const CREDENTIAL_HELP: Record<string, string> = {
  webhook_secret:
    'Authenticates inbound delivery receipts and replies. Set the same value on the provider’s webhook. Required before going live.',
};

interface ReadinessCheck {
  key: string;
  label: string;
  passed: boolean;
  blocking: boolean;
  detail: string;
  remedy: string;
}

interface Readiness {
  provider: string;
  channel: string;
  mode: string;
  ready: boolean;
  blocking_count: number;
  warning_count: number;
  checks: ReadinessCheck[];
}

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

      {(data ?? [])
        .filter((integration) => integration.channel === 'SMS')
        .map((integration) => (
          <GoLiveChecklist key={integration.id} integration={integration} />
        ))}

      <Card title="Webhooks" className="mt-4">
        <p className="text-sm text-slate-600">
          Point your provider's delivery and engagement webhooks at these endpoints. Delivery
          events are matched to a stored message by its provider message ID, and anything
          unrecognised is ignored rather than creating stray records. An inbound <em>reply</em> is
          matched by phone number instead, so that an opt-out is honoured even when the provider
          does not echo our message ID back — which is why a live integration also requires a
          webhook secret, sent as an <code className="rounded bg-slate-100 px-1">X-Webhook-Secret</code>{' '}
          header or a <code className="rounded bg-slate-100 px-1">?secret=</code> query parameter.
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
        <LocalhostWebhookNote />
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
  const [revealed, setRevealed] = useState<Record<string, boolean>>({});
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
  const sendingKeys =
    integration.channel === 'WHATSAPP' && activeProfile
      ? activeProfile.required_credentials
      : integration.required_credentials;
  // Not a sending credential — messages go out fine without it — but inbound
  // webhooks are refused in live mode until it is set, so it needs a field.
  const requiredKeys = [...sendingKeys, 'webhook_secret'];

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

        {/* Always shown, not gated on live mode. Credentials have to be entered
            and tested *before* switching over — requiring live first would mean
            declaring yourself ready in order to find out whether you are. */}
        <div>
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
                      type={
                        revealed[key]
                          ? 'text'
                          : key.includes('secret') ||
                              key.includes('token') ||
                              key.includes('key')
                            ? 'password'
                            : 'text'
                      }
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
                    {key === 'webhook_secret' && (
                      <SecretGenerator
                        value={credentials[key] ?? ''}
                        revealed={Boolean(revealed[key])}
                        onReveal={() =>
                          setRevealed({ ...revealed, [key]: !revealed[key] })
                        }
                        onGenerate={(secret) => {
                          setCredentials({ ...credentials, [key]: secret });
                          // A value that has to be transcribed into TNZ is not
                          // usefully protected by dots.
                          setRevealed({ ...revealed, [key]: true });
                        }}
                      />
                    )}
                    {CREDENTIAL_HELP[key] && (
                      <p className="mt-1 text-xs text-slate-500">{CREDENTIAL_HELP[key]}</p>
                    )}
                  </div>
                );
              })}
            </div>
            <p className="mt-2 text-xs text-slate-500">
              Stored values are never returned to the browser — only a masked hint. Leaving a field
              blank keeps the existing secret.
              {mode === 'mock' && ' Saving these while in Mock changes nothing about sending — it just means they are ready when you switch to Live.'}
            </p>
          </div>
        </div>

        <div className="border-t border-slate-200 pt-4">
          <SectionTitle>Test</SectionTitle>
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              className="btn-secondary"
              onClick={async () => {
                const result = await testConnection.run();
                if (result) {
                  // A mock result is neither a pass nor a failure — it is a
                  // statement that nothing was actually contacted.
                  notify(
                    result.message,
                    result.status === 'OK' ? 'success' : result.is_mock ? 'info' : 'error',
                  );
                }
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

/**
 * The go-live checklist for the SMS integration.
 *
 * A connection test proves the credentials work. This answers the question an
 * operator actually has before flipping to live: what happens when this starts
 * texting real people. Blockers are separated from advisories because a
 * fraction of unreachable numbers is normal, and an unauthenticated webhook is
 * not.
 */
function GoLiveChecklist({ integration }: { integration: Integration }) {
  const { data, loading, error, refetch } = useQuery<Readiness>(
    `/api/v1/integrations/${integration.id}/readiness`,
  );

  if (loading) return <LoadingState label="Checking go-live readiness…" />;
  if (error) return <ErrorState message={error} onRetry={refetch} />;
  if (!data) return null;

  const blockers = data.checks.filter((c) => c.blocking && !c.passed);
  const warnings = data.checks.filter((c) => !c.blocking && !c.passed);
  const passing = data.checks.filter((c) => c.passed);

  return (
    <Card
      className="mt-4"
      title="TNZ go-live readiness"
      description={
        data.mode === 'live'
          ? 'This integration is live. These checks describe the system texting real people right now.'
          : 'Everything that must be true before switching this integration from mock to live.'
      }
      actions={
        <Badge
          className={
            data.ready
              ? 'bg-emerald-50 text-emerald-700 ring-emerald-200'
              : 'bg-red-50 text-red-700 ring-red-200'
          }
        >
          {data.ready ? 'Ready to go live' : `${data.blocking_count} blocker${data.blocking_count === 1 ? '' : 's'}`}
        </Badge>
      }
    >
      {blockers.length > 0 && (
        <div className="mb-4">
          <SectionTitle>Blocking</SectionTitle>
          <ul className="space-y-2">
            {blockers.map((check) => (
              <li
                key={check.key}
                className="rounded-lg bg-red-50 p-3 ring-1 ring-red-100"
              >
                <p className="text-sm font-medium text-red-800">{check.label}</p>
                <p className="mt-0.5 text-xs text-red-700">{check.detail}</p>
                {check.remedy && (
                  <p className="mt-1 text-xs text-red-600">{check.remedy}</p>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      {warnings.length > 0 && (
        <div className="mb-4">
          <SectionTitle>Worth knowing</SectionTitle>
          <ul className="space-y-2">
            {warnings.map((check) => (
              <li
                key={check.key}
                className="rounded-lg bg-amber-50 p-3 ring-1 ring-amber-100"
              >
                <p className="text-sm font-medium text-amber-900">{check.label}</p>
                <p className="mt-0.5 text-xs text-amber-800">{check.detail}</p>
                {check.remedy && (
                  <p className="mt-1 text-xs text-amber-700">{check.remedy}</p>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      {passing.length > 0 && (
        <div>
          <SectionTitle>Passing</SectionTitle>
          <ul className="space-y-1.5">
            {passing.map((check) => (
              <li key={check.key} className="flex gap-2 text-xs">
                <span aria-hidden className="mt-0.5 text-emerald-600">✓</span>
                <span>
                  <span className="font-medium text-slate-700">{check.label}</span>
                  <span className="text-slate-500"> — {check.detail}</span>
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </Card>
  );
}

/**
 * The webhook endpoints are only useful if a provider can actually reach them.
 *
 * The URLs above are built from whatever this dashboard talks to, which during
 * development is localhost. Presenting that as something to paste into TNZ
 * would send somebody off to configure a URL that can never be delivered to,
 * and the failure would look like "TNZ never sends us receipts" rather than
 * "that address does not exist".
 */
function LocalhostWebhookNote() {
  let host = '';
  try {
    host = new URL(api.baseUrl).hostname;
  } catch {
    host = '';
  }
  const isLocal = host === 'localhost' || host === '127.0.0.1' || host === '::1';
  if (!isLocal) return null;

  return (
    <p className="mt-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
      These point at <code className="font-mono">{host}</code>, which TNZ cannot reach. Before
      going live, deploy the backend somewhere with a public HTTPS address and configure that
      URL at the provider — or tunnel to this machine while you are testing. A webhook
      configured against localhost never arrives, and the symptom is silence rather than an
      error.
    </p>
  );
}

/**
 * Generate a webhook secret, and let it be copied before it is stored.
 *
 * Left to invent one, people pick something memorable, and this value is the
 * only thing standing between a public endpoint and a stranger being able to
 * restore marketing consent somebody withdrew. Generated in the browser with
 * the platform CSPRNG — it is posted once on save and masked from then on, so
 * this is the only moment it can be copied into TNZ.
 */
function SecretGenerator({
  value,
  revealed,
  onGenerate,
  onReveal,
}: {
  value: string;
  revealed: boolean;
  onGenerate: (secret: string) => void;
  onReveal: () => void;
}) {
  const [copied, setCopied] = useState(false);

  return (
    <div className="mt-1.5 flex flex-wrap items-center gap-2">
      <button
        type="button"
        className="btn-secondary px-2 py-1 text-xs"
        onClick={() => {
          const bytes = new Uint8Array(24);
          crypto.getRandomValues(bytes);
          const secret = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
          onGenerate(secret);
          setCopied(false);
        }}
      >
        Generate
      </button>
      {value && (
        <button type="button" className="btn-ghost px-2 py-1 text-xs" onClick={onReveal}>
          {revealed ? 'Hide' : 'Show'}
        </button>
      )}
      {value && (
        <button
          type="button"
          className="btn-ghost px-2 py-1 text-xs"
          onClick={async () => {
            try {
              await navigator.clipboard.writeText(value);
              setCopied(true);
            } catch {
              // Clipboard access can be refused. Reveal the value instead, so
              // there is always a way to get it into TNZ.
              setCopied(false);
              onReveal();
            }
          }}
        >
          {copied ? 'Copied' : 'Copy'}
        </button>
      )}
      {value && (
        <span className="text-xs text-slate-500">
          Copy this into TNZ&rsquo;s webhook settings now — after saving it is masked.
        </span>
      )}
    </div>
  );
}
