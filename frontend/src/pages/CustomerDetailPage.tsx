import { Fragment, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';

import { api } from '../api/client';
import GenerateMessagePanel from '../features/GenerateMessagePanel';
import {
  Badge,
  Card,
  EmptyState,
  ErrorState,
  Field,
  LoadingState,
  Modal,
  PageHeader,
  ProgressBar,
  SectionTitle,
  Spinner,
  TableShell,
  notify,
} from '../components/ui';
import { useMutation, useQuery } from '../hooks/useApi';
import type { CustomerDetail, Segment } from '../types';
import {
  formatBusinessTime,
  formatCurrency,
  formatDate,
  formatDateTime,
  formatDays,
  formatNumber,
  formatPercent,
  humanize,
} from '../utils/format';
import {
  AUTOMATION_KIND_LABEL,
  LIFECYCLE_BADGE,
  MESSAGE_STATUS_BADGE,
  RISK_BADGE,
  SEND_STATUS_BADGE,
  SKIP_REASON_LABEL,
} from '../utils/theme';

const TABS = ['Overview', 'Orders', 'Communications', 'Automations', 'Campaigns', 'History'] as const;
type Tab = (typeof TABS)[number];

export default function CustomerDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [tab, setTab] = useState<Tab>('Overview');
  const [showGenerate, setShowGenerate] = useState(false);
  const [showSuppress, setShowSuppress] = useState(false);
  const [showSegment, setShowSegment] = useState(false);

  const { data, loading, error, refetch } = useQuery<CustomerDetail>(
    id ? `/api/v1/customers/${id}` : null,
  );

  const recalc = useMutation(async () => api.post(`/api/v1/customers/${id}/recalculate`));
  const suppress = useMutation(async (reason: string) =>
    api.post(`/api/v1/customers/${id}/suppress`, { channel: 'ALL', reason }),
  );
  const unsuppress = useMutation(async () =>
    api.del(`/api/v1/customers/${id}/suppress?channel=ALL`),
  );

  if (loading) return <LoadingState label="Loading customer profile…" />;
  if (error) return <ErrorState message={error} onRetry={refetch} />;
  if (!data) return null;

  const p = data.profile;

  return (
    <>
      <button
        type="button"
        className="btn-ghost mb-3 -ml-2 px-2 py-1 text-xs"
        onClick={() => navigate(-1)}
      >
        ← Back
      </button>

      <PageHeader
        title={p.full_name || p.external_id}
        description={`${p.external_id} · ${p.email ?? 'no email'} · ${p.city ?? 'unknown city'}`}
        actions={
          <>
            <button
              type="button"
              className="btn-secondary"
              onClick={async () => {
                const result = await recalc.run();
                if (result) {
                  notify('Metrics, lifecycle, churn and recommendation recalculated.');
                  refetch();
                }
              }}
              disabled={recalc.loading}
            >
              {recalc.loading && <Spinner className="h-4 w-4" />}
              Recalculate
            </button>
            <button type="button" className="btn-secondary" onClick={() => setShowSegment(true)}>
              Segments
            </button>
            {p.is_suppressed ? (
              <button
                type="button"
                className="btn-secondary"
                onClick={async () => {
                  await unsuppress.run();
                  notify('Suppression removed.');
                  refetch();
                }}
              >
                Remove suppression
              </button>
            ) : (
              <button type="button" className="btn-secondary" onClick={() => setShowSuppress(true)}>
                Suppress
              </button>
            )}
            <button type="button" className="btn-primary" onClick={() => setShowGenerate(true)}>
              Generate message
            </button>
          </>
        }
      />

      <div className="mb-4 flex flex-wrap items-center gap-2">
        <Badge className={LIFECYCLE_BADGE[p.lifecycle_stage]}>{humanize(p.lifecycle_stage)}</Badge>
        <Badge className={RISK_BADGE[p.churn_risk_band]}>
          Churn {p.churn_score.toFixed(0)}/100 · {humanize(p.churn_risk_band)}
        </Badge>
        {p.rfm_segment && (
          <Badge className="bg-indigo-50 text-indigo-700 ring-indigo-200">
            RFM {p.rfm_cell} · {p.rfm_segment}
          </Badge>
        )}
        {p.is_suppressed && (
          <Badge className="bg-slate-200 text-slate-700 ring-slate-300">Suppressed</Badge>
        )}
        {!p.marketing_consent && (
          <Badge className="bg-red-50 text-red-700 ring-red-200">No marketing consent</Badge>
        )}
        {!p.age_verified && (
          <Badge className="bg-red-50 text-red-700 ring-red-200">Age not verified</Badge>
        )}
      </div>

      <div className="mb-4 flex gap-1 overflow-x-auto border-b border-slate-200">
        {TABS.map((t) => (
          <button
            key={t}
            type="button"
            onClick={() => setTab(t)}
            className={`whitespace-nowrap border-b-2 px-3 py-2 text-sm font-medium transition-colors ${
              tab === t
                ? 'border-brand-600 text-brand-700'
                : 'border-transparent text-slate-500 hover:text-slate-700'
            }`}
          >
            {t}
            {t === 'Orders' && ` (${data.orders.length})`}
            {t === 'Communications' && ` (${data.communication_events.length})`}
            {t === 'Automations' && ` (${data.automation_history.length})`}
            {t === 'Campaigns' && ` (${data.campaigns.length})`}
          </button>
        ))}
      </div>

      {tab === 'Overview' && <OverviewTab data={data} />}
      {tab === 'Orders' && <OrdersTab data={data} />}
      {tab === 'Communications' && <CommunicationsTab data={data} />}
      {tab === 'Automations' && <AutomationsTab data={data} />}
      {tab === 'Campaigns' && <CampaignsTab data={data} />}
      {tab === 'History' && <HistoryTab data={data} />}

      <Modal
        open={showGenerate}
        title="Generate a message"
        description="Grounded in this customer's verified data and your brand settings."
        onClose={() => setShowGenerate(false)}
        size="lg"
      >
        <GenerateMessagePanel
          customerId={p.id}
          defaultObjective={p.recommended_action}
          onGenerated={() => refetch()}
        />
      </Modal>

      <SuppressDialog
        open={showSuppress}
        busy={suppress.loading}
        name={p.full_name}
        onCancel={() => setShowSuppress(false)}
        onConfirm={async (reason) => {
          await suppress.run(reason);
          setShowSuppress(false);
          notify(`${p.full_name} is now suppressed from all messaging.`);
          refetch();
        }}
      />

      <SegmentDialog
        open={showSegment}
        customerId={p.id}
        current={data.segments}
        onClose={() => setShowSegment(false)}
        onChanged={refetch}
      />
    </>
  );
}

