import { useState } from 'react';
import { Link } from 'react-router-dom';

import {
  Badge,
  Card,
  EmptyState,
  ErrorState,
  LoadingState,
  PageHeader,
  TableShell,
} from '../components/ui';
import { useQuery } from '../hooks/useApi';

/**
 * Smart Reorder: who is about to enter their usual ordering window.
 *
 * Every time comes from the server already converted to New Zealand local
 * time. The browser does no timezone arithmetic — a viewer opening this from
 * another country must see the customer's evening, not their own.
 */
interface UpcomingCustomer {
  customer_id: number;
  name: string;
  automation_id: number;
  automation_name: string;
  automation_status: string;
  predicted_at_local: string;
  minutes_away: number;
  is_due_now: boolean;
  usual_day: string | null;
  usual_time: string | null;
  confidence: number;
  interval_label: string;
  last_order_at: string | null;
  channel: string;
  last_sent_at: string | null;
}

interface UpcomingResponse {
  generated_at: string;
  horizon_hours: number;
  due_now: number;
  upcoming: number;
  total: number;
  truncated: boolean;
  customers: UpcomingCustomer[];
}

interface AccuracyBlock {
  total_resolved: number;
  hits: number;
  early: number;
  late: number;
  no_order: number;
  accuracy_pct: number;
  ordered_at_all_pct: number;
  median_error_minutes: number | null;
  within_30_minutes: number;
  within_1_hour: number;
  within_3_hours: number;
  within_24_hours: number;
}

interface OverviewResponse {
  eligible_customers: number;
  predicted_today: number;
  predicted_next_24h: number;
  min_confidence: number;
  enrolled_customers: number;
  enrolled_due_next_24h: number;
  active_campaigns: number;
  predictions_pending: number;
  accuracy: AccuracyBlock;
}

const HORIZONS = [
  { hours: 2, label: 'Next 2 hours' },
  { hours: 24, label: 'Next 24 hours' },
  { hours: 72, label: 'Next 3 days' },
];

function Stat({ label, value, hint }: { label: string; value: string | number; hint?: string }) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4">
      <p className="text-2xl font-semibold text-slate-900">{value}</p>
      <p className="mt-0.5 text-sm text-slate-600">{label}</p>
      {hint && <p className="mt-1 text-xs text-slate-400">{hint}</p>}
    </div>
  );
}

/** "in 25 minutes", "12 minutes ago" — the number an operator is judging by. */
function relative(minutes: number): string {
  const abs = Math.abs(minutes);
  const text =
    abs < 60
      ? `${abs} min`
      : abs < 1440
        ? `${Math.round(abs / 60)} hr`
        : `${Math.round(abs / 1440)} days`;
  return minutes >= 0 ? `in ${text}` : `${text} ago`;
}

