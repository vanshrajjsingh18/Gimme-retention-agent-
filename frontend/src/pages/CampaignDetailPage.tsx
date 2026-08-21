import { useEffect, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';

import { api } from '../api/client';
import {
  Badge,
  Card,
  ConfirmDialog,
  EmptyState,
  ErrorState,
  Field,
  LoadingState,
  Modal,
  PageHeader,
  SectionTitle,
  Spinner,
  StatTile,
  TableShell,
  notify,
} from '../components/ui';
import { useMutation, useQuery } from '../hooks/useApi';
import type { AudiencePreview, Campaign, ComplianceReport } from '../types';
import { formatCurrency, formatDateTime, formatNumber, formatPercent, humanize } from '../utils/format';
import { CAMPAIGN_STATUS_BADGE, LIFECYCLE_BADGE } from '../utils/theme';

const EDITABLE = new Set([
  'DRAFT',
  'AI_GENERATED',
  'VALIDATED',
  'COMPLIANCE_CHECKED',
  'AWAITING_APPROVAL',
]);

/** The workflow steps shown as a progress rail at the top of the page. */
const STEPS = [
  { key: 'content', label: 'Message' },
  { key: 'audience', label: 'Audience' },
  { key: 'compliance', label: 'Compliance' },
  { key: 'approval', label: 'Approval' },
  { key: 'send', label: 'Send' },
] as const;

export default function CampaignDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { data: campaign, loading, error, refetch } = useQuery<Campaign>(
    id ? `/api/v1/campaigns/${id}` : null,
  );

  const [subject, setSubject] = useState('');
  const [body, setBody] = useState('');
  const [dirty, setDirty] = useState(false);
  const [showTest, setShowTest] = useState(false);
  const [showRun, setShowRun] = useState(false);
  const [audience, setAudience] = useState<AudiencePreview | null>(null);

  useEffect(() => {
    if (campaign) {
      setSubject(campaign.subject);
      setBody(campaign.body);
      setDirty(false);
    }
  }, [campaign?.id, campaign?.updated_at]);

  const save = useMutation(async () =>
    api.patch<Campaign>(`/api/v1/campaigns/${id}`, { subject, body }),
  );
  const loadAudience = useMutation(async () => {
    const result = await api.get<AudiencePreview>(`/api/v1/campaigns/${id}/audience`);
    setAudience(result);
    return result;
  });
  const check = useMutation(async () =>
    api.post<ComplianceReport>(`/api/v1/campaigns/${id}/compliance-check`),
  );
  const submit = useMutation(async () => api.post<Campaign>(`/api/v1/campaigns/${id}/submit`));
  const approve = useMutation(async () => api.post<Campaign>(`/api/v1/campaigns/${id}/approve`));
  const snapshot = useMutation(async () =>
    api.post<AudiencePreview>(`/api/v1/campaigns/${id}/audience/snapshot`),
  );
  const run = useMutation(async () =>
    api.post<Record<string, number | string | boolean>>(`/api/v1/campaigns/${id}/run`, {
      generate_per_customer: true,
      simulate_engagement: true,
    }),
  );
  const cancel = useMutation(async () => api.post<Campaign>(`/api/v1/campaigns/${id}/cancel`));
  const sendTest = useMutation(async (to: string) =>
    api.post<{ success: boolean; is_simulated: boolean; error: string | null }>(
      `/api/v1/campaigns/${id}/send-test`,
      { to },
    ),
  );

  // Load the audience breakdown once the campaign is known.
  useEffect(() => {
    if (campaign) loadAudience.run();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [campaign?.id]);

  if (loading) return <LoadingState label="Loading campaign…" />;
  if (error) return <ErrorState message={error} onRetry={refetch} />;
  if (!campaign) return null;

  const editable = EDITABLE.has(campaign.status);
  const compliance = (campaign.compliance_result ?? {}) as ComplianceReport;
  const hasCompliance = 'passed' in compliance;
  const compliancePassed = hasCompliance && compliance.passed;
  const isApproved = ['APPROVED', 'SCHEDULED', 'RUNNING', 'COMPLETED'].includes(campaign.status);
  const isSent = campaign.messages_sent > 0;

  const stepState: Record<string, 'done' | 'current' | 'todo'> = {
    content: body.trim() ? 'done' : 'current',
    audience: (audience?.eligible_count ?? 0) > 0 ? 'done' : body.trim() ? 'current' : 'todo',
    compliance: compliancePassed ? 'done' : hasCompliance ? 'current' : 'todo',
    approval: isApproved ? 'done' : compliancePassed ? 'current' : 'todo',
    send: isSent ? 'done' : isApproved ? 'current' : 'todo',
  };

  return (
    <>
      <button
        type="button"
        className="btn-ghost mb-3 -ml-2 px-2 py-1 text-xs"
        onClick={() => navigate('/campaigns')}
      >
        ← All campaigns
      </button>

      <PageHeader
        title={campaign.name}
        description={`${humanize(campaign.objective)} · ${campaign.channel} · ${
          campaign.segment_name ?? 'All customers'
        }`}
        actions={
          <>
            {campaign.status !== 'COMPLETED' && campaign.status !== 'CANCELLED' && (
              <button
                type="button"
                className="btn-secondary"
                onClick={async () => {
                  await cancel.run();
                  notify('Campaign cancelled.', 'info');
                  refetch();
                }}
                disabled={cancel.loading}
              >
                Cancel campaign
              </button>
            )}
            <Badge
              className={
                CAMPAIGN_STATUS_BADGE[campaign.status] ??
                'bg-slate-100 text-slate-700 ring-slate-200'
              }
            >
              {humanize(campaign.status)}
            </Badge>
          </>
        }
      />

      <ol className="mb-5 flex flex-wrap gap-2">
        {STEPS.map((step, index) => {
          const state = stepState[step.key];
          return (
            <li key={step.key} className="flex items-center gap-2">
              <span
                className={`flex h-6 w-6 items-center justify-center rounded-full text-xs font-semibold ${
                  state === 'done'
                    ? 'bg-emerald-500 text-white'
                    : state === 'current'
                      ? 'bg-brand-600 text-white'
                      : 'bg-slate-200 text-slate-500'
                }`}
              >
                {state === 'done' ? '✓' : index + 1}
              </span>
              <span
                className={`text-sm ${
                  state === 'todo' ? 'text-slate-400' : 'font-medium text-slate-700'
                }`}
              >
                {step.label}
              </span>
              {index < STEPS.length - 1 && <span className="text-slate-300">→</span>}
            </li>
          );
        })}
      </ol>

      {isSent && (
        <section className="mb-4 grid grid-cols-2 gap-4 lg:grid-cols-5">
          <StatTile label="Sent" value={formatNumber(campaign.messages_sent)} />
          <StatTile
            label="Delivered"
            value={formatPercent(
              campaign.messages_sent ? campaign.messages_delivered / campaign.messages_sent : 0,
            )}
            sublabel={`${formatNumber(campaign.messages_delivered)} messages`}
          />
          <StatTile
            label="Opened"
            value={formatPercent(
              campaign.messages_delivered
                ? campaign.messages_opened / campaign.messages_delivered
                : 0,
            )}
            sublabel={`${formatNumber(campaign.messages_opened)} opens`}
          />
          <StatTile
            label="Conversions"
            value={formatNumber(campaign.conversions)}
            tone="positive"
            sublabel={formatPercent(
              campaign.messages_sent ? campaign.conversions / campaign.messages_sent : 0,
            )}
          />
          <StatTile
            label="Attributed revenue"
            value={formatCurrency(campaign.attributed_revenue)}
            tone="positive"
            sublabel={`${formatCurrency(
              campaign.messages_sent ? campaign.attributed_revenue / campaign.messages_sent : 0,
              true,
            )} per message`}
          />
        </section>
      )}

      <div className="grid gap-4 lg:grid-cols-3">
        <div className="space-y-4 lg:col-span-2">
          <Card
            title="Message"
            description={
              editable
                ? 'This is the campaign-level copy. Per-customer personalisation is generated at send time.'
                : 'Locked: the campaign has been approved or sent.'
            }
            actions={
              editable ? (
                <button
                  type="button"
                  className="btn-secondary px-2.5 py-1 text-xs"
                  onClick={async () => {
                    const result = await save.run();
                    if (result) {
                      notify('Message saved. Re-run the compliance check.');
                      refetch();
                    }
                  }}
                  disabled={!dirty || save.loading}
                >
                  {save.loading && <Spinner className="h-4 w-4" />}
                  Save
                </button>
              ) : undefined
            }
          >
            {campaign.channel === 'EMAIL' && (
              <div className="mb-3">
                <label className="label" htmlFor="campaign-subject">
                  Subject
                </label>
                <input
                  id="campaign-subject"
                  className="input"
                  value={subject}
                  onChange={(e) => {
                    setSubject(e.target.value);
                    setDirty(true);
                  }}
                  disabled={!editable}
                />
              </div>
            )}
            <label className="label" htmlFor="campaign-body">
              Body
            </label>
            <textarea
              id="campaign-body"
              className="input min-h-[220px] leading-relaxed"
              value={body}
              onChange={(e) => {
                setBody(e.target.value);
                setDirty(true);
              }}
              disabled={!editable}
            />
            {dirty && (
              <p className="mt-2 text-xs text-amber-700">
                Unsaved changes. Saving resets the campaign to draft and clears any approval.
              </p>
            )}
            {save.error && (
              <p className="mt-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
                {save.error}
              </p>
            )}
          </Card>

          <Card
            title="Compliance"
            description="Alcohol marketing rules and grounding checks. A critical finding blocks sending."
            actions={
              <button
                type="button"
                className="btn-secondary px-2.5 py-1 text-xs"
                onClick={async () => {
                  const result = await check.run();
                  if (result) {
                    notify(
                      result.passed
                        ? 'Compliance checks passed.'
                        : `${result.blocking_count} blocking finding${
                            result.blocking_count === 1 ? '' : 's'
                          }.`,
                      result.passed ? 'success' : 'error',
                    );
                    refetch();
                  }
                }}
                disabled={check.loading}
              >
                {check.loading && <Spinner className="h-4 w-4" />}
                Run check
              </button>
            }
          >
            {!hasCompliance ? (
              <EmptyState
                title="Not checked yet"
                description="Run the compliance check before submitting for approval."
              />
            ) : (
              <>
                <div
                  className={`rounded-lg border px-4 py-3 ${
                    compliance.passed
                      ? 'border-emerald-200 bg-emerald-50'
                      : 'border-red-200 bg-red-50'
                  }`}
                >
                  <p
                    className={`text-sm font-medium ${
                      compliance.passed ? 'text-emerald-800' : 'text-red-800'
                    }`}
                  >
                    {compliance.passed
                      ? 'No blocking findings'
                      : `${compliance.blocking_count} blocking finding${
                          compliance.blocking_count === 1 ? '' : 's'
                        } — sending is blocked`}
                  </p>
                  {compliance.checked_at && (
                    <p className="mt-0.5 text-xs text-slate-600">
                      Checked {formatDateTime(compliance.checked_at)}
                    </p>
                  )}
                </div>

                {(compliance.findings ?? []).length > 0 && (
                  <ul className="mt-3 space-y-2">
                    {compliance.findings.map((f, index) => (
                      <li
                        key={`${f.code}-${index}`}
                        className="flex items-start gap-2 rounded-lg border border-slate-200 px-3 py-2"
                      >
                        <Badge
                          className={
                            f.severity === 'CRITICAL'
                              ? 'bg-red-50 text-red-700 ring-red-200'
                              : f.severity === 'WARNING'
                                ? 'bg-amber-50 text-amber-800 ring-amber-200'
                                : 'bg-slate-100 text-slate-600 ring-slate-200'
                          }
                        >
                          {f.severity}
                        </Badge>
                        <div className="min-w-0">
                          <p className="font-mono text-xs font-medium text-slate-700">{f.code}</p>
                          <p className="text-xs text-slate-600">{f.message}</p>
                          {f.excerpt && (
                            <p className="mt-1 inline-block rounded bg-slate-100 px-1.5 py-0.5 font-mono text-xs text-slate-700">
                              “{f.excerpt}”
                            </p>
                          )}
                        </div>
                      </li>
                    ))}
                  </ul>
                )}
              </>
            )}
          </Card>

          <RecipientsCard campaignId={campaign.id} isSent={isSent} />
        </div>

        <div className="space-y-4">
          <Card
            title="Audience"
            description="Who is eligible right now."
            actions={
              <button
                type="button"
                className="btn-secondary px-2.5 py-1 text-xs"
                onClick={() => loadAudience.run()}
                disabled={loadAudience.loading}
              >
                {loadAudience.loading && <Spinner className="h-4 w-4" />}
                Refresh
              </button>
            }
          >
            {loadAudience.error ? (
              <p className="text-sm text-red-700">{loadAudience.error}</p>
            ) : !audience ? (
              <LoadingState label="Calculating…" />
            ) : (
              <>
                <div className="mb-4 flex items-baseline gap-2">
                  <span className="text-3xl font-semibold tabular-nums text-slate-900">
                    {formatNumber(audience.eligible_count)}
                  </span>
                  <span className="text-sm text-slate-500">
                    eligible of {formatNumber(audience.audience_size)}
                  </span>
                </div>

                {Object.keys(audience.excluded_by_reason).length > 0 && (
                  <>
                    <SectionTitle>Excluded</SectionTitle>
                    <ul className="space-y-2">
                      {Object.entries(audience.excluded_by_reason).map(([reason, count]) => (
                        <li key={reason}>
                          <div className="flex items-baseline justify-between gap-2">
                            <span className="text-xs font-medium text-slate-700">
                              {humanize(reason.replace('EXCLUDED_', ''))}
                            </span>
                            <span className="shrink-0 text-xs tabular-nums text-slate-500">
                              {formatNumber(count)}
                            </span>
                          </div>
                          {(audience.exclusion_samples[reason] ?? []).slice(0, 1).map((sample) => (
                            <p key={sample.id} className="text-xs text-slate-400">
                              e.g. {sample.reason}
                            </p>
                          ))}
                        </li>
                      ))}
                    </ul>
                  </>
                )}

                {audience.sample_recipients.length > 0 && (
                  <div className="mt-4 border-t border-slate-100 pt-3">
                    <SectionTitle>Sample recipients</SectionTitle>
                    <ul className="space-y-1.5">
                      {audience.sample_recipients.slice(0, 5).map((r) => (
                        <li key={r.id} className="flex items-center justify-between gap-2">
                          <Link
                            to={`/customers/${r.id}`}
                            className="truncate text-xs text-brand-700 hover:text-brand-800"
                          >
                            {r.full_name}
                          </Link>
                          <Badge
                            className={
                              LIFECYCLE_BADGE[
                                r.lifecycle_stage as keyof typeof LIFECYCLE_BADGE
                              ] ?? 'bg-slate-100 text-slate-600 ring-slate-200'
                            }
                          >
                            {humanize(r.lifecycle_stage)}
                          </Badge>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </>
            )}
          </Card>

          <Card title="Actions">
            <div className="space-y-2">
              <button
                type="button"
                className="btn-secondary w-full"
                onClick={() => setShowTest(true)}
              >
                Send test message
              </button>

              <button
                type="button"
                className="btn-secondary w-full"
                onClick={async () => {
                  const result = await submit.run();
                  if (result) {
                    notify('Submitted for approval.');
                    refetch();
                  }
                }}
                disabled={submit.loading || !compliancePassed || isApproved}
                title={
                  !compliancePassed
                    ? 'Compliance checks must pass before submitting.'
                    : undefined
                }
              >
                {submit.loading && <Spinner className="h-4 w-4" />}
                Submit for approval
              </button>

              <button
                type="button"
                className="btn-primary w-full"
                onClick={async () => {
                  const result = await approve.run();
                  if (result) {
                    notify('Campaign approved. It can now be sent.');
                    refetch();
                  }
                }}
                disabled={
                  approve.loading ||
                  isApproved ||
                  campaign.status !== 'AWAITING_APPROVAL'
                }
                title={
                  campaign.status !== 'AWAITING_APPROVAL'
                    ? 'Submit the campaign for approval first.'
                    : undefined
                }
              >
                {approve.loading && <Spinner className="h-4 w-4 text-white" />}
                Approve
              </button>

              <button
                type="button"
                className="btn-primary w-full"
                onClick={() => setShowRun(true)}
                disabled={!isApproved || campaign.status === 'COMPLETED'}
                title={!isApproved ? 'A campaign must be approved before it can send.' : undefined}
              >
                Run campaign
              </button>
            </div>

            {(submit.error || approve.error || run.error) && (
              <p className="mt-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
                {submit.error ?? approve.error ?? run.error}
              </p>
            )}

            <dl className="mt-4 space-y-2 border-t border-slate-100 pt-3">
              <Field label="Attribution window" value={`${campaign.attribution_window_hours} hours`} />
              <Field label="Approved" value={formatDateTime(campaign.approved_at)} />
              <Field label="Started" value={formatDateTime(campaign.started_at)} />
              <Field label="Completed" value={formatDateTime(campaign.completed_at)} />
            </dl>
          </Card>
        </div>
      </div>

      <TestSendDialog
        open={showTest}
        busy={sendTest.loading}
        onCancel={() => setShowTest(false)}
        onSend={async (to) => {
          const result = await sendTest.run(to);
          setShowTest(false);
          if (result) {
            notify(
              result.success
                ? `Test sent${result.is_simulated ? ' (simulated — nothing left this machine)' : ''}.`
                : `Test failed: ${result.error}`,
              result.success ? 'success' : 'error',
            );
          }
        }}
      />

      <ConfirmDialog
        open={showRun}
        title="Run this campaign?"
        message={`This will generate and send a personalised message to each of the ${formatNumber(
          audience?.eligible_count ?? 0,
        )} eligible recipients. Eligibility is re-checked for every customer at send time.`}
        confirmLabel="Run campaign"
        busy={run.loading}
        onCancel={() => setShowRun(false)}
        onConfirm={async () => {
          await snapshot.run();
          const stats = await run.run();
          setShowRun(false);
          if (stats) {
            notify(
              `Sent ${stats.sent} messages${stats.is_mock ? ' in mock mode' : ''}. ` +
                `${stats.skipped_ineligible} skipped, ${stats.failed} failed.`,
            );
            refetch();
          }
        }}
      />
    </>
  );
}

function RecipientsCard({ campaignId, isSent }: { campaignId: number; isSent: boolean }) {
  const [status, setStatus] = useState('');
  const { data, loading } = useQuery<{
    recipients: {
      customer_id: number;
      full_name: string;
      email: string | null;
      lifecycle_stage: string;
      status: string;
      exclusion_reason: string | null;
      sent_at: string | null;
      opened_at: string | null;
      converted_at: string | null;
    }[];
  }>(
    `/api/v1/campaigns/${campaignId}/recipients?limit=200${status ? `&status=${status}` : ''}`,
    [status],
  );

  if (!isSent && !data?.recipients.length) return null;

  return (
    <Card title="Recipients" bodyClassName="">
      <div className="flex flex-wrap gap-1.5 px-5 pb-3 pt-4">
        {['', 'DELIVERED', 'SENT', 'CONVERTED', 'FAILED', 'EXCLUDED_NO_CONSENT', 'EXCLUDED_AGE'].map(
          (s) => (
            <button
              key={s || 'all'}
              type="button"
              onClick={() => setStatus(s)}
              aria-pressed={status === s}
              className={`rounded-md px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${
                status === s
                  ? 'bg-brand-50 text-brand-700 ring-brand-300'
                  : 'bg-white text-slate-600 ring-slate-200 hover:bg-slate-50'
              }`}
            >
              {s ? humanize(s.replace('EXCLUDED_', 'Excluded: ')) : 'All'}
            </button>
          ),
        )}
      </div>

      {loading ? (
        <LoadingState label="Loading recipients…" />
      ) : !data || data.recipients.length === 0 ? (
        <EmptyState title="No recipients with this status" />
      ) : (
        <TableShell>
          <thead className="bg-slate-50">
            <tr>
              <th className="table-head">Customer</th>
              <th className="table-head">Status</th>
              <th className="table-head">Sent</th>
              <th className="table-head">Opened</th>
              <th className="table-head">Converted</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {data.recipients.map((r) => (
              <tr key={r.customer_id} className="hover:bg-slate-50">
                <td className="table-cell">
                  <Link
                    to={`/customers/${r.customer_id}`}
                    className="font-medium text-brand-700 hover:text-brand-800"
                  >
                    {r.full_name}
                  </Link>
                  <p className="text-xs text-slate-500">{r.email ?? '—'}</p>
                </td>
                <td className="table-cell">
                  <Badge
                    className={
                      r.status.startsWith('EXCLUDED')
                        ? 'bg-slate-100 text-slate-600 ring-slate-200'
                        : r.status === 'CONVERTED'
                          ? 'bg-emerald-50 text-emerald-700 ring-emerald-200'
                          : r.status === 'FAILED'
                            ? 'bg-red-50 text-red-700 ring-red-200'
                            : 'bg-blue-50 text-blue-700 ring-blue-200'
                    }
                  >
                    {humanize(r.status)}
                  </Badge>
                  {r.exclusion_reason && (
                    <p className="mt-0.5 max-w-xs whitespace-normal text-xs text-slate-500">
                      {r.exclusion_reason}
                    </p>
                  )}
                </td>
                <td className="table-cell text-xs">{formatDateTime(r.sent_at)}</td>
                <td className="table-cell text-xs">{formatDateTime(r.opened_at)}</td>
                <td className="table-cell text-xs">{formatDateTime(r.converted_at)}</td>
              </tr>
            ))}
          </tbody>
        </TableShell>
      )}
    </Card>
  );
}

function TestSendDialog({
  open,
  busy,
  onCancel,
  onSend,
}: {
  open: boolean;
  busy: boolean;
  onCancel: () => void;
  onSend: (to: string) => void;
}) {
  const [to, setTo] = useState('');
  return (
    <Modal
      open={open}
      title="Send a test message"
      description="Goes to one address only and never touches campaign metrics."
      onClose={onCancel}
      size="sm"
      footer={
        <>
          <button type="button" className="btn-secondary" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
          <button
            type="button"
            className="btn-primary"
            onClick={() => onSend(to)}
            disabled={busy || to.trim().length < 3}
          >
            {busy && <Spinner className="h-4 w-4 text-white" />}
            Send test
          </button>
        </>
      }
    >
      <label className="label" htmlFor="test-to">
        Send to
      </label>
      <input
        id="test-to"
        className="input"
        placeholder="you@gimmedelivery.co.nz"
        value={to}
        onChange={(e) => setTo(e.target.value)}
      />
    </Modal>
  );
}
