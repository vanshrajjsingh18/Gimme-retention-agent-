import { Link } from 'react-router-dom';
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

import {
  Badge,
  Card,
  EmptyState,
  ErrorState,
  LoadingState,
  PageHeader,
  StatTile,
} from '../components/ui';
import { useQuery } from '../hooks/useApi';
import type { OverviewAnalytics } from '../types';
import { countFormatter, humanizeLabel, tooltipStyle } from '../utils/charts';
import { formatCurrency, formatDateTime, formatNumber, formatPercent, humanize } from '../utils/format';
import { LIFECYCLE_BADGE, lifecycleColor } from '../utils/theme';

interface ActivityItem {
  type: string;
  at: string;
  title: string;
  detail: string;
  customer_id: number | null;
  campaign_id: number | null;
}

export default function OverviewPage() {
  const { data, loading, error, refetch } = useQuery<OverviewAnalytics>('/api/v1/analytics/overview');
  const { data: activity } = useQuery<{ activity: ActivityItem[] }>('/api/v1/analytics/activity?limit=8');

  if (loading) return <LoadingState label="Loading retention overview…" />;
  if (error) return <ErrorState message={error} onRetry={refetch} />;
  if (!data) return null;

  const hasData = data.total_customers > 0;

  return (
    <>
      <PageHeader
        title="Retention overview"
        description={`Computed from your database at ${formatDateTime(data.generated_at)}.`}
        actions={
          <Link to="/customers?churn_risk_band=CRITICAL" className="btn-secondary">
            View critical risk
          </Link>
        }
      />

      {!hasData ? (
        <Card>
          <EmptyState
            title="No customer data yet"
            description="Import customers and orders, or generate the synthetic demo dataset, to populate every dashboard."
            action={
              <Link to="/data" className="btn-primary">
                Go to data & imports
              </Link>
            }
          />
        </Card>
      ) : (
        <>
          <section className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <StatTile
              label="Total customers"
              value={formatNumber(data.total_customers)}
              sublabel={`${formatNumber(data.active_customers)} active`}
            />
            <StatTile
              label="Revenue (30 days)"
              value={formatCurrency(data.revenue_30d)}
              trend={data.revenue_change_30d}
              sublabel={`${formatNumber(data.orders_30d)} orders`}
            />
            <StatTile
              label="Average order value"
              value={formatCurrency(data.average_order_value, true)}
              sublabel={`${formatNumber(data.total_orders)} orders all time`}
            />
            <StatTile
              label="Repeat purchase rate"
              value={formatPercent(data.repeat_purchase_rate)}
              sublabel={`${formatNumber(data.repeat_customers)} repeat customers`}
            />
          </section>

          <section className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <StatTile
              label="At risk"
              value={formatNumber(data.at_risk_customers)}
              tone="warning"
              sublabel="overdue on their own cycle"
            />
            <StatTile
              label="Dormant + churned"
              value={formatNumber(data.dormant_customers + data.churned_customers)}
              tone="critical"
              sublabel={`${formatNumber(data.dormant_customers)} dormant, ${formatNumber(
                data.churned_customers,
              )} churned`}
            />
            <StatTile
              label="Revenue at risk"
              value={formatCurrency(data.revenue_at_risk)}
              tone="critical"
              sublabel="annualised, weighted by churn score"
            />
            <StatTile
              label="Campaign revenue"
              value={formatCurrency(data.campaign_attributed_revenue)}
              tone="positive"
              sublabel={`${formatPercent(data.campaign_revenue_share)} of total revenue`}
            />
          </section>

          <section className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-3">
            <Card
              title="Lifecycle distribution"
              description="Every customer sits in exactly one stage."
              className="lg:col-span-2"
            >
              <div className="h-72">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart
                    data={data.lifecycle_distribution}
                    margin={{ top: 8, right: 8, bottom: 8, left: 0 }}
                  >
                    <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" vertical={false} />
                    <XAxis
                      dataKey="stage"
                      tickFormatter={(v: string) => humanize(v)}
                      tick={{ fontSize: 11, fill: '#64748b' }}
                      interval={0}
                      angle={-30}
                      textAnchor="end"
                      height={64}
                    />
                    <YAxis tick={{ fontSize: 11, fill: '#64748b' }} allowDecimals={false} />
                    <Tooltip
                      formatter={countFormatter('Customers')}
                      labelFormatter={humanizeLabel}
                      contentStyle={tooltipStyle}
                    />
                    <Bar dataKey="count" radius={[4, 4, 0, 0]}>
                      {data.lifecycle_distribution.map((entry) => (
                        <Cell key={entry.stage} fill={lifecycleColor(entry.stage)} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div>
              <div className="mt-3 flex flex-wrap gap-1.5">
                {data.lifecycle_distribution.map((entry) => (
                  <Link
                    key={entry.stage}
                    to={`/customers?lifecycle_stage=${entry.stage}`}
                    className="inline-flex"
                  >
                    <Badge
                      className={
                        LIFECYCLE_BADGE[entry.stage as keyof typeof LIFECYCLE_BADGE] ??
                        'bg-slate-100 text-slate-700 ring-slate-200'
                      }
                    >
                      {humanize(entry.stage)} · {formatNumber(entry.count)}
                    </Badge>
                  </Link>
                ))}
              </div>
            </Card>

            <Card title="Retention health" description="Rates derived from order history.">
              <dl className="space-y-4">
                <MetricRow
                  label="90-day retention"
                  value={formatPercent(data.retention_rate_90d)}
                  hint="Ordered in the last 90 days and the 90 before"
                />
                {/* This counts returns we can *attribute to a campaign*, which
                    is a different and much smaller number than how many
                    customers came back. Labelling it "returned after a lapse"
                    read as a flat contradiction of the row below. */}
                <MetricRow
                  label="Campaign-driven win-backs"
                  value={formatPercent(data.reactivation_rate)}
                  hint={`${formatNumber(data.total_reactivations)} of ${formatNumber(
                    data.dormant_customers + data.churned_customers,
                  )} lapsed customers returned via a campaign`}
                />
                <MetricRow
                  label="New customers (30 days)"
                  value={formatNumber(data.new_customers_30d)}
                  hint="Signed up in the last 30 days"
                />
                <MetricRow
                  label="Reactivated customers"
                  value={formatNumber(data.reactivated_customers)}
                  hint="Came back after lapsing, however they found us"
                />
                <MetricRow
                  label="Estimated LTV pool"
                  value={formatCurrency(data.estimated_ltv_total)}
                  hint="Sum of per-customer projections"
                />
              </dl>
            </Card>
          </section>

          <section className="mt-4">
            <Card title="Recent activity" description="Conversions and campaign sends.">
              {!activity || activity.activity.length === 0 ? (
                <EmptyState
                  title="No activity yet"
                  description="Run a campaign to see conversions and sends appear here."
                />
              ) : (
                <ul className="divide-y divide-slate-100">
                  {activity.activity.map((item, index) => (
                    <li key={`${item.type}-${item.at}-${index}`} className="flex items-start gap-3 py-3">
                      <Badge
                        className={
                          item.type === 'CONVERSION'
                            ? 'bg-emerald-50 text-emerald-700 ring-emerald-200'
                            : 'bg-blue-50 text-blue-700 ring-blue-200'
                        }
                      >
                        {humanize(item.type)}
                      </Badge>
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-sm text-slate-800">{item.title}</p>
                        <p className="text-xs text-slate-500">
                          {item.detail} · {formatDateTime(item.at)}
                        </p>
                      </div>
                      {item.campaign_id && (
                        <Link
                          to={`/campaigns/${item.campaign_id}`}
                          className="shrink-0 text-xs font-medium text-brand-600 hover:text-brand-700"
                        >
                          View
                        </Link>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </Card>
          </section>
        </>
      )}
    </>
  );
}

function MetricRow({ label, value, hint }: { label: string; value: string; hint: string }) {
  return (
    <div className="flex items-start justify-between gap-4">
      <div className="min-w-0">
        <dt className="text-sm font-medium text-slate-700">{label}</dt>
        <dd className="text-xs text-slate-500">{hint}</dd>
      </div>
      <p className="shrink-0 text-sm font-semibold tabular-nums text-slate-900">{value}</p>
    </div>
  );
}
