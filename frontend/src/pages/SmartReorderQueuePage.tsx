import { useState } from 'react';
import { Link } from 'react-router-dom';

import { api } from '../api/client';
import {
  Badge,
  Card,
  ConfirmDialog,
  EmptyState,
  ErrorState,
  LoadingState,
  Modal,
  PageHeader,
  Spinner,
  TableShell,
  notify,
} from '../components/ui';
import { useMutation, useQuery } from '../hooks/useApi';

/**
 * The individual messages Smart Reorder is preparing to send.
 *
 * This is the screen that makes the feature inspectable. A Smart Reorder
 * campaign is one rule that produces thousands of different messages at
 * thousands of different minutes, and without this page the only way to find
 * out what it says to whom is to let it send and read the ledger afterwards.
 *
 * Every time is New Zealand local, converted on the server — somebody opening
 * this from another country must see the customer's evening, not their own.
 */
interface QueuedMessage {
  id: number;
  customer_id: number;
  customer_name: string;
  to: string | null;
  automation_id: number;
  channel: string;
  status: string;
  scheduled_at_local: string;
  predicted_order_at_local: string | null;
  offset_minutes: number;
  moved_for_send_window: boolean;
  confidence: number;
  message: string;
  edited: boolean;
  minutes_away: number;
  product: string | null;
  usual_day: string | null;
  usual_time: string | null;
  is_test: boolean;
}

interface QueueResponse {
  generated_at: string;
  count: number;
  messages: QueuedMessage[];
}

const STATUS_BADGE: Record<string, string> = {
  SCHEDULED: 'bg-blue-50 text-blue-700 ring-blue-200',
  DRAFT: 'bg-slate-100 text-slate-600 ring-slate-200',
  PROCESSING: 'bg-amber-50 text-amber-800 ring-amber-200',
  SENT: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
  DELIVERED: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
  CONVERTED: 'bg-emerald-100 text-emerald-800 ring-emerald-300',
  CANCELLED: 'bg-slate-100 text-slate-600 ring-slate-200',
  SUPPRESSED: 'bg-amber-50 text-amber-800 ring-amber-200',
  FAILED: 'bg-red-50 text-red-700 ring-red-200',
  EXPIRED: 'bg-slate-100 text-slate-500 ring-slate-200',
};

const VIEWS = [
  { value: '', label: 'Pending' },
  { value: 'SENT', label: 'Sent' },
  { value: 'CANCELLED', label: 'Cancelled' },
  { value: 'CONVERTED', label: 'Converted' },
  { value: 'SUPPRESSED', label: 'Suppressed' },
];

