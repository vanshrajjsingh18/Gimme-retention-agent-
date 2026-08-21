import { useState } from 'react';

import { api } from '../api/client';
import {
  Badge,
  Card,
  ConfirmDialog,
  ErrorState,
  LoadingState,
  PageHeader,
  SectionTitle,
  Spinner,
  notify,
} from '../components/ui';
import { useMutation, useQuery } from '../hooks/useApi';
import type { Channel, ComplianceReport, ComplianceRule } from '../types';
import { humanize } from '../utils/format';

interface ComplianceConfig {
  minimum_age: number;
  require_age_verification: boolean;
  frequency_cap_30d: number;
  frequency_cap_7d: number;
  quiet_hours_start: string;
  quiet_hours_end: string;
  enforce_quiet_hours: boolean;
  require_responsible_drinking_statement: boolean;
  allowed_coupon_codes: string[];
  allowed_promotions: string[];
  delivery_promise: string;
  disabled_rules: string[];
}

export default function CompliancePage() {
  const { data: rules, loading, error, refetch } = useQuery<ComplianceRule[]>(
    '/api/v1/compliance/rules',
  );
  const { data: config, refetch: refetchConfig } = useQuery<ComplianceConfig>(
    '/api/v1/compliance/config',
  );
  const { data: claims } = useQuery<{ claims: { code: string; label: string }[] }>(
    '/api/v1/compliance/prohibited-claims',
  );

  const [pendingDisable, setPendingDisable] = useState<ComplianceRule | null>(null);

  const toggle = useMutation(async (rule: ComplianceRule, enabled: boolean) => {
    const result = await api.patch<ComplianceRule>(`/api/v1/compliance/rules/${rule.id}`, {
      enabled,
    });
    refetch();
    refetchConfig();
    return result;
  });

  if (loading) return <LoadingState label="Loading compliance rules…" />;
  if (error) return <ErrorState message={error} onRetry={refetch} />;

  const blocking = (rules ?? []).filter((r) => r.blocks_send);
  const advisory = (rules ?? []).filter((r) => !r.blocks_send);

  return (
    <>
      <PageHeader
        title="Compliance"
        description="GIMME sells alcohol, so these rules are enforced in code before any message can send."
      />

      <div className="grid gap-4 lg:grid-cols-3">
        <div className="space-y-4 lg:col-span-2">
          <Card
            title="Blocking rules"
            description="A failure here stops the send. Every campaign is re-checked at approval and again per recipient at send time."
          >
            <ul className="divide-y divide-slate-100">
              {blocking.map((rule) => (
                <RuleRow
                  key={rule.id}
                  rule={rule}
                  busy={toggle.loading}
                  onToggle={(enabled) => {
                    if (!enabled) setPendingDisable(rule);
                    else toggle.run(rule, true);
                  }}
                />
              ))}
            </ul>
          </Card>

          <Card title="Advisory rules" description="Reported as warnings; they do not block a send.">
            <ul className="divide-y divide-slate-100">
              {advisory.map((rule) => (
                <RuleRow
                  key={rule.id}
                  rule={rule}
                  busy={toggle.loading}
                  onToggle={(enabled) => toggle.run(rule, enabled)}
                />
              ))}
            </ul>
          </Card>

          <ContentChecker />
        </div>

        <div className="space-y-4">
          {config && (
            <Card title="Active enforcement" description="Assembled from brand settings and rules.">
              <dl className="space-y-3 text-sm">
                <ConfigRow
                  label="Minimum age"
                  value={`${config.minimum_age}+`}
                  active={config.require_age_verification}
                  activeLabel="verification required"
                />
                <ConfigRow
                  label="Frequency cap (30 days)"
                  value={
                    config.frequency_cap_30d > 1000 ? 'Disabled' : `${config.frequency_cap_30d} messages`
                  }
                  active={config.frequency_cap_30d <= 1000}
                />
                <ConfigRow
                  label="Frequency cap (7 days)"
                  value={
                    config.frequency_cap_7d > 1000 ? 'Disabled' : `${config.frequency_cap_7d} messages`
                  }
                  active={config.frequency_cap_7d <= 1000}
                />
                <ConfigRow
                  label="Quiet hours"
                  value={`${config.quiet_hours_start}–${config.quiet_hours_end}`}
                  active={config.enforce_quiet_hours}
                  activeLabel="SMS & WhatsApp only"
                />
                <ConfigRow
                  label="Responsible drinking statement"
                  value={config.require_responsible_drinking_statement ? 'Required on email' : 'Not required'}
                  active={config.require_responsible_drinking_statement}
                />
                <ConfigRow
                  label="Approved promotions"
                  value={
                    config.allowed_promotions.length
                      ? `${config.allowed_promotions.length} approved`
                      : 'None — no offers allowed'
                  }
                  active
                />
                <ConfigRow
                  label="Active coupon codes"
                  value={
                    config.allowed_coupon_codes.length
                      ? config.allowed_coupon_codes.join(', ')
                      : 'None — any code is blocked'
                  }
                  active
                />
                <ConfigRow
                  label="Delivery promise"
                  value={config.delivery_promise || 'Not set'}
                  active={Boolean(config.delivery_promise)}
                />
              </dl>

              {config.disabled_rules.length > 0 && (
                <div className="mt-4 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2">
                  <p className="text-xs font-medium text-amber-800">
                    {config.disabled_rules.length} rule
                    {config.disabled_rules.length === 1 ? '' : 's'} disabled
                  </p>
                  <p className="mt-0.5 text-xs text-amber-700">
                    Disabled rules are recorded in the audit log.
                  </p>
                </div>
              )}
            </Card>
          )}

          {claims && (
            <Card
              title="Prohibited claims"
              description="Built-in patterns that block a message outright."
            >
              <ul className="space-y-2">
                {claims.claims.map((claim) => (
                  <li key={claim.code} className="text-sm">
                    <p className="font-mono text-xs font-medium text-slate-700">{claim.code}</p>
                    <p className="text-xs text-slate-600">{claim.label}</p>
                  </li>
                ))}
              </ul>
            </Card>
          )}
        </div>
      </div>

      <ConfirmDialog
        open={pendingDisable !== null}
        title={`Disable "${pendingDisable?.name}"?`}
        message={
          `This rule currently blocks non-compliant sends. Disabling it means campaigns can ` +
          `send without this protection. The change is recorded in the audit log.`
        }
        confirmLabel="Disable rule"
        destructive
        busy={toggle.loading}
        onCancel={() => setPendingDisable(null)}
        onConfirm={async () => {
          if (pendingDisable) {
            await toggle.run(pendingDisable, false);
            notify(`"${pendingDisable.name}" disabled.`, 'info');
          }
          setPendingDisable(null);
        }}
      />
    </>
  );
}