// --------------------------------------------------------------------------
function OverviewTab({ data }: { data: CustomerDetail }) {
  const p = data.profile;

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
      <div className="space-y-4 lg:col-span-2">
        <Card title="Next best action" description="Deterministic rule output, not an LLM guess.">
          <div className="flex flex-wrap items-center gap-2">
            <p className="text-base font-semibold text-slate-900">
              {humanize(p.recommended_action)}
            </p>
            <Badge className="bg-slate-100 text-slate-600 ring-slate-200">
              via {p.recommended_channel}
            </Badge>
          </div>
          <p className="mt-2 text-sm text-slate-600">{p.recommendation_explanation}</p>
          {p.recommendation_reason_codes.length > 0 && (
            <div className="mt-3 flex flex-wrap gap-1.5">
              {p.recommendation_reason_codes.map((code) => (
                <Badge key={code} className="bg-slate-100 font-mono text-slate-600 ring-slate-200">
                  {code}
                </Badge>
              ))}
            </div>
          )}
        </Card>

        <Card
          title="Churn risk"
          description="Every point comes from a named, weighted factor computed in code."
        >
          <div className="flex items-center gap-4">
            <div className="w-24 shrink-0">
              <p className="text-3xl font-semibold tabular-nums text-slate-900">
                {p.churn_score.toFixed(0)}
              </p>
              <p className="text-xs text-slate-500">out of 100</p>
            </div>
            <div className="min-w-0 flex-1">
              <ProgressBar value={p.churn_score} tone={p.churn_risk_band} />
              <p className="mt-2 text-sm text-slate-600">{p.churn_explanation}</p>
            </div>
          </div>

          {p.churn_factors.length > 0 && (
            <div className="mt-5">
              <SectionTitle>Contributing factors</SectionTitle>
              <ul className="space-y-3">
                {p.churn_factors.map((factor) => (
                  <li key={factor.code}>
                    <div className="flex items-baseline justify-between gap-3">
                      <p className="text-sm font-medium text-slate-800">{factor.label}</p>
                      <p className="shrink-0 text-xs tabular-nums text-slate-500">
                        +{factor.points.toFixed(1)} pts
                      </p>
                    </div>
                    <p className="mt-0.5 text-xs text-slate-500">{factor.detail}</p>
                    <div className="mt-1.5">
                      <ProgressBar value={factor.severity * 100} tone={p.churn_risk_band} />
                    </div>
                  </li>
                ))}
              </ul>
            </div>
          )}
          <p className="mt-4 border-t border-slate-100 pt-3 text-xs text-slate-500">
            Revenue at risk: {formatCurrency(p.revenue_at_risk)} annualised.
          </p>
        </Card>

        <Card title="Purchase behaviour">
          <dl className="grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-3">
            <Field label="Lifetime revenue" value={formatCurrency(p.lifetime_revenue, true)} />
            <Field label="Average order value" value={formatCurrency(p.average_order_value, true)} />
            <Field label="Estimated LTV" value={formatCurrency(p.estimated_ltv)} />
            <Field label="Completed orders" value={formatNumber(p.completed_orders)} />
            <Field label="Cancelled / refunded" value={formatNumber(p.cancelled_orders)} />
            <Field label="Total units" value={formatNumber(p.total_units)} />
            <Field label="First order" value={formatDate(p.first_order_at)} />
            <Field label="Last order" value={formatDate(p.last_order_at)} />
            <Field label="Days since last order" value={formatDays(p.days_since_last_order)} />
            <Field
              label="Expected cycle"
              value={p.expected_cycle_days ? `${p.expected_cycle_days} days` : '—'}
              hint={p.cadence_source ? `${p.cadence_source} cadence` : undefined}
            />
            <Field
              label="Days overdue"
              value={
                p.days_overdue === null ? (
                  '—'
                ) : (
                  <span className={p.days_overdue > 0 ? 'text-orange-700' : 'text-emerald-700'}>
                    {p.days_overdue > 0 ? `+${p.days_overdue.toFixed(0)}` : p.days_overdue.toFixed(0)}
                  </span>
                )
              }
            />
            <Field
              label="Median interval"
              value={p.median_purchase_interval_days ? `${p.median_purchase_interval_days} days` : '—'}
            />
            <Field
              label="Average interval"
              value={p.average_purchase_interval_days ? `${p.average_purchase_interval_days} days` : '—'}
            />
            <Field
              label="Purchase frequency"
              value={`${p.purchase_frequency_per_month.toFixed(2)} / month`}
            />
            <Field label="Discount dependency" value={formatPercent(p.discount_dependency, 0)} />
            <Field label="Engagement score" value={`${p.engagement_score.toFixed(0)} / 100`} />
            <Field label="Typical order day" value={p.typical_order_weekday ?? '—'} />
            <Field
              label="Typical order time"
              value={p.typical_order_hour !== null ? `${String(p.typical_order_hour).padStart(2, '0')}:00` : '—'}
            />
          </dl>

          <div className="mt-5 grid gap-4 border-t border-slate-100 pt-4 sm:grid-cols-3">
            <div>
              <SectionTitle>Preferred categories</SectionTitle>
              <div className="flex flex-wrap gap-1.5">
                {p.preferred_categories.length ? (
                  p.preferred_categories.map((c) => (
                    <Badge key={c} className="bg-slate-100 text-slate-700 ring-slate-200">
                      {c}
                    </Badge>
                  ))
                ) : (
                  <span className="text-xs text-slate-400">None yet</span>
                )}
              </div>
            </div>
            <div>
              <SectionTitle>Preferred brands</SectionTitle>
              <div className="flex flex-wrap gap-1.5">
                {p.preferred_brands.length ? (
                  p.preferred_brands.map((b) => (
                    <Badge key={b} className="bg-slate-100 text-slate-700 ring-slate-200">
                      {b}
                    </Badge>
                  ))
                ) : (
                  <span className="text-xs text-slate-400">None yet</span>
                )}
              </div>
            </div>
            <div>
              <SectionTitle>Top products</SectionTitle>
              <ul className="space-y-1 text-xs text-slate-600">
                {p.top_products.length ? (
                  p.top_products.map((product) => (
                    <li key={product.product_name} className="flex justify-between gap-2">
                      <span className="truncate">{product.product_name}</span>
                      <span className="shrink-0 tabular-nums text-slate-400">
                        ×{product.quantity}
                      </span>
                    </li>
                  ))
                ) : (
                  <li className="text-slate-400">None yet</li>
                )}
              </ul>
            </div>
          </div>
        </Card>
      </div>

      <div className="space-y-4">
        <Card title="Identity">
          <dl className="space-y-3">
            <Field label="Customer ID" value={<span className="font-mono text-xs">{p.external_id}</span>} />
            <Field label="Email" value={p.email ?? '—'} />
            <Field label="Phone" value={p.phone ?? '—'} />
            <Field
              label="Location"
              value={[p.city, p.region, p.postcode].filter(Boolean).join(', ') || '—'}
            />
            <Field label="Signed up" value={formatDate(p.signup_date)} />
            <Field label="Source" value={p.acquisition_source ?? '—'} />
            <Field
              label="Age verified"
              value={
                p.age_verified ? (
                  <Badge className="bg-emerald-50 text-emerald-700 ring-emerald-200">Verified</Badge>
                ) : (
                  <Badge className="bg-red-50 text-red-700 ring-red-200">Not verified</Badge>
                )
              }
            />
          </dl>
        </Card>

        <Card title="Consent & suppression">
          <ul className="space-y-2">
            {(
              [
                ['Marketing', p.marketing_consent],
                ['Email', p.email_consent],
                ['SMS', p.sms_consent],
                ['WhatsApp', p.whatsapp_consent],
              ] as [string, boolean][]
            ).map(([label, granted]) => (
              <li key={label} className="flex items-center justify-between">
                <span className="text-sm text-slate-700">{label}</span>
                <Badge
                  className={
                    granted
                      ? 'bg-emerald-50 text-emerald-700 ring-emerald-200'
                      : 'bg-slate-100 text-slate-500 ring-slate-200'
                  }
                >
                  {granted ? 'Granted' : 'Not granted'}
                </Badge>
              </li>
            ))}
          </ul>
          {p.suppressed_channels.length > 0 && (
            <div className="mt-4 border-t border-slate-100 pt-3">
              <SectionTitle>Suppressed channels</SectionTitle>
              <div className="flex flex-wrap gap-1.5">
                {p.suppressed_channels.map((c) => (
                  <Badge key={c} className="bg-slate-200 text-slate-700 ring-slate-300">
                    {c}
                  </Badge>
                ))}
              </div>
            </div>
          )}
          <p className="mt-3 text-xs text-slate-500">
            Preferred channel: <span className="font-medium">{p.preferred_channel}</span>
          </p>
        </Card>

        <Card title="RFM">
          <dl className="grid grid-cols-3 gap-3 text-center">
            <ScoreTile label="Recency" value={p.recency_score} />
            <ScoreTile label="Frequency" value={p.frequency_score} />
            <ScoreTile label="Monetary" value={p.monetary_score} />
          </dl>
          <p className="mt-3 text-center text-sm text-slate-600">
            {p.rfm_segment ?? 'Not scored'}{' '}
            <span className="font-mono text-xs text-slate-400">{p.rfm_cell ?? ''}</span>
          </p>
        </Card>

        {data.segments.length > 0 && (
          <Card title="Segments">
            <div className="flex flex-wrap gap-1.5">
              {data.segments.map((s) => (
                <Link key={s.id} to={`/customers?segment_id=${s.id}`}>
                  <Badge className="bg-brand-50 text-brand-700 ring-brand-200">{s.name}</Badge>
                </Link>
              ))}
            </div>
          </Card>
        )}

        {data.attribution.length > 0 && (
          <Card title="Attributed conversions">
            <ul className="space-y-3">
              {data.attribution.map((a) => (
                <li key={a.order_external_id} className="text-sm">
                  <div className="flex items-baseline justify-between gap-2">
                    <Link
                      to={`/campaigns/${a.campaign_id}`}
                      className="truncate font-medium text-brand-700 hover:text-brand-800"
                    >
                      {a.campaign_name}
                    </Link>
                    <span className="shrink-0 tabular-nums text-slate-900">
                      {formatCurrency(a.revenue, true)}
                    </span>
                  </div>
                  <p className="text-xs text-slate-500">
                    {formatDate(a.ordered_at)} · {a.hours_since_touch.toFixed(1)}h after contact
                    {a.is_reactivation && ' · reactivation'}
                  </p>
                </li>
              ))}
            </ul>
          </Card>
        )}
      </div>
    </div>
  );
}