function formatLocal(value: string | null): string {
  if (!value) return '—';
  const match = value.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
  if (!match) return value;
  const [, y, mo, d, h, mi] = match;
  return new Date(Number(y), Number(mo) - 1, Number(d), Number(h), Number(mi)).toLocaleString(
    'en-NZ',
    { weekday: 'short', day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit', hour12: true },
  );
}

function AccuracyCard({ accuracy, pending }: { accuracy: AccuracyBlock; pending: number }) {
  // Nothing resolved is not a score of zero. Rendering 0% would read as "the
  // engine was tested and failed" when it has simply not been checked yet.
  if (accuracy.total_resolved === 0) {
    return (
      <Card title="Prediction accuracy" description="How close the predictions turned out to be.">
        <EmptyState
          title="Not enough data yet"
          description={
            pending > 0
              ? `${pending} prediction${pending === 1 ? '' : 's'} are waiting to be checked against real orders. Accuracy appears once they resolve.`
              : 'Accuracy appears once Smart Reorder has made predictions and customers have had a chance to order.'
          }
        />
      </Card>
    );
  }

  return (
    <Card title="Prediction accuracy" description={`Across ${accuracy.total_resolved} resolved predictions.`}>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat label="Ordered near prediction" value={`${accuracy.accuracy_pct}%`} />
        <Stat label="Ordered at all" value={`${accuracy.ordered_at_all_pct}%`} />
        <Stat
          label="Median error"
          value={accuracy.median_error_minutes === null ? '—' : `${accuracy.median_error_minutes} min`}
        />
        <Stat label="Never ordered" value={accuracy.no_order} />
      </div>

      <div className="mt-4 border-t border-slate-100 pt-3">
        <p className="mb-2 text-xs font-medium text-slate-500">Ordered within</p>
        <div className="grid grid-cols-4 gap-2 text-center">
          {[
            ['30 min', accuracy.within_30_minutes],
            ['1 hour', accuracy.within_1_hour],
            ['3 hours', accuracy.within_3_hours],
            ['24 hours', accuracy.within_24_hours],
          ].map(([label, value]) => (
            <div key={label as string}>
              <p className="text-sm font-semibold text-slate-900">{value}</p>
              <p className="text-xs text-slate-500">{label}</p>
            </div>
          ))}
        </div>
      </div>

      {/* Early and late are kept apart because they call for opposite fixes. */}
      <p className="mt-3 text-xs text-slate-400">
        {accuracy.early} ordered earlier than predicted · {accuracy.late} ordered later.
        {accuracy.early > accuracy.late * 2 &&
          ' Consistently early suggests the reminder is arriving after the decision is made.'}
        {accuracy.late > accuracy.early * 2 &&
          ' Consistently late suggests the predicted interval is too short.'}
      </p>
    </Card>
  );
}

export default function SmartReorderPage() {
  const [hours, setHours] = useState(24);

  const overview = useQuery<OverviewResponse>('/api/v1/smart-reorder/overview');
  const upcoming = useQuery<UpcomingResponse>(
    `/api/v1/smart-reorder/upcoming?hours=${hours}`,
    [hours],
  );

  if (overview.loading && !overview.data) return <LoadingState label="Loading Smart Reorder…" />;
  if (overview.error) return <ErrorState message={overview.error} onRetry={overview.refetch} />;

  const stats = overview.data;
  const rows = upcoming.data?.customers ?? [];

  return (
    <>
      <PageHeader
        title="Smart Reorder"
        description="Customers are reminded around the time they usually order, learned from their own history. Every send still re-checks consent, the send window, and whether they have already ordered."
      />

      {/* The first row is the opportunity, counted from every customer's
          stored prediction — true whether or not a campaign is running. The
          second is what a live campaign is actually watching. Showing only
          the second made the whole page read zero until somebody activated
          something, which is the opposite of what it is for. */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat
          label="Eligible customers"
          value={stats?.eligible_customers ?? 0}
          hint={`Confident routine (${stats?.min_confidence ?? 70}%+) and contactable`}
        />
        <Stat label="Predicted today" value={stats?.predicted_today ?? 0} hint="In their own local day" />
        <Stat label="Predicted next 24h" value={stats?.predicted_next_24h ?? 0} />
        <Stat
          label="Enrolled in a campaign"
          value={stats?.enrolled_customers ?? 0}
          hint={`${stats?.active_campaigns ?? 0} active`}
        />
      </div>

      <Card
        className="mt-4"
        title="Customers likely to order now"
        description="Entering their predicted window. Times are New Zealand local."
        actions={
          <div className="inline-flex overflow-hidden rounded-lg border border-slate-300">
            {HORIZONS.map((h) => (
              <button
                key={h.hours}
                type="button"
                onClick={() => setHours(h.hours)}
                aria-pressed={hours === h.hours}
                className={`px-3 py-1.5 text-xs font-medium transition-colors ${
                  hours === h.hours
                    ? 'bg-brand-600 text-white'
                    : 'bg-white text-slate-600 hover:bg-slate-50'
                }`}
              >
                {h.label}
              </button>
            ))}
          </div>
        }
      >
        {upcoming.loading && !upcoming.data && <LoadingState label="Reading predictions…" />}
        {upcoming.error && <ErrorState message={upcoming.error} onRetry={upcoming.refetch} />}

        {/* Two different situations, and telling them apart matters: "no
            campaign is running" is something to go and fix, while "nobody is
            due in the next two hours" is the system working. One message for
            both told an operator with a live campaign and 22 enrolled
            customers that they had neither. */}
        {upcoming.data && rows.length === 0 && (
          <EmptyState
            title={
              (stats?.enrolled_customers ?? 0) > 0
                ? 'Nobody is due in this window'
                : 'No campaign is watching yet'
            }
            description={
              (stats?.enrolled_customers ?? 0) > 0
                ? `${stats?.enrolled_customers} customers are enrolled, none of them due in the next ${hours} hours. Try a longer window.`
                : 'Customers appear here once a Smart Reorder campaign is active and they have enough order history for a routine to be learned.'
            }
          />
        )}

        {rows.length > 0 && (
          <>
            <p className="mb-3 text-sm text-slate-600">
              <span className="font-semibold text-slate-900">{upcoming.data?.due_now ?? 0}</span> due
              now · {upcoming.data?.upcoming ?? 0} coming up
            </p>
            <TableShell>
              <thead className="bg-slate-50">
                <tr>
                  <th className="table-head">Customer</th>
                  {/* This column is the reminder, not the order. It was
                      headed "Predicted", beside a "Usual" column holding the
                      predicted order time — so the two times a reader most
                      needs to tell apart were labelled as if the reminder
                      were the prediction. */}
                  <th className="table-head">Reminder</th>
                  <th className="table-head">When</th>
                  <th className="table-head">Confidence</th>
                  <th className="table-head">Usual</th>
                  <th className="table-head">Channel</th>
                  <th className="table-head">Campaign</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
              {rows.map((row) => (
                <tr key={`${row.automation_id}-${row.customer_id}`} className="table-row">
                  <td className="table-cell">
                    <Link
                      to={`/customers/${row.customer_id}`}
                      className="font-medium text-brand-700 hover:underline"
                    >
                      {row.name || `Customer ${row.customer_id}`}
                    </Link>
                  </td>
                  <td className="table-cell">{formatLocal(row.predicted_at_local)}</td>
                  <td className="table-cell">
                    {row.is_due_now ? (
                      <Badge className="bg-emerald-50 text-emerald-700 ring-emerald-200">
                        Due now
                      </Badge>
                    ) : (
                      <span className="text-slate-600">{relative(row.minutes_away)}</span>
                    )}
                  </td>
                  <td className="table-cell">
                    <Badge
                      className={
                        row.confidence >= 70
                          ? 'bg-emerald-50 text-emerald-700 ring-emerald-200'
                          : row.confidence >= 50
                            ? 'bg-amber-50 text-amber-800 ring-amber-200'
                            : 'bg-slate-100 text-slate-600 ring-slate-200'
                      }
                    >
                      {row.confidence}%
                    </Badge>
                  </td>
                  <td className="table-cell whitespace-normal text-slate-600">
                    {row.usual_day ?? '—'}
                    {row.usual_time ? ` ${row.usual_time}` : ''} · every {row.interval_label}
                  </td>
                  <td className="table-cell">{row.channel}</td>
                  <td className="table-cell">
                    <Link
                      to={`/automations/${row.automation_id}`}
                      className="text-brand-700 hover:underline"
                    >
                      {row.automation_name}
                    </Link>
                  </td>
                </tr>
              ))}
              </tbody>
            </TableShell>
            {upcoming.data?.truncated && (
              <p className="mt-2 text-xs text-slate-400">
                Showing the first {rows.length}. The counts above cover everybody.
              </p>
            )}
          </>
        )}
      </Card>

      <div className="mt-4">
        <AccuracyCard
          accuracy={stats?.accuracy ?? ({ total_resolved: 0 } as AccuracyBlock)}
          pending={stats?.predictions_pending ?? 0}
        />
      </div>
    </>
  );
}