function localTime(value: string | null): string {
  if (!value) return '—';
  const match = value.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
  if (!match) return value;
  const [, y, mo, d, h, mi] = match;
  return new Date(Number(y), Number(mo) - 1, Number(d), Number(h), Number(mi)).toLocaleString(
    'en-NZ',
    { weekday: 'short', day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit', hour12: true },
  );
}

function relative(minutes: number): string {
  const abs = Math.abs(minutes);
  const text = abs < 60 ? `${abs} min` : abs < 1440 ? `${Math.round(abs / 60)} hr` : `${Math.round(abs / 1440)} days`;
  return minutes >= 0 ? `in ${text}` : `${text} ago`;
}

export default function SmartReorderQueuePage() {
  const [status, setStatus] = useState('');
  const [open, setOpen] = useState<QueuedMessage | null>(null);
  const [cancelling, setCancelling] = useState<QueuedMessage | null>(null);

  const { data, loading, error, refetch } = useQuery<QueueResponse>(
    `/api/v1/smart-reorder/queue?limit=200${status ? `&status=${status}` : ''}`,
    [status],
  );

  const cancel = useMutation(async (id: number) =>
    api.post(`/api/v1/smart-reorder/queue/${id}/cancel`, {}),
  );

  const rows = data?.messages ?? [];

  return (
    <>
      <PageHeader
        title="Upcoming Smart Reorder messages"
        description="Each row is one customer's own reminder, written and scheduled for their own predicted ordering time. Every send re-checks whether they have already ordered."
      />

      <Card
        title={status ? VIEWS.find((v) => v.value === status)?.label : 'Waiting to send'}
        description={`${rows.length} individual message${rows.length === 1 ? '' : 's'}. Times are New Zealand local.`}
        actions={
          <div className="inline-flex overflow-hidden rounded-lg border border-slate-300">
            {VIEWS.map((view) => (
              <button
                key={view.value || 'pending'}
                type="button"
                onClick={() => setStatus(view.value)}
                aria-pressed={status === view.value}
                className={`px-3 py-1.5 text-xs font-medium transition-colors ${
                  status === view.value
                    ? 'bg-brand-600 text-white'
                    : 'bg-white text-slate-600 hover:bg-slate-50'
                }`}
              >
                {view.label}
              </button>
            ))}
          </div>
        }
      >
        {loading && !data && <LoadingState label="Reading the queue…" />}
        {error && <ErrorState message={error} onRetry={refetch} />}

        {data && rows.length === 0 && (
          <EmptyState
            title={status ? 'Nothing here yet' : 'No messages are queued'}
            description={
              status
                ? 'No individual messages have reached this state.'
                : 'Messages appear once a Smart Reorder campaign is active and its customers have enough order history for a routine to be learned. Run a dry run on the campaign to see what it would schedule.'
            }
          />
        )}

        {rows.length > 0 && (
          <TableShell>
            <thead className="bg-slate-50">
              <tr>
                <th className="table-head">Customer</th>
                <th className="table-head">Predicted order</th>
                <th className="table-head">Reminder</th>
                <th className="table-head">When</th>
                <th className="table-head">Channel</th>
                <th className="table-head">Message</th>
                <th className="table-head">Confidence</th>
                <th className="table-head">Status</th>
                <th className="table-head">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {rows.map((row) => (
                <tr key={row.id} className="table-row">
                  <td className="table-cell">
                    <Link
                      to={`/customers/${row.customer_id}`}
                      className="font-medium text-brand-700 hover:underline"
                    >
                      {row.customer_name}
                    </Link>
                    <span className="block text-xs text-slate-400">{row.to ?? '—'}</span>
                  </td>
                  <td className="table-cell">{localTime(row.predicted_order_at_local)}</td>
                  <td className="table-cell">
                    {localTime(row.scheduled_at_local)}
                    {/* A reminder pulled off its aimed time is a different
                        promise, so the row says so rather than quietly
                        showing a number that is not "30 minutes before". */}
                    {row.moved_for_send_window && (
                      <span className="block text-xs text-amber-700">moved to send window</span>
                    )}
                  </td>
                  <td className="table-cell text-slate-600">{relative(row.minutes_away)}</td>
                  <td className="table-cell">{row.channel}</td>
                  <td className="table-cell max-w-xs whitespace-normal text-slate-600">
                    {row.message.length > 90 ? `${row.message.slice(0, 90)}…` : row.message}
                    {row.edited && (
                      <span className="block text-xs text-brand-700">edited</span>
                    )}
                  </td>
                  <td className="table-cell">
                    <Badge
                      className={
                        row.confidence >= 70
                          ? 'bg-emerald-50 text-emerald-700 ring-emerald-200'
                          : 'bg-amber-50 text-amber-800 ring-amber-200'
                      }
                    >
                      {row.confidence}%
                    </Badge>
                  </td>
                  <td className="table-cell">
                    <Badge className={STATUS_BADGE[row.status] ?? STATUS_BADGE.DRAFT}>
                      {row.status}
                    </Badge>
                  </td>
                  <td className="table-cell">
                    <div className="flex gap-1.5">
                      <button
                        type="button"
                        className="btn-secondary px-2 py-1 text-xs"
                        onClick={() => setOpen(row)}
                      >
                        Open
                      </button>
                      {row.status === 'SCHEDULED' && (
                        <button
                          type="button"
                          className="btn-secondary px-2 py-1 text-xs text-red-700"
                          onClick={() => setCancelling(row)}
                        >
                          Cancel
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </TableShell>
        )}
      </Card>

      {open && (
        <MessageModal message={open} onClose={() => setOpen(null)} onSaved={refetch} />
      )}

      <ConfirmDialog
        open={cancelling !== null}
        title="Cancel this reminder?"
        message={
          cancelling
            ? `${cancelling.customer_name} will not receive their ${localTime(
                cancelling.scheduled_at_local,
              )} message. Their next reminder will be planned from their next order.`
            : ''
        }
        confirmLabel="Cancel reminder"
        destructive
        onCancel={() => setCancelling(null)}
        onConfirm={async () => {
          if (!cancelling) return;
          const result = await cancel.run(cancelling.id);
          setCancelling(null);
          if (result) {
            notify('Reminder cancelled.');
            refetch();
          }
        }}
      />
    </>
  );
}

/** One customer's reminder: the reasoning behind it, and the copy itself. */
function MessageModal({
  message,
  onClose,
  onSaved,
}: {
  message: QueuedMessage;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [body, setBody] = useState(message.message);
  const save = useMutation(async () =>
    api.post(`/api/v1/smart-reorder/queue/${message.id}/edit`, { body }),
  );
  const editable = message.status === 'SCHEDULED' || message.status === 'DRAFT';

  return (
    <Modal
      open
      title={message.customer_name}
      description="This message belongs to this customer alone."
      onClose={onClose}
      size="md"
      footer={
        <>
          <button type="button" className="btn-secondary" onClick={onClose}>
            Close
          </button>
          {editable && (
            <button
              type="button"
              className="btn-primary"
              disabled={save.loading || body === message.message}
              onClick={async () => {
                const result = await save.run();
                if (result) {
                  notify('Message updated for this customer.');
                  onSaved();
                  onClose();
                }
              }}
            >
              {save.loading && <Spinner className="h-4 w-4 text-white" />}
              Save message
            </button>
          )}
        </>
      }
    >
      <dl className="grid grid-cols-2 gap-3 text-sm">
        <div>
          <dt className="text-xs text-slate-500">Usually orders</dt>
          <dd className="text-slate-800">
            {message.usual_day ?? '—'} {message.usual_time ?? ''}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-slate-500">Last product</dt>
          <dd className="text-slate-800">{message.product ?? '—'}</dd>
        </div>
        <div>
          <dt className="text-xs text-slate-500">Predicted next order</dt>
          <dd className="text-slate-800">{localTime(message.predicted_order_at_local)}</dd>
        </div>
        <div>
          <dt className="text-xs text-slate-500">Reminder scheduled</dt>
          <dd className="text-slate-800">{localTime(message.scheduled_at_local)}</dd>
        </div>
        <div>
          <dt className="text-xs text-slate-500">Confidence</dt>
          <dd className="text-slate-800">{message.confidence}%</dd>
        </div>
        <div>
          <dt className="text-xs text-slate-500">Channel</dt>
          <dd className="text-slate-800">{message.channel}</dd>
        </div>
      </dl>

      <label className="label mt-4" htmlFor="queued-message-body">
        Message
      </label>
      <textarea
        id="queued-message-body"
        className="input min-h-[120px] leading-relaxed"
        value={body}
        onChange={(e) => setBody(e.target.value)}
        disabled={!editable}
      />
      {save.error && (
        <p className="mt-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
          {save.error}
        </p>
      )}
      <p className="mt-2 text-xs text-slate-500">
        Editing this changes what this one customer receives. The campaign&apos;s template is
        untouched, and everyone else keeps their own message.
      </p>
    </Modal>
  );
}
