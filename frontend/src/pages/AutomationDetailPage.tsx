import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';

import { api } from '../api/client';
import AutomationEditor from '../features/AutomationEditor';
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
import type {
  Automation,
  AutomationEnrollment,
  AutomationRunReport,
  AutomationSend,
  AutomationStats,
} from '../types';
import { formatBusinessTime, formatDateTime, formatNumber, humanize } from '../utils/format';
import {
  AUTOMATION_KIND_LABEL,
  AUTOMATION_STATUS_BADGE,
  SEND_STATUS_BADGE,
  SEQUENCE_TRIGGER_LABEL,
  SKIP_REASON_LABEL,
} from '../utils/theme';

/** Why this customer is being messaged now, shown so an operator can judge it.
 *
 * A nudge's timing is only as good as the pattern behind it, and a weak
 * pattern is worth seeing before approving rather than after. */
function RecipientRationale({ context }: { context: Record<string, unknown> }) {
  const usualDay = context.usual_day as string | undefined;
  const confidence = context.pattern_confidence as number | undefined;
  const offer = context.offer as { include_discount?: boolean; reason?: string } | undefined;
  const stepName = context.step_name as string | undefined;
  const offsetDays = context.offset_days as number | undefined;

  const bits: string[] = [];
  if (usualDay) {
    const hour = context.usual_hour as number | undefined;
    bits.push(`Usually orders ${usualDay}${hour != null ? ` around ${hour}:00` : ''}`);
  }
  if (stepName != null && offsetDays != null) bits.push(`${stepName} — day ${offsetDays}`);
  if (bits.length === 0 && !offer) return null;

  // Below this the modal weekday explains under half their orders, which is a
  // weak basis for claiming to know when they usually buy.
  const weak = typeof confidence === 'number' && confidence < 0.5;

  return (
    <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-slate-500">
      {bits.map((bit) => (
        <span key={bit}>{bit}</span>
      ))}
      {typeof confidence === 'number' && (
        <Badge
          className={
            weak
              ? 'bg-amber-50 text-amber-800 ring-amber-200'
              : 'bg-slate-100 text-slate-600 ring-slate-200'
          }
        >
          {weak ? 'weak pattern' : 'pattern'} {Math.round(confidence * 100)}%
        </Badge>
      )}
      {offer && (
        <span title={offer.reason}>
          {offer.include_discount ? 'Offer included' : 'No offer'}
        </span>
      )}
    </div>
  );
}