function RuleRow({
  rule,
  busy,
  onToggle,
}: {
  rule: ComplianceRule;
  busy: boolean;
  onToggle: (enabled: boolean) => void;
}) {
  return (
    <li className="flex items-start justify-between gap-4 py-3">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <p className="text-sm font-medium text-slate-800">{rule.name}</p>
          <Badge
            className={
              rule.severity === 'CRITICAL'
                ? 'bg-red-50 text-red-700 ring-red-200'
                : 'bg-amber-50 text-amber-800 ring-amber-200'
            }
          >
            {rule.severity}
          </Badge>
          <span className="font-mono text-xs text-slate-400">{rule.code}</span>
        </div>
        <p className="mt-0.5 text-xs text-slate-500">{rule.description}</p>
      </div>

      <label className="flex shrink-0 cursor-pointer items-center gap-2">
        <span className="text-xs text-slate-500">{rule.enabled ? 'On' : 'Off'}</span>
        <input
          type="checkbox"
          className="peer sr-only"
          checked={rule.enabled}
          disabled={busy}
          onChange={(e) => onToggle(e.target.checked)}
          aria-label={`${rule.enabled ? 'Disable' : 'Enable'} ${rule.name}`}
        />
        <span className="relative h-5 w-9 rounded-full bg-slate-300 transition-colors peer-checked:bg-emerald-500 peer-focus-visible:ring-2 peer-focus-visible:ring-brand-500 peer-focus-visible:ring-offset-2">
          <span className="absolute left-0.5 top-0.5 h-4 w-4 rounded-full bg-white transition-transform peer-checked:translate-x-4" />
        </span>
      </label>
    </li>
  );
}

