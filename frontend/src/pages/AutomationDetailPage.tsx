import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';

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
  StatTile,
  TableShell,
  notify,
} from '../components/ui';
import { useMutation, useQuery } from '../hooks/useApi';
import type {
  Automation,
  AutomationEnrollment,
  AutomationRunReport,
  AutomationSend,
  AutomationStats,
} from '../types';
import { formatDateTime, formatNumber, humanize } from '../utils/format';
import {
  AUTOMATION_KIND_LABEL,
  AUTOMATION_STATUS_BADGE,
  SEND_STATUS_BADGE,
  SKIP_REASON_LABEL,
} from '../utils/theme';

export default function AutomationDetailPage() {
  const { id } = useParams();
  const base = `/api/v1/automations/${id}`;

  const { data: automation, loading, error, refetch } = useQuery<Automation>(base);
  const { data: stats, refetch: refetchStats } = useQuery<AutomationStats>(`${base}/stats`);
  const { data: sends, refetch: refetchSends } = useQuery<AutomationSend[]>(`${base}/sends?limit=50`);
  const { data: enrollments } = useQuery<AutomationEnrollment[]>(`${base}/enrollments?limit=50`);

  const [report, setReport] = useState<AutomationRunReport | null>(null);
  const [confirmRun, setConfirmRun] = useState(false);

  const dryRun = useMutation(async () => api.post<AutomationRunReport>(`${base}/preview`));
  const liveRun = useMutation(async () => api.post<AutomationRunReport>(`${base}/run`));
  const lifecycle = useMutation(async (action: string) =>
    api.post<Automation>(`${base}/${action}`),
  );

  if (loading) return <LoadingState label="Loading automation…" />;
  if (error || !automation) return <ErrorState message={error ?? 'Not found.'} onRetry={refetch} />;

  const needsApproval = automation.require_approval && !automation.approved_at;
  const isActive = automation.status === 'ACTIVE';

  async function act(action: string, message: string) {
    const result = await lifecycle.run(action);
    if (result) {
      notify(message);
      refetch();
      refetchStats();
    }
  }

  return (
    <>
      <PageHeader
        title={automation.name}
        description={
          automation.description ||
          `${AUTOMATION_KIND_LABEL[automation.kind] ?? automation.kind} over SMS.`
        }
        actions={
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              className="btn-secondary"
              disabled={dryRun.loading}
              onClick={async () => {
                const result = await dryRun.run();
                if (result) {
                  setReport(result);
                  notify(
                    `Dry run: ${result.previewed} would receive a message, ${result.skipped} would not.`,
                    'info',
                  );
                }
              }}
            >
              {dryRun.loading && <Spinner className="h-4 w-4" />}
              Dry run
            </button>
            {needsApproval && (
              <button
                type="button"
                className="btn-secondary"
                onClick={() => act('approve', 'Approved. It can now be activated.')}
              >
                Approve
              </button>
            )}
            {automation.status === 'DRAFT' && !needsApproval && (
              <button
                type="button"
                className="btn-primary"
                onClick={() => act('activate', 'Automation activated.')}
              >
                Activate
              </button>
            )}
            {automation.status === 'PAUSED' && (
              <button
                type="button"
                className="btn-primary"
                onClick={() => act('resume', 'Automation resumed.')}
              >
                Resume
              </button>
            )}
            {isActive && (
              <>
                <button type="button" className="btn-secondary" onClick={() => setConfirmRun(true)}>
                  Run now
                </button>
                <button
                  type="button"
                  className="btn-ghost"
                  onClick={() => act('pause', 'Automation paused.')}
                >
                  Pause
                </button>
              </>
            )}
          </div>
        }
      />

      {needsApproval && (
        <div className="mb-4 rounded-lg bg-amber-50 px-4 py-3 text-sm text-amber-900 ring-1 ring-amber-200">
          This automation has not been approved yet, so it cannot send. Run a dry run first to see
          exactly who would receive what.
        </div>
      )}

      <div className="mb-6 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatTile label="Status" value={automation.status} />
        <StatTile label="Sent" value={formatNumber(stats?.total_sent ?? 0)} />
        <StatTile label="Skipped" value={formatNumber(stats?.total_skipped ?? 0)} />
        <StatTile
          label="Next run"
          value={automation.next_run_at ? formatDateTime(automation.next_run_at) : '—'}
        />
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        <Card title="Configuration" className="lg:col-span-1">
          <dl className="space-y-3">
            <Field
              label="Type"
              value={AUTOMATION_KIND_LABEL[automation.kind] ?? automation.kind}
            />
            <Field
              label="State"
              value={
                <Badge className={AUTOMATION_STATUS_BADGE[automation.status]}>
                  {automation.status}
                </Badge>
              }
            />
            <Field
              label="Audience"
              value={
                automation.segment_id ? (
                  <Link
                    to="/segments"
                    className="text-brand-700 hover:underline"
                  >{`Segment #${automation.segment_id}`}</Link>
                ) : (
                  `${automation.manual_customer_ids.length} customers (manual list)`
                )
              }
              hint="Re-evaluated at send time, not at creation."
            />
            <Field label="Repeats" value={humanize(automation.recurrence)} />
            <Field
              label="Send window"
              value="09:00–19:00 NZ time"
              hint="Anything falling outside is deferred to the next open slot, never dropped."
            />
            {automation.kind === 'SEQUENCE' && (
              <>
                <Field label="Enrollment" value={humanize(automation.enrollment_mode)} />
                <Field
                  label="Stops on order"
                  value={automation.stop_on_order ? 'Yes' : 'No'}
                  hint={
                    automation.stop_on_order
                      ? 'A customer who orders has met the goal and receives no further steps.'
                      : undefined
                  }
                />
              </>
            )}
            <Field
              label="Approved"
              value={automation.approved_at ? formatDateTime(automation.approved_at) : 'Not yet'}
            />
          </dl>
        </Card>

        <div className="space-y-6 lg:col-span-2">
          {automation.kind === 'SEQUENCE' && automation.steps.length > 0 && (
            <Card title="Steps">
              <ol className="space-y-3">
                {automation.steps.map((step) => (
                  <li key={step.id} className="rounded-lg bg-slate-50 p-3">
                    <div className="flex items-center gap-2">
                      <Badge className="bg-brand-50 text-brand-700 ring-brand-200">
                        {`Day ${step.offset_days}`}
                      </Badge>
                      {/* The badge already says the offset, so only show a name
                          that adds something to it. */}
                      {step.name && step.name !== `Day ${step.offset_days}` && (
                        <span className="text-sm font-medium text-slate-900">{step.name}</span>
                      )}
                    </div>
                    <p className="mt-2 whitespace-pre-wrap text-sm text-slate-600">
                      {step.message_template || <em className="text-slate-400">Uses the segment default copy.</em>}
                    </p>
                  </li>
                ))}
              </ol>
              <p className="mt-3 text-xs text-slate-500">
                Offsets are counted from each customer’s own enrollment, so the same sequence can run
                all year and everyone gets the same experience.
              </p>
            </Card>
          )}

          {report && (
            <Card
              title={report.dry_run ? 'Dry run — nothing was sent' : 'Run result'}
              actions={
                <button type="button" className="btn-ghost" onClick={() => setReport(null)}>
                  Dismiss
                </button>
              }
            >
              <div className="mb-4 grid gap-3 sm:grid-cols-4">
                <StatTile
                  label={report.dry_run ? 'Would receive' : 'Sent'}
                  value={formatNumber(report.dry_run ? report.previewed : report.sent)}
                />
                <StatTile label="Skipped" value={formatNumber(report.skipped)} />
                <StatTile label="Failed" value={formatNumber(report.failed)} />
                <StatTile label="Provider" value={report.is_mock ? 'Mock' : report.provider} />
              </div>

              {Object.keys(report.skips_by_reason).length > 0 && (
                <div className="mb-4">
                  <SectionTitle>Why customers were excluded</SectionTitle>
                  <ul className="mt-2 space-y-1 text-sm text-slate-600">
                    {Object.entries(report.skips_by_reason).map(([reason, count]) => (
                      <li key={reason} className="flex justify-between gap-4">
                        <span>{SKIP_REASON_LABEL[reason] ?? humanize(reason)}</span>
                        <span className="tabular-nums text-slate-900">{count}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              <SectionTitle>Recipients</SectionTitle>
              <div className="mt-2 space-y-2">
                {report.recipients.map((recipient) => (
                  <div
                    key={`${recipient.customer_id}-${recipient.local_date}-${recipient.status}`}
                    className="rounded-lg border border-slate-200 p-3"
                  >
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div className="flex items-center gap-2">
                        <span className="text-sm font-medium text-slate-900">
                          {recipient.customer_name}
                        </span>
                        <span className="text-xs text-slate-500">{recipient.to}</span>
                      </div>
                      <div className="flex items-center gap-2">
                        <span className="text-xs text-slate-500">
                          {formatDateTime(recipient.scheduled_for_local)} NZ
                        </span>
                        <Badge className={SEND_STATUS_BADGE[recipient.status]}>
                          {recipient.status}
                        </Badge>
                      </div>
                    </div>
                    {recipient.skip_reason ? (
                      <p className="mt-2 text-sm text-amber-800">
                        <strong>{SKIP_REASON_LABEL[recipient.skip_reason] ?? recipient.skip_reason}:</strong>{' '}
                        {recipient.skip_detail}
                      </p>
                    ) : (
                      <p className="mt-2 whitespace-pre-wrap text-sm text-slate-600">
                        {recipient.body}
                      </p>
                    )}
                  </div>
                ))}
                {report.truncated && (
                  <p className="text-xs text-slate-500">
                    Showing the first {report.recipients.length} of {report.candidates}.
                  </p>
                )}
                {report.recipients.length === 0 && (
                  <EmptyState
                    title="Nobody matches right now"
                    description="The audience is resolved live, so this will change as customers move between segments."
                  />
                )}
              </div>
            </Card>
          )}

          {automation.kind !== 'COHORT_BULK' && (
            <Card title="Enrollments">
              {!enrollments || enrollments.length === 0 ? (
                <EmptyState
                  title="Nobody enrolled yet"
                  description="Customers are enrolled on the first run, or whenever they start matching the audience."
                />
              ) : (
                <TableShell>
                  <thead className="bg-slate-50">
                    <tr>
                      <th className="table-head">Customer</th>
                      <th className="table-head">Status</th>
                      <th className="table-head">Enrolled</th>
                      <th className="table-head">
                        {automation.kind === 'NUDGE' ? 'Next nudge' : 'Step'}
                      </th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {enrollments.map((enrollment) => (
                      <tr key={enrollment.id}>
                        <td className="table-cell">
                          <Link
                            to={`/customers/${enrollment.customer_id}`}
                            className="text-brand-700 hover:underline"
                          >
                            #{enrollment.customer_id}
                          </Link>
                        </td>
                        <td className="table-cell">
                          <Badge
                            className={
                              enrollment.status === 'ACTIVE'
                                ? 'bg-emerald-50 text-emerald-700 ring-emerald-200'
                                : 'bg-slate-100 text-slate-600 ring-slate-200'
                            }
                          >
                            {enrollment.status}
                          </Badge>
                          {enrollment.stop_reason && (
                            <p className="mt-0.5 text-xs text-slate-500">{enrollment.stop_reason}</p>
                          )}
                        </td>
                        <td className="table-cell text-sm text-slate-600">
                          {formatDateTime(enrollment.enrolled_at)}
                        </td>
                        <td className="table-cell text-sm text-slate-600">
                          {automation.kind === 'NUDGE'
                            ? enrollment.next_due_at
                              ? formatDateTime(enrollment.next_due_at)
                              : '—'
                            : `Step ${enrollment.current_step + 1}`}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </TableShell>
              )}
            </Card>
          )}

          <Card title="Send ledger" description="Every attempt, sent or withheld, with its reason.">
            {!sends || sends.length === 0 ? (
              <EmptyState
                title="No sends yet"
                description="Run a dry run to see what would happen without sending anything."
              />
            ) : (
              <TableShell>
                <thead className="bg-slate-50">
                  <tr>
                    <th className="table-head">Customer</th>
                    <th className="table-head">Status</th>
                    <th className="table-head">Local date</th>
                    <th className="table-head">Detail</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {sends.map((send) => (
                    <tr key={send.id}>
                      <td className="table-cell">
                        <Link
                          to={`/customers/${send.customer_id}`}
                          className="text-brand-700 hover:underline"
                        >
                          #{send.customer_id}
                        </Link>
                      </td>
                      <td className="table-cell">
                        <Badge className={SEND_STATUS_BADGE[send.status]}>{send.status}</Badge>
                      </td>
                      <td className="table-cell text-sm text-slate-600">{send.local_date}</td>
                      <td className="table-cell max-w-md whitespace-normal text-xs text-slate-500">
                        {send.skip_reason
                          ? `${SKIP_REASON_LABEL[send.skip_reason] ?? send.skip_reason} — ${send.skip_detail ?? ''}`
                          : send.error_message || send.body.slice(0, 120)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </TableShell>
            )}
          </Card>
        </div>
      </div>

      <ConfirmDialog
        open={confirmRun}
        title="Run this automation now?"
        message="This sends real messages to everyone who passes the consent, quiet-hours and dedup checks. Run a dry run first if you want to see the list."
        confirmLabel="Send now"
        onCancel={() => setConfirmRun(false)}
        onConfirm={async () => {
          setConfirmRun(false);
          const result = await liveRun.run();
          if (result) {
            setReport(result);
            notify(`Sent ${result.sent}, skipped ${result.skipped}.`);
            refetch();
            refetchStats();
            refetchSends();
          }
        }}
      />
    </>
  );
}
