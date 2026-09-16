import { Badge, Card, Spinner } from '../components/ui';
import { useQuery } from '../hooks/useApi';

/**
 * A customer's ordering routine, as the prediction engine sees it.
 *
 * Every time here is the customer's own local clock, computed server-side.
 * The browser deliberately does no timezone arithmetic: the backend already
 * converts out of the UTC the database stores, and a second conversion in the
 * front end is how the two drift apart.
 */
interface OrderPatternResponse {
  customer_id: number;
  has_prediction: boolean;
  reason: string;
  preferred_weekday_name: string | null;
  preferred_time_label: string | null;
  last_order_at: string | null;
  predicted_next_order_at: string | null;
  reminder_at: string | null;
  interval_label: string | null;
  overall_confidence: number;
  day_confidence: number;
  time_confidence: number;
  interval_confidence: number;
  confidence_band: 'HIGH' | 'MEDIUM' | 'LOW';
  orders_considered: number;
  timezone: string;
}

const BAND_STYLES: Record<string, string> = {
  HIGH: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
  MEDIUM: 'bg-amber-50 text-amber-800 ring-amber-200',
  LOW: 'bg-slate-100 text-slate-600 ring-slate-200',
};

/** A server-sent naive local datetime, shown as the customer would read it. */
function formatLocal(value: string | null): string {
  if (!value) return '—';
  // Parsed field by field rather than handed to Date(): these are already
  // local to the business, and letting the browser apply its own zone would
  // shift a New Zealand evening by however far away the viewer happens to be.
  const match = value.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
  if (!match) return value;
  const [, year, month, day, hour, minute] = match;
  const date = new Date(
    Number(year),
    Number(month) - 1,
    Number(day),
    Number(hour),
    Number(minute),
  );
  return date.toLocaleString('en-NZ', {
    weekday: 'long',
    day: 'numeric',
    month: 'short',
    hour: 'numeric',
    minute: '2-digit',
    hour12: true,
  });
}

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-1.5">
      <span className="text-xs text-slate-500">{label}</span>
      <span className="text-right text-sm font-medium text-slate-900">{value}</span>
    </div>
  );
}

export default function OrderingPatternCard({ customerId }: { customerId: number | string }) {
  const { data, loading, error } = useQuery<OrderPatternResponse>(
    `/api/v1/customers/${customerId}/order-pattern`,
  );

  return (
    <Card
      title="Ordering pattern"
      description="Learned from this customer's own order history."
      actions={
        data?.has_prediction ? (
          <Badge className={BAND_STYLES[data.confidence_band]}>
            {data.overall_confidence}% confident
          </Badge>
        ) : undefined
      }
    >
      {loading && (
        <div className="flex items-center gap-2 text-sm text-slate-500">
          <Spinner className="h-4 w-4" />
          Reading order history…
        </div>
      )}

      {error && <p className="text-sm text-red-600">{error}</p>}

      {/* Not an error state. A customer with two orders genuinely has no
          routine, and the engine's own sentence says so better than a blank
          panel or an invented figure would. */}
      {data && !data.has_prediction && !loading && (
        <div className="rounded-lg bg-slate-50 p-3">
          <p className="text-sm text-slate-600">{data.reason}</p>
          <p className="mt-2 text-xs text-slate-400">
            Smart Reorder: not eligible yet
          </p>
        </div>
      )}

      {data?.has_prediction && (
        <>
          <div className="divide-y divide-slate-100">
            <Row label="Typical day" value={data.preferred_weekday_name ?? '—'} />
            <Row label="Typical time" value={data.preferred_time_label ?? '—'} />
            <Row label="Typical interval" value={`Every ${data.interval_label}`} />
            <Row label="Last order" value={formatLocal(data.last_order_at)} />
            <Row
              label="Predicted next order"
              value={formatLocal(data.predicted_next_order_at)}
            />
            <Row label="Reminder would send" value={formatLocal(data.reminder_at)} />
            <Row
              label="Smart Reorder"
              value={
                data.confidence_band === 'LOW' ? (
                  <span className="text-slate-500">Below confidence threshold</span>
                ) : (
                  <span className="text-emerald-700">Eligible</span>
                )
              }
            />
          </div>

          {/* The three parts are shown separately because they fail
              separately: a customer can keep a rock-solid weekday and order at
              any hour, and one blended number would hide which. */}
          <div className="mt-3 grid grid-cols-3 gap-2 border-t border-slate-100 pt-3">
            {[
              ['Day', data.day_confidence],
              ['Time', data.time_confidence],
              ['Interval', data.interval_confidence],
            ].map(([label, value]) => (
              <div key={label as string} className="text-center">
                <p className="text-sm font-semibold text-slate-900">{value}%</p>
                <p className="text-xs text-slate-500">{label}</p>
              </div>
            ))}
          </div>

          <p className="mt-3 text-xs text-slate-400">
            Based on {data.orders_considered} orders · times shown in {data.timezone}
          </p>
        </>
      )}
    </Card>
  );
}
