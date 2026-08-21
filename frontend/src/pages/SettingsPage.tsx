import { useState } from 'react';

import { api } from '../api/client';
import {
  Badge,
  Card,
  ConfirmDialog,
  EmptyState,
  ErrorState,
  Field,
  LoadingState,
  PageHeader,
  SectionTitle,
  Spinner,
  TableShell,
  notify,
} from '../components/ui';
import { useMutation, useQuery } from '../hooks/useApi';
import { useAuth } from '../hooks/useAuth';
import type { SystemStatus } from '../types';
import { formatDateTime, formatNumber, humanize } from '../utils/format';

export default function SettingsPage() {
  const { user } = useAuth();
  const { data: status, loading, error, refetch } = useQuery<SystemStatus>(
    '/api/v1/system/status',
  );
  const [showSeed, setShowSeed] = useState(false);
  const [seedCount, setSeedCount] = useState(1000);

  const recalc = useMutation(async () =>
    api.post<{ customers_processed: number; rfm_scored: number }>(
      '/api/v1/analytics/recalculate',
    ),
  );

  const seed = useMutation(async () =>
    api.post<{ totals: Record<string, number> }>(
      `/api/v1/system/seed-demo-data?customers=${seedCount}&reset=true&include_campaigns=true`,
    ),
  );

  if (loading) return <LoadingState label="Loading system status…" />;
  if (error) return <ErrorState message={error} onRetry={refetch} />;
  if (!status) return null;

  return (
    <>
      <PageHeader title="Settings" description="System status, maintenance and the audit trail." />

      <div className="grid gap-4 lg:grid-cols-3">
        <Card title="System" className="lg:col-span-2">
          <dl className="grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-3">
            <Field label="Application" value={status.app_name} />
            <Field label="Environment" value={humanize(status.environment)} />
            <Field
              label="Operating mode"
              value={
                status.mock_mode ? (
                  <Badge className="bg-amber-50 text-amber-800 ring-amber-200">Mock</Badge>
                ) : (
                  <Badge className="bg-emerald-50 text-emerald-700 ring-emerald-200">Live</Badge>
                )
              }
            />
            <Field label="LLM provider" value={`${status.llm.provider} (${status.llm.mode})`} />
            <Field label="LLM model" value={status.llm.model} />
            <Field
              label="Scheduler"
              value={
                status.scheduler.running ? (
                  <Badge className="bg-emerald-50 text-emerald-700 ring-emerald-200">Running</Badge>
                ) : (
                  <Badge className="bg-slate-100 text-slate-600 ring-slate-200">Stopped</Badge>
                )
              }
            />
          </dl>

          {status.scheduler.jobs.length > 0 && (
            <div className="mt-5 border-t border-slate-100 pt-4">
              <SectionTitle>Background jobs</SectionTitle>
              <ul className="space-y-1.5">
                {status.scheduler.jobs.map((job) => (
                  <li key={job.id} className="flex justify-between gap-3 text-xs">
                    <span className="text-slate-700">{humanize(job.id)}</span>
                    <span className="text-slate-500">
                      next {formatDateTime(job.next_run_at)}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          <div className="mt-5 border-t border-slate-100 pt-4">
            <SectionTitle>Message providers</SectionTitle>
            <ul className="space-y-1.5">
              {status.integrations.map((integration) => (
                <li key={integration.provider} className="flex items-center justify-between gap-3">
                  <span className="text-sm text-slate-700">
                    {humanize(integration.provider)}{' '}
                    <span className="text-xs text-slate-400">{integration.channel}</span>
                  </span>
                  <Badge
                    className={
                      integration.mode === 'mock'
                        ? 'bg-amber-50 text-amber-800 ring-amber-200'
                        : 'bg-emerald-50 text-emerald-700 ring-emerald-200'
                    }
                  >
                    {integration.mode === 'mock' ? 'Mock' : humanize(integration.status)}
                  </Badge>
                </li>
              ))}
            </ul>
          </div>
        </Card>

        <div className="space-y-4">
          <Card title="Your account">
            <dl className="space-y-3">
              <Field label="Name" value={user?.full_name ?? '—'} />
              <Field label="Email" value={user?.email ?? '—'} />
              <Field label="Role" value={humanize(user?.role ?? '')} />
            </dl>
          </Card>

          <Card title="Data volume">
            <dl className="space-y-2">
              {Object.entries(status.data).map(([key, value]) => (
                <div key={key} className="flex justify-between gap-3">
                  <dt className="text-xs text-slate-600">{humanize(key)}</dt>
                  <dd className="text-xs font-medium tabular-nums text-slate-900">
                    {formatNumber(value)}
                  </dd>
                </div>
              ))}
            </dl>
          </Card>
        </div>
      </div>

      <Card title="Maintenance" className="mt-4">
        <div className="grid gap-4 sm:grid-cols-2">
          <div>
            <p className="text-sm font-medium text-slate-800">Recalculate intelligence</p>
            <p className="mt-1 text-xs text-slate-500">
              Recomputes metrics, lifecycle stage, churn score, RFM and next best action for every
              customer, then refreshes segment membership. Safe to run any time.
            </p>
            <button
              type="button"
              className="btn-secondary mt-3"
              disabled={recalc.loading}
              onClick={async () => {
                const result = await recalc.run();
                if (result) {
                  notify(
                    `Recalculated ${formatNumber(result.customers_processed)} customers.`,
                  );
                  refetch();
                }
              }}
            >
              {recalc.loading && <Spinner className="h-4 w-4" />}
              Recalculate now
            </button>
          </div>

          <div>
            <p className="text-sm font-medium text-slate-800">Regenerate demo data</p>
            <p className="mt-1 text-xs text-slate-500">
              Deletes all customer and transactional data and regenerates a fresh synthetic
              dataset. Brand settings, compliance rules, integrations and API keys are kept.
            </p>
            <div className="mt-3 flex gap-2">
              <input
                type="number"
                min={10}
                max={5000}
                className="input max-w-[130px]"
                value={seedCount}
                onChange={(e) => setSeedCount(Number(e.target.value))}
                aria-label="Number of customers"
              />
              <button type="button" className="btn-danger" onClick={() => setShowSeed(true)}>
                Regenerate
              </button>
            </div>
          </div>
        </div>
      </Card>

      <AuditLogCard />

      <ConfirmDialog
        open={showSeed}
        title="Regenerate demo data?"
        message={`This permanently deletes every customer, order, campaign and message, then generates ${formatNumber(
          seedCount,
        )} new synthetic customers. Configuration is preserved. This cannot be undone.`}
        confirmLabel="Delete and regenerate"
        destructive
        busy={seed.loading}
        onCancel={() => setShowSeed(false)}
        onConfirm={async () => {
          const result = await seed.run();
          setShowSeed(false);
          if (result) {
            notify(
              `Generated ${formatNumber(result.totals.customers)} customers and ${formatNumber(
                result.totals.orders,
              )} orders.`,
            );
            refetch();
          }
        }}
      />
    </>
  );
}

function AuditLogCard() {
  const { data, loading } = useQuery<{
    entries: {
      id: number;
      actor: string;
      action: string;
      entity_type: string;
      entity_id: string;
      detail: Record<string, unknown>;
      created_at: string;
    }[];
  }>('/api/v1/system/audit-log?limit=40');

  return (
    <Card
      title="Audit log"
      description="Approvals, consent changes, suppressions and compliance rule changes."
      className="mt-4"
      bodyClassName=""
    >
      {loading ? (
        <LoadingState />
      ) : !data || data.entries.length === 0 ? (
        <EmptyState title="No audit entries yet" />
      ) : (
        <TableShell>
          <thead className="bg-slate-50">
            <tr>
              <th className="table-head">Action</th>
              <th className="table-head">Actor</th>
              <th className="table-head">Entity</th>
              <th className="table-head">Detail</th>
              <th className="table-head">When</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {data.entries.map((entry) => (
              <tr key={entry.id} className="hover:bg-slate-50">
                <td className="table-cell">
                  <Badge className="bg-slate-100 text-slate-700 ring-slate-200">
                    {humanize(entry.action)}
                  </Badge>
                </td>
                <td className="table-cell text-xs">{entry.actor}</td>
                <td className="table-cell text-xs text-slate-500">
                  {entry.entity_type}
                  {entry.entity_id ? ` #${entry.entity_id}` : ''}
                </td>
                <td className="table-cell">
                  <p className="max-w-md truncate font-mono text-xs text-slate-500">
                    {Object.keys(entry.detail ?? {}).length > 0
                      ? JSON.stringify(entry.detail)
                      : '—'}
                  </p>
                </td>
                <td className="table-cell text-xs text-slate-500">
                  {formatDateTime(entry.created_at)}
                </td>
              </tr>
            ))}
          </tbody>
        </TableShell>
      )}
    </Card>
  );
}
