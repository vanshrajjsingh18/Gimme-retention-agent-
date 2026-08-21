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
  ProgressBar,
  StatTile,
  TableShell,
} from '../components/ui';
import { useQuery } from '../hooks/useApi';
import { axisTick, countFormatter, labelFormatter, tooltipStyle } from '../utils/charts';
import { formatCurrency, formatDays, formatNumber, formatPercent, humanize } from '../utils/format';
import { CHART_COLORS, RISK_BADGE, riskColor } from '../utils/theme';

interface ChurnAnalytics {
  risk_distribution: { band: string; count: number; revenue_at_risk: number }[];
  score_distribution: { range: string; count: number }[];
  revenue_at_risk: number;
  risk_movement: { increased: number; decreased: number; unchanged: number };
  churn_reasons: { code: string; label: string; count: number }[];
  reactivation_rate: number;
  total_reactivations: number;
  top_at_risk_customers: {
    id: number;
    full_name: string;
    lifecycle_stage: string;
    churn_score: number;
    risk_band: string;
    explanation: string;
    lifetime_revenue: number;
    days_since_last_order: number | null;
  }[];
}

export default function ChurnAnalyticsPage() {
  const { data, loading, error, refetch } = useQuery<ChurnAnalytics>('/api/v1/analytics/churn');

  if (loading) return <LoadingState label="Loading churn analytics…" />;
  if (error) return <ErrorState message={error} onRetry={refetch} />;
  if (!data) return null;

  const totalScored = data.risk_distribution.reduce((sum, b) => sum + b.count, 0);
  const atRisk = data.risk_distribution
    .filter((b) => b.band === 'HIGH' || b.band === 'CRITICAL')
    .reduce((sum, b) => sum + b.count, 0);
  const maxReason = Math.max(1, ...data.churn_reasons.map((r) => r.count));

  return (
    <>
      <PageHeader
        title="Churn analytics"
        description="Every score is the sum of named, weighted factors computed in code — no black box."
      />

      <section className="mb-4 grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatTile
          label="High + critical risk"
          value={formatNumber(atRisk)}
          tone="critical"
          sublabel={`${formatPercent(totalScored ? atRisk / totalScored : 0)} of scored customers`}
        />
        <StatTile
          label="Revenue at risk"
          value={formatCurrency(data.revenue_at_risk)}
          tone="critical"
          sublabel="lifetime revenue of high + critical customers"
        />
        <StatTile
          label="Reactivations"
          value={formatNumber(data.total_reactivations)}
          tone="positive"
          sublabel={`${formatPercent(data.reactivation_rate)} of lapsed customers`}
        />
        <StatTile
          label="Risk increased"
          value={formatNumber(data.risk_movement.increased)}
          tone="warning"
          sublabel={`${formatNumber(data.risk_movement.decreased)} improved since last run`}
        />
      </section>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Risk distribution" description="Customers and exposure per band.">
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={data.risk_distribution} margin={{ top: 8, right: 8, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" vertical={false} />
                <XAxis dataKey="band" tick={axisTick} />
                <YAxis tick={axisTick} allowDecimals={false} />
                <Tooltip contentStyle={tooltipStyle} formatter={countFormatter('Customers')} />
                <Bar dataKey="count" radius={[4, 4, 0, 0]}>
                  {data.risk_distribution.map((entry) => (
                    <Cell key={entry.band} fill={riskColor(entry.band)} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
          <ul className="mt-3 space-y-2 border-t border-slate-100 pt-3">
            {data.risk_distribution.map((band) => (
              <li key={band.band} className="flex items-center justify-between gap-3 text-sm">
                <div className="flex items-center gap-2">
                  <Badge className={RISK_BADGE[band.band as keyof typeof RISK_BADGE]}>
                    {humanize(band.band)}
                  </Badge>
                  <span className="text-slate-600">{formatNumber(band.count)} customers</span>
                </div>
                <span className="tabular-nums text-slate-500">
                  {formatCurrency(band.revenue_at_risk)} at risk
                </span>
              </li>
            ))}
          </ul>
        </Card>

        <Card title="Score distribution" description="How churn scores spread across 0-100.">
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={data.score_distribution} margin={{ top: 8, right: 8, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" vertical={false} />
                <XAxis dataKey="range" tick={axisTick} />
                <YAxis tick={axisTick} allowDecimals={false} />
                <Tooltip
                  contentStyle={tooltipStyle}
                  formatter={countFormatter('Customers')}
                  labelFormatter={labelFormatter((l) => `Score ${l}`)}
                />
                <Bar dataKey="count" radius={[4, 4, 0, 0]}>
                  {data.score_distribution.map((entry, index) => (
                    <Cell
                      key={entry.range}
                      fill={['#0f9d58', '#d9a300', '#e8710a', '#d93025'][index] ?? CHART_COLORS[0]}
                    />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Card>

        <Card
          title="Why customers are at risk"
          description="The top contributing factor for each scored customer."
        >
          {data.churn_reasons.length === 0 ? (
            <EmptyState title="No risk factors recorded" />
          ) : (
            <ul className="space-y-3">
              {data.churn_reasons.map((reason) => (
                <li key={reason.code}>
                  <div className="mb-1 flex items-baseline justify-between gap-3">
                    <span className="text-sm text-slate-700">{reason.label}</span>
                    <span className="shrink-0 text-xs tabular-nums text-slate-500">
                      {formatNumber(reason.count)}
                    </span>
                  </div>
                  <ProgressBar value={(reason.count / maxReason) * 100} />
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card
          title="Risk movement"
          description="How scores changed at the last recalculation."
        >
          <dl className="grid grid-cols-3 gap-3 text-center">
            <div className="rounded-lg bg-red-50 py-4">
              <p className="text-2xl font-semibold tabular-nums text-red-700">
                {formatNumber(data.risk_movement.increased)}
              </p>
              <p className="text-xs text-red-600">Got riskier</p>
            </div>
            <div className="rounded-lg bg-emerald-50 py-4">
              <p className="text-2xl font-semibold tabular-nums text-emerald-700">
                {formatNumber(data.risk_movement.decreased)}
              </p>
              <p className="text-xs text-emerald-600">Improved</p>
            </div>
            <div className="rounded-lg bg-slate-100 py-4">
              <p className="text-2xl font-semibold tabular-nums text-slate-700">
                {formatNumber(data.risk_movement.unchanged)}
              </p>
              <p className="text-xs text-slate-500">Unchanged</p>
            </div>
          </dl>
          <p className="mt-4 text-xs text-slate-500">
            Movement is measured against the previous stored score, so it only becomes meaningful
            after intelligence has been recalculated at least twice.
          </p>
        </Card>
      </div>

      <Card
        title="Priority save list"
        description="Highest-value customers at high or critical risk."
        className="mt-4"
        bodyClassName=""
      >
        {data.top_at_risk_customers.length === 0 ? (
          <EmptyState
            title="Nobody at high risk"
            description="No customer currently scores above the high-risk threshold."
          />
        ) : (
          <TableShell>
            <thead className="bg-slate-50">
              <tr>
                <th className="table-head">Customer</th>
                <th className="table-head">Stage</th>
                <th className="table-head text-right">Revenue</th>
                <th className="table-head text-right">Last order</th>
                <th className="table-head">Risk</th>
                <th className="table-head">Why</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {data.top_at_risk_customers.map((customer) => (
                <tr key={customer.id} className="hover:bg-slate-50">
                  <td className="table-cell">
                    <Link
                      to={`/customers/${customer.id}`}
                      className="font-medium text-brand-700 hover:text-brand-800"
                    >
                      {customer.full_name}
                    </Link>
                  </td>
                  <td className="table-cell text-xs">{humanize(customer.lifecycle_stage)}</td>
                  <td className="table-cell text-right tabular-nums">
                    {formatCurrency(customer.lifetime_revenue)}
                  </td>
                  <td className="table-cell text-right tabular-nums">
                    {formatDays(customer.days_since_last_order)}
                  </td>
                  <td className="table-cell">
                    <Badge className={RISK_BADGE[customer.risk_band as keyof typeof RISK_BADGE]}>
                      {customer.churn_score.toFixed(0)}
                    </Badge>
                  </td>
                  <td className="table-cell">
                    <p className="max-w-md whitespace-normal text-xs text-slate-600">
                      {customer.explanation}
                    </p>
                  </td>
                </tr>
              ))}
            </tbody>
          </TableShell>
        )}
      </Card>
    </>
  );
}