export default function AutomationDetailPage() {
  const { id } = useParams();
  const base = `/api/v1/automations/${id}`;

  const { data: automation, loading, error, refetch } = useQuery<Automation>(base);
  const { data: stats, refetch: refetchStats } = useQuery<AutomationStats>(`${base}/stats`);
  const { data: sends, refetch: refetchSends } = useQuery<AutomationSend[]>(`${base}/sends?limit=50`);
  const { data: enrollments, refetch: refetchEnrollments } = useQuery<AutomationEnrollment[]>(
    `${base}/enrollments?limit=50`,
  );

  const [report, setReport] = useState<AutomationRunReport | null>(null);
  const [confirmRun, setConfirmRun] = useState(false);
  const [editing, setEditing] = useState(false);
  const [enrolling, setEnrolling] = useState(false);
  const [enrolIds, setEnrolIds] = useState('');

  const dryRun = useMutation(async () => api.post<AutomationRunReport>(`${base}/preview`));
  const liveRun = useMutation(async () => api.post<AutomationRunReport>(`${base}/run`));
  const lifecycle = useMutation(async (action: string) =>
    api.post<Automation>(`${base}/${action}`),
  );
  const enrollmentHold = useMutation(async (id: number, action: 'pause' | 'resume') =>
    api.post<AutomationEnrollment>(`${base}/enrollments/${id}/${action}`),
  );
  const addEnrollments = useMutation(async (ids: number[]) =>
    api.post<{ enrolled: number; already_enrolled: number; unknown_customers: number }>(
      `${base}/enrollments`,
      ids,
    ),
  );

  if (loading) return <LoadingState label="Loading automation…" />;
  if (error || !automation) return <ErrorState message={error ?? 'Not found.'} onRetry={refetch} />;

  const needsApproval = automation.require_approval && !automation.approved_at;
  const isActive = automation.status === 'ACTIVE';

  /** Hold or release one customer without touching the rest of the campaign. */
  async function holdEnrollment(enrollmentId: number, action: 'pause' | 'resume') {
    const result = await enrollmentHold.run(enrollmentId, action);
    if (result) {
      notify(action === 'pause' ? 'Customer paused.' : 'Customer resumed.');
      refetchEnrollments();
    }
  }

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
            <button type="button" className="btn-ghost" onClick={() => setEditing(true)}>
              Edit
            </button>
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
          value={automation.next_run_at ? formatBusinessTime(automation.next_run_at) : '—'}
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
                  <Link to="/segments" className="text-brand-700 hover:underline">
                    {automation.segment_name ?? `Segment #${automation.segment_id}`}
                  </Link>
                ) : (
                  `${automation.manual_customer_ids.length} customers (manual list)`
                )
              }
              hint="Re-evaluated at send time, not at creation."
            />
            {automation.kind === 'NUDGE' ? (
              <Field
                label="Runs"
                value="Continuously"
                hint="Each customer is messaged on their own schedule, until they opt out."
              />
            ) : automation.kind === 'SEQUENCE' ? (
              <Field
                label="Runs"
                value="Per customer, from enrollment"
                hint="Step timing is counted from when each customer joined."
              />
            ) : (
              <Field label="Repeats" value={humanize(automation.recurrence)} />
            )}
            <Field
              label="Send window"
              value="09:00–19:00 NZ time"
              hint="Anything falling outside is deferred to the next open slot, never dropped."
            />
            {automation.kind === 'SEQUENCE' && (
              <>
                <Field
                  label="Clock starts from"
                  value={
                    SEQUENCE_TRIGGER_LABEL[automation.trigger_type] ?? automation.trigger_type
                  }
                  hint="Step offsets are counted from this moment, not from today."
                />
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
                      {step.use_llm && (
                        <Badge className="bg-violet-50 text-violet-700 ring-violet-200">
                          Written per customer
                        </Badge>
                      )}
                    </div>
                    <p className="mt-2 whitespace-pre-wrap text-sm text-slate-600">
                      {step.message_template || <em className="text-slate-400">Uses the segment default copy.</em>}
                    </p>
                    {step.use_llm && (
                      <p className="mt-1 text-xs text-slate-500">
                        Drafted for each recipient and compliance-checked before sending. The copy
                        above is the fallback if a draft fails.
                      </p>
                    )}
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
                          {formatBusinessTime(recipient.scheduled_for)} NZ
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
                      <>
                        <p className="mt-2 whitespace-pre-wrap text-sm text-slate-600">
                          {recipient.body}
                        </p>
                        <RecipientRationale context={recipient.context} />
                      </>
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
            <Card
              title="Enrollments"
              actions={
                automation.kind === 'SEQUENCE' ? (
                  <button
                    type="button"
                    className="btn-secondary"
                    onClick={() => setEnrolling(true)}
                  >
                    Add customers
                  </button>
                ) : undefined
              }
            >
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
                      <th className="table-head">Enrolled (NZ)</th>
                      <th className="table-head">
                        {automation.kind === 'NUDGE' ? 'Next nudge (NZ)' : 'Step'}
                      </th>
                      <th className="table-head" />
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
                          {formatBusinessTime(enrollment.enrolled_at)}
                        </td>
                        <td className="table-cell text-sm text-slate-600">
                          {automation.kind === 'NUDGE'
                            ? enrollment.next_due_at
                              ? formatBusinessTime(enrollment.next_due_at)
                              : '—'
                            : `Step ${enrollment.current_step + 1}`}
                        </td>
                        <td className="table-cell text-right">
                          {enrollment.status === 'ACTIVE' && (
                            <button
                              type="button"
                              className="btn-ghost text-xs"
                              onClick={() => holdEnrollment(enrollment.id, 'pause')}
                            >
                              Pause
                            </button>
                          )}
                          {enrollment.status === 'PAUSED' && (
                            <button
                              type="button"
                              className="btn-ghost text-xs"
                              onClick={() => holdEnrollment(enrollment.id, 'resume')}
                            >
                              Resume
                            </button>
                          )}
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

      {editing && (
        <Modal
          open
          size="lg"
          title={`Edit ${automation.name}`}
          description="Change the name, description or message. The editor says up front if a change needs re-approving."
          onClose={() => setEditing(false)}
        >
          <AutomationEditor
            automation={automation}
            onCancel={() => setEditing(false)}
            onSaved={() => {
              setEditing(false);
              setReport(null);
              refetch();
              refetchStats();
            }}
          />
        </Modal>
      )}

      {enrolling && (
        <Modal
          open
          title="Add customers to this sequence"
          description="Each one starts at the step matching their trigger, not necessarily step one."
          onClose={() => setEnrolling(false)}
          footer={
            <>
              <button type="button" className="btn-secondary" onClick={() => setEnrolling(false)}>
                Cancel
              </button>
              <button
                type="button"
                className="btn-primary"
                disabled={addEnrollments.loading || !enrolIds.trim()}
                onClick={async () => {
                  const ids = enrolIds
                    .split(/[\s,]+/)
                    .map((value) => Number(value.trim()))
                    .filter((value) => Number.isInteger(value) && value > 0);
                  const result = await addEnrollments.run(ids);
                  if (result) {
                    notify(
                      `Enrolled ${result.enrolled}. ` +
                        `${result.already_enrolled} already in, ` +
                        `${result.unknown_customers} not found.`,
                    );
                    setEnrolling(false);
                    setEnrolIds('');
                    refetchEnrollments();
                  }
                }}
              >
                {addEnrollments.loading && <Spinner className="h-4 w-4 text-white" />}
                Enrol
              </button>
            </>
          }
        >
          <label className="label" htmlFor="enrol-ids">
            Customer IDs
          </label>
          <textarea
            id="enrol-ids"
            className="input"
            rows={3}
            value={enrolIds}
            onChange={(event) => setEnrolIds(event.target.value)}
            placeholder="14, 92, 318"
          />
          <p className="mt-1 text-xs text-slate-500">
            Separated by commas or spaces. Somebody already enrolled keeps the progress they
            have rather than being reset to the first step.
          </p>
          {addEnrollments.error && (
            <p className="mt-2 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700" role="alert">
              {addEnrollments.error}
            </p>
          )}
        </Modal>
      )}

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