function ScoreTile({ label, value }: { label: string; value: number | null }) {
  return (
    <div className="rounded-lg bg-slate-50 py-3">
      <p className="text-xl font-semibold tabular-nums text-slate-900">{value ?? '—'}</p>
      <p className="text-xs text-slate-500">{label}</p>
    </div>
  );
}

// --------------------------------------------------------------------------
/**
 * Every automated message aimed at this customer, sent or withheld.
 *
 * The withheld ones are the point: without them, a customer who received
 * nothing looks identical to one who was never in the audience, and "why
 * didn't they get the campaign?" has no answer on their own profile.
 */
function AutomationsTab({ data }: { data: CustomerDetail }) {
  const rows = data.automation_history;
  if (rows.length === 0) {
    return (
      <Card>
        <EmptyState
          title="No automated messages"
          description="This customer has not been in the audience of any automation run."
        />
      </Card>
    );
  }

  const sent = rows.filter((r) => r.status === 'SENT' || r.status === 'DELIVERED').length;
  const withheld = rows.filter((r) => r.status === 'SKIPPED').length;

  return (
    <Card
      title="Automation history"
      description={`${sent} sent, ${withheld} withheld. Times are NZ.`}
      bodyClassName=""
    >
      <TableShell>
        <thead className="bg-slate-50">
          <tr>
            <th className="table-head">Automation</th>
            <th className="table-head">When</th>
            <th className="table-head">Status</th>
            <th className="table-head">Message or reason</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {rows.map((row) => (
            <tr key={row.id} className="hover:bg-slate-50">
              <td className="table-cell">
                <Link
                  to={`/automations/${row.automation_id}`}
                  className="font-medium text-brand-700 hover:underline"
                >
                  {row.automation_name}
                </Link>
                <p className="mt-0.5 text-xs text-slate-500">
                  {AUTOMATION_KIND_LABEL[row.automation_kind] ?? row.automation_kind}
                  {row.variant_index != null &&
                    ` · variant ${String.fromCharCode(65 + row.variant_index)}`}
                </p>
              </td>
              <td className="table-cell text-sm text-slate-600">
                {formatBusinessTime(row.sent_at ?? row.scheduled_for)}
              </td>
              <td className="table-cell">
                <Badge className={SEND_STATUS_BADGE[row.status]}>{row.status}</Badge>
                {row.delivered_at && (
                  <p className="mt-0.5 text-xs text-slate-500">
                    delivered {formatBusinessTime(row.delivered_at)}
                  </p>
                )}
              </td>
              <td className="table-cell max-w-lg whitespace-normal text-sm">
                {row.skip_reason ? (
                  <span className="text-amber-800">
                    <strong>{SKIP_REASON_LABEL[row.skip_reason] ?? row.skip_reason}</strong>
                    {row.skip_detail ? ` — ${row.skip_detail}` : ''}
                  </span>
                ) : (
                  <span className="text-slate-600">
                    {row.generated && (
                      <span
                        className="mr-1.5 rounded bg-violet-50 px-1.5 py-0.5 text-xs font-medium text-violet-700 ring-1 ring-violet-200"
                        title="Drafted for this customer by the model, then compliance-checked."
                      >
                        Drafted
                      </span>
                    )}
                    {row.error_message ?? row.body}
                  </span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </TableShell>
    </Card>
  );
}

function OrdersTab({ data }: { data: CustomerDetail }) {
  const [expanded, setExpanded] = useState<number | null>(null);

  if (data.orders.length === 0) {
    return (
      <Card>
        <EmptyState
          title="No orders yet"
          description="This customer has not placed an order."
        />
      </Card>
    );
  }

  return (
    <Card bodyClassName="">
      <TableShell>
        <thead className="bg-slate-50">
          <tr>
            <th className="table-head">Order</th>
            <th className="table-head">Date</th>
            <th className="table-head">Status</th>
            <th className="table-head text-right">Total</th>
            <th className="table-head text-right">Discount</th>
            <th className="table-head">Coupon</th>
            <th className="table-head text-right">Items</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {data.orders.map((order) => (
            // A Fragment is needed so an order and its expanded item rows stay
            // siblings inside <tbody>; it carries the key for the pair.
            <Fragment key={order.id}>
              <tr
                className="cursor-pointer hover:bg-slate-50"
                onClick={() => setExpanded(expanded === order.id ? null : order.id)}
              >
                <td className="table-cell font-mono text-xs">{order.external_id}</td>
                <td className="table-cell">{formatDateTime(order.ordered_at)}</td>
                <td className="table-cell">
                  <Badge
                    className={
                      order.status === 'COMPLETED'
                        ? 'bg-emerald-50 text-emerald-700 ring-emerald-200'
                        : 'bg-slate-100 text-slate-600 ring-slate-200'
                    }
                  >
                    {humanize(order.status)}
                  </Badge>
                </td>
                <td className="table-cell text-right tabular-nums">
                  {formatCurrency(order.total_amount, true)}
                </td>
                <td className="table-cell text-right tabular-nums text-slate-500">
                  {order.discount_amount > 0 ? formatCurrency(order.discount_amount, true) : '—'}
                </td>
                <td className="table-cell font-mono text-xs">{order.coupon_code ?? '—'}</td>
                <td className="table-cell text-right tabular-nums">{order.items.length}</td>
              </tr>
              {expanded === order.id && order.items.length > 0 && (
                <tr className="bg-slate-50">
                  <td colSpan={7} className="px-6 py-3">
                    <ul className="space-y-1">
                      {order.items.map((item) => (
                        <li key={item.id} className="flex justify-between gap-4 text-xs">
                          <span className="text-slate-700">
                            {item.product_name}
                            <span className="ml-2 text-slate-400">
                              {item.category} · {item.brand}
                            </span>
                          </span>
                          <span className="shrink-0 tabular-nums text-slate-600">
                            {item.quantity} × {formatCurrency(item.unit_price, true)} ={' '}
                            {formatCurrency(item.line_total, true)}
                          </span>
                        </li>
                      ))}
                    </ul>
                  </td>
                </tr>
              )}
            </Fragment>
          ))}
        </tbody>
      </TableShell>
    </Card>
  );
}

function CommunicationsTab({ data }: { data: CustomerDetail }) {
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Card title="Message history" bodyClassName="">
        {data.messages.length === 0 ? (
          <EmptyState title="No messages" description="Nothing has been generated or sent yet." />
        ) : (
          <ul className="divide-y divide-slate-100">
            {data.messages.map((m) => (
              <li key={m.id} className="px-5 py-3">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium text-slate-800">
                      {m.subject || humanize(m.objective)}
                    </p>
                    <p className="text-xs text-slate-500">
                      {m.channel} · {formatDateTime(m.sent_at ?? m.created_at)}
                      {m.is_test && ' · test'}
                    </p>
                  </div>
                  <Badge
                    className={
                      MESSAGE_STATUS_BADGE[m.status] ?? 'bg-slate-100 text-slate-700 ring-slate-200'
                    }
                  >
                    {humanize(m.status)}
                  </Badge>
                </div>
                <p className="mt-1.5 line-clamp-2 whitespace-pre-wrap text-xs text-slate-500">
                  {m.body}
                </p>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card title="Communication events" bodyClassName="">
        {data.communication_events.length === 0 ? (
          <EmptyState title="No events" description="Delivery and engagement events appear here." />
        ) : (
          <ul className="divide-y divide-slate-100">
            {data.communication_events.map((e) => (
              <li key={e.id} className="flex items-center justify-between gap-3 px-5 py-2.5">
                <div className="min-w-0">
                  <p className="truncate text-sm text-slate-800">{humanize(e.event_type)}</p>
                  <p className="text-xs text-slate-500">
                    {e.channel} · {e.provider} · {formatDateTime(e.occurred_at)}
                  </p>
                </div>
                {e.is_simulated && (
                  <Badge className="shrink-0 bg-amber-50 text-amber-800 ring-amber-200">
                    Simulated
                  </Badge>
                )}
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}

function CampaignsTab({ data }: { data: CustomerDetail }) {
  if (data.campaigns.length === 0) {
    return (
      <Card>
        <EmptyState
          title="Not in any campaign"
          description="This customer has not been included in a campaign audience yet."
        />
      </Card>
    );
  }
  return (
    <Card bodyClassName="">
      <TableShell>
        <thead className="bg-slate-50">
          <tr>
            <th className="table-head">Campaign</th>
            <th className="table-head">Channel</th>
            <th className="table-head">Status</th>
            <th className="table-head">Sent</th>
            <th className="table-head">Opened</th>
            <th className="table-head">Converted</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {data.campaigns.map((c) => (
            <tr key={c.campaign_id} className="hover:bg-slate-50">
              <td className="table-cell">
                <Link
                  to={`/campaigns/${c.campaign_id}`}
                  className="font-medium text-brand-700 hover:text-brand-800"
                >
                  {c.name}
                </Link>
                <p className="text-xs text-slate-500">{humanize(c.objective)}</p>
              </td>
              <td className="table-cell">{c.channel}</td>
              <td className="table-cell">
                <Badge className="bg-slate-100 text-slate-700 ring-slate-200">
                  {humanize(c.status)}
                </Badge>
                {c.exclusion_reason && (
                  <p className="mt-1 max-w-xs whitespace-normal text-xs text-slate-500">
                    {c.exclusion_reason}
                  </p>
                )}
              </td>
              <td className="table-cell text-xs">{formatDateTime(c.sent_at)}</td>
              <td className="table-cell text-xs">{formatDateTime(c.opened_at)}</td>
              <td className="table-cell text-xs">{formatDateTime(c.converted_at)}</td>
            </tr>
          ))}
        </tbody>
      </TableShell>
    </Card>
  );
}

function HistoryTab({ data }: { data: CustomerDetail }) {
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Card title="Lifecycle transitions">
        {data.lifecycle_history.length === 0 ? (
          <EmptyState title="No transitions" description="No stage change has been recorded." />
        ) : (
          <ol className="space-y-4">
            {data.lifecycle_history.map((h, index) => (
              <li key={`${h.changed_at}-${index}`} className="relative pl-6">
                <span className="absolute left-0 top-1.5 h-2.5 w-2.5 rounded-full bg-brand-500" />
                {index < data.lifecycle_history.length - 1 && (
                  <span className="absolute left-[4.5px] top-5 h-full w-px bg-slate-200" />
                )}
                <div className="flex flex-wrap items-center gap-1.5">
                  {h.from_stage && (
                    <>
                      <Badge className="bg-slate-100 text-slate-600 ring-slate-200">
                        {humanize(h.from_stage)}
                      </Badge>
                      <span className="text-slate-400">→</span>
                    </>
                  )}
                  <Badge
                    className={
                      LIFECYCLE_BADGE[h.to_stage as keyof typeof LIFECYCLE_BADGE] ??
                      'bg-slate-100 text-slate-700 ring-slate-200'
                    }
                  >
                    {humanize(h.to_stage)}
                  </Badge>
                </div>
                <p className="mt-1 text-xs text-slate-600">{h.reason}</p>
                <p className="text-xs text-slate-400">{formatDateTime(h.changed_at)}</p>
              </li>
            ))}
          </ol>
        )}
      </Card>

      <Card title="Consent history">
        {data.profile.consent_history.length === 0 ? (
          <EmptyState title="No consent records" />
        ) : (
          <ul className="divide-y divide-slate-100">
            {data.profile.consent_history.map((c, index) => (
              <li key={index} className="flex items-center justify-between gap-3 py-2.5">
                <div>
                  <p className="text-sm text-slate-800">{humanize(c.consent_type)}</p>
                  <p className="text-xs text-slate-500">
                    {c.source} · {formatDateTime(c.occurred_at)}
                  </p>
                </div>
                <Badge
                  className={
                    c.granted
                      ? 'bg-emerald-50 text-emerald-700 ring-emerald-200'
                      : 'bg-red-50 text-red-700 ring-red-200'
                  }
                >
                  {c.granted ? 'Granted' : 'Revoked'}
                </Badge>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}

// --------------------------------------------------------------------------
function SuppressDialog({
  open,
  busy,
  name,
  onCancel,
  onConfirm,
}: {
  open: boolean;
  busy: boolean;
  name: string;
  onCancel: () => void;
  onConfirm: (reason: string) => void;
}) {
  const [reason, setReason] = useState('');
  return (
    <Modal
      open={open}
      title="Suppress this customer"
      description="They will be excluded from every campaign on every channel."
      onClose={onCancel}
      size="sm"
      footer={
        <>
          <button type="button" className="btn-secondary" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
          <button
            type="button"
            className="btn-danger"
            onClick={() => onConfirm(reason)}
            disabled={busy}
          >
            {busy && <Spinner className="h-4 w-4 text-white" />}
            Suppress {name}
          </button>
        </>
      }
    >
      <label className="label" htmlFor="suppress-reason">
        Reason (recorded in the audit log)
      </label>
      <input
        id="suppress-reason"
        className="input"
        placeholder="e.g. Customer asked us to stop"
        value={reason}
        onChange={(e) => setReason(e.target.value)}
      />
    </Modal>
  );
}

function SegmentDialog({
  open,
  customerId,
  current,
  onClose,
  onChanged,
}: {
  open: boolean;
  customerId: number;
  current: { id: number; name: string; segment_type: string }[];
  onClose: () => void;
  onChanged: () => void;
}) {
  const { data: segments } = useQuery<Segment[]>(open ? '/api/v1/segments' : null);
  const add = useMutation(async (segmentId: number) =>
    api.post(`/api/v1/segments/${segmentId}/members/${customerId}`),
  );
  const manual = (segments ?? []).filter((s) => s.segment_type === 'MANUAL');
  const currentIds = new Set(current.map((c) => c.id));

  return (
    <Modal open={open} title="Segments" onClose={onClose} size="md">
      <SectionTitle>Currently in</SectionTitle>
      {current.length === 0 ? (
        <p className="text-sm text-slate-500">Not a member of any segment.</p>
      ) : (
        <div className="flex flex-wrap gap-1.5">
          {current.map((s) => (
            <Badge key={s.id} className="bg-brand-50 text-brand-700 ring-brand-200">
              {s.name}
            </Badge>
          ))}
        </div>
      )}

      <div className="mt-5 border-t border-slate-200 pt-4">
        <SectionTitle>Add to a manual segment</SectionTitle>
        {manual.length === 0 ? (
          <p className="text-sm text-slate-500">
            You have no manual segments. Dynamic segment membership is set by the rule, so a
            customer cannot be added by hand.{' '}
            <Link to="/segments" className="font-medium text-brand-600">
              Create one
            </Link>
            .
          </p>
        ) : (
          <ul className="space-y-2">
            {manual.map((s) => (
              <li key={s.id} className="flex items-center justify-between gap-3">
                <span className="text-sm text-slate-700">{s.name}</span>
                <button
                  type="button"
                  className="btn-secondary px-2.5 py-1 text-xs"
                  disabled={currentIds.has(s.id) || add.loading}
                  onClick={async () => {
                    await add.run(s.id);
                    notify(`Added to ${s.name}.`);
                    onChanged();
                  }}
                >
                  {currentIds.has(s.id) ? 'Already in' : 'Add'}
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </Modal>
  );
}