function ConfigRow({
  label,
  value,
  active,
  activeLabel,
}: {
  label: string;
  value: string;
  active: boolean;
  activeLabel?: string;
}) {
  return (
    <div className="flex items-start justify-between gap-3">
      <dt className="text-xs text-slate-600">{label}</dt>
      <dd className="shrink-0 text-right">
        <p className={`text-xs font-medium ${active ? 'text-slate-900' : 'text-slate-400'}`}>
          {value}
        </p>
        {activeLabel && active && <p className="text-xs text-slate-400">{activeLabel}</p>}
      </dd>
    </div>
  );
}

function ContentChecker() {
  const [subject, setSubject] = useState('');
  const [body, setBody] = useState('');
  const [channel, setChannel] = useState<Channel>('EMAIL');
  const [report, setReport] = useState<ComplianceReport | null>(null);

  const check = useMutation(async () => {
    const result = await api.post<ComplianceReport>('/api/v1/compliance/check-content', {
      subject,
      body,
      channel,
    });
    setReport(result);
    return result;
  });

  return (
    <Card
      title="Test some copy"
      description="Paste any draft to see exactly which rules it trips, before it reaches a campaign."
    >
      <div className="grid gap-3 sm:grid-cols-[160px_1fr]">
        <div>
          <label className="label" htmlFor="check-channel">
            Channel
          </label>
          <select
            id="check-channel"
            className="input"
            value={channel}
            onChange={(e) => setChannel(e.target.value as Channel)}
          >
            {(['EMAIL', 'SMS', 'WHATSAPP'] as Channel[]).map((c) => (
              <option key={c} value={c}>
                {humanize(c)}
              </option>
            ))}
          </select>
        </div>
        {channel === 'EMAIL' && (
          <div>
            <label className="label" htmlFor="check-subject">
              Subject
            </label>
            <input
              id="check-subject"
              className="input"
              value={subject}
              onChange={(e) => setSubject(e.target.value)}
            />
          </div>
        )}
      </div>

      <div className="mt-3">
        <label className="label" htmlFor="check-body">
          Body
        </label>
        <textarea
          id="check-body"
          className="input min-h-[120px]"
          value={body}
          onChange={(e) => setBody(e.target.value)}
          placeholder="Paste marketing copy here…"
        />
      </div>

      <button
        type="button"
        className="btn-primary mt-3"
        onClick={() => check.run()}
        disabled={!body.trim() || check.loading}
      >
        {check.loading && <Spinner className="h-4 w-4 text-white" />}
        Check copy
      </button>

      {report && (
        <div className="mt-4">
          <div
            className={`rounded-lg border px-4 py-3 ${
              report.passed ? 'border-emerald-200 bg-emerald-50' : 'border-red-200 bg-red-50'
            }`}
          >
            <p
              className={`text-sm font-medium ${
                report.passed ? 'text-emerald-800' : 'text-red-800'
              }`}
            >
              {report.passed
                ? 'No blocking findings'
                : `${report.blocking_count} blocking finding${
                    report.blocking_count === 1 ? '' : 's'
                  }`}
            </p>
          </div>

          {report.findings.length > 0 && (
            <>
              <SectionTitle>Findings</SectionTitle>
              <ul className="space-y-2">
                {report.findings.map((f, index) => (
                  <li
                    key={`${f.code}-${index}`}
                    className="flex items-start gap-2 rounded-lg border border-slate-200 px-3 py-2"
                  >
                    <Badge
                      className={
                        f.blocks_send
                          ? 'bg-red-50 text-red-700 ring-red-200'
                          : 'bg-amber-50 text-amber-800 ring-amber-200'
                      }
                    >
                      {f.blocks_send ? 'Blocks' : 'Warning'}
                    </Badge>
                    <div className="min-w-0">
                      <p className="font-mono text-xs font-medium text-slate-700">{f.code}</p>
                      <p className="text-xs text-slate-600">{f.message}</p>
                      {f.excerpt && (
                        <p className="mt-1 inline-block rounded bg-slate-100 px-1.5 py-0.5 font-mono text-xs">
                          “{f.excerpt}”
                        </p>
                      )}
                    </div>
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}
    </Card>
  );
}
