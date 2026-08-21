import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

import { Card, ErrorState, LoadingState, PageHeader } from '../components/ui';
import { useQuery } from '../hooks/useApi';
import {
  axisTick,
  countFormatter,
  humanizeLabel,
  labelFormatter,
  namedCountFormatter,
  smallAxisTick,
  tooltipStyle,
} from '../utils/charts';
import { CHART_COLORS, lifecycleColor } from '../utils/theme';

interface CustomerAnalytics {
  customer_growth: { month: string; new_customers: number }[];
  new_vs_repeat: { month: string; new: number; repeat: number; revenue: number }[];
  lifecycle_distribution: { stage: string; count: number }[];
  rfm_distribution: { segment: string; count: number; revenue: number }[];
  rfm_grid: { recency: number; frequency: number; count: number }[];
  purchase_frequency: { orders: number; customers: number }[];
  ltv_distribution: { range: string; min: number; max: number | null; count: number }[];
  revenue_distribution: { range: string; min: number; max: number | null; count: number }[];
}

export default function CustomerAnalyticsPage() {
  const { data, loading, error, refetch } = useQuery<CustomerAnalytics>(
    '/api/v1/analytics/customers',
  );

  if (loading) return <LoadingState label="Loading customer analytics…" />;
  if (error) return <ErrorState message={error} onRetry={refetch} />;
  if (!data) return null;

  const maxGridCount = Math.max(1, ...data.rfm_grid.map((c) => c.count));

  return (
    <>
      <PageHeader
        title="Customer analytics"
        description="Growth, mix and value distribution across your customer base."
      />

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Customer growth" description="New signups per month.">
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={data.customer_growth} margin={{ top: 8, right: 8, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" vertical={false} />
                <XAxis dataKey="month" tick={axisTick} />
                <YAxis tick={axisTick} allowDecimals={false} />
                <Tooltip contentStyle={tooltipStyle} formatter={countFormatter('New customers')} />
                <Line
                  type="monotone"
                  dataKey="new_customers"
                  stroke={CHART_COLORS[0]}
                  strokeWidth={2}
                  dot={{ r: 3 }}
                />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </Card>

        <Card
          title="New vs repeat orders"
          description="A repeat order is one from a customer who had ordered before."
        >
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={data.new_vs_repeat} margin={{ top: 8, right: 8, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" vertical={false} />
                <XAxis dataKey="month" tick={axisTick} />
                <YAxis tick={axisTick} allowDecimals={false} />
                <Tooltip contentStyle={tooltipStyle} formatter={namedCountFormatter()} />
                <Legend wrapperStyle={{ fontSize: 12 }} />
                <Bar dataKey="new" name="New" stackId="a" fill={CHART_COLORS[0]} />
                <Bar
                  dataKey="repeat"
                  name="Repeat"
                  stackId="a"
                  fill={CHART_COLORS[1]}
                  radius={[4, 4, 0, 0]}
                />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Card>

        <Card title="Lifecycle distribution">
          <div className="h-72">
            <ResponsiveContainer width="100%" height="100%">
              <PieChart>
                <Pie
                  data={data.lifecycle_distribution}
                  dataKey="count"
                  nameKey="stage"
                  innerRadius={55}
                  outerRadius={95}
                  paddingAngle={1}
                >
                  {data.lifecycle_distribution.map((entry) => (
                    <Cell key={entry.stage} fill={lifecycleColor(entry.stage)} />
                  ))}
                </Pie>
                <Tooltip contentStyle={tooltipStyle} formatter={namedCountFormatter()} />
                <Legend wrapperStyle={{ fontSize: 11 }} formatter={humanizeLabel} />
              </PieChart>
            </ResponsiveContainer>
          </div>
        </Card>

        <Card title="RFM segments" description="Customers and revenue by named RFM segment.">
          <div className="h-72">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart
                data={data.rfm_distribution}
                layout="vertical"
                margin={{ top: 8, right: 16, left: 8 }}
              >
                <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" horizontal={false} />
                <XAxis type="number" tick={axisTick} allowDecimals={false} />
                <YAxis
                  type="category"
                  dataKey="segment"
                  tick={smallAxisTick}
                  width={110}
                />
                <Tooltip contentStyle={tooltipStyle} formatter={countFormatter('Customers')} />
                <Bar dataKey="count" fill={CHART_COLORS[2]} radius={[0, 4, 4, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Card>

        <Card
          title="RFM grid"
          description="Recency against frequency. Darker cells hold more customers."
        >
          <div className="overflow-x-auto">
            <table className="border-separate border-spacing-1">
              <thead>
                <tr>
                  <th className="w-16" />
                  {[1, 2, 3, 4, 5].map((f) => (
                    <th key={f} className="w-14 pb-1 text-center text-xs text-slate-500">
                      F{f}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {[5, 4, 3, 2, 1].map((r) => (
                  <tr key={r}>
                    <th className="pr-1 text-right text-xs font-medium text-slate-500">R{r}</th>
                    {[1, 2, 3, 4, 5].map((f) => {
                      const cell = data.rfm_grid.find(
                        (c) => c.recency === r && c.frequency === f,
                      );
                      const count = cell?.count ?? 0;
                      const intensity = count / maxGridCount;
                      return (
                        <td key={f}>
                          <div
                            className="flex h-11 items-center justify-center rounded text-xs font-medium tabular-nums"
                            style={{
                              backgroundColor:
                                count === 0
                                  ? '#f1f5f9'
                                  : `rgba(27, 110, 245, ${0.12 + intensity * 0.78})`,
                              color: intensity > 0.5 ? '#fff' : '#334155',
                            }}
                            title={`Recency ${r}, Frequency ${f}: ${count} customers`}
                          >
                            {count || ''}
                          </div>
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>

        <Card
          title="Purchase frequency"
          description="How many customers have placed each number of orders."
        >
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={data.purchase_frequency} margin={{ top: 8, right: 8, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" vertical={false} />
                <XAxis dataKey="orders" tick={axisTick} />
                <YAxis tick={axisTick} allowDecimals={false} />
                <Tooltip
                  contentStyle={tooltipStyle}
                  formatter={countFormatter('Customers')}
                  labelFormatter={labelFormatter((l) => `${l} order${l === '1' ? '' : 's'}`)}
                />
                <Bar dataKey="customers" fill={CHART_COLORS[4]} radius={[4, 4, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Card>

        <Card title="Lifetime revenue distribution">
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={data.revenue_distribution} margin={{ top: 8, right: 8, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" vertical={false} />
                <XAxis dataKey="range" tick={smallAxisTick} />
                <YAxis tick={axisTick} allowDecimals={false} />
                <Tooltip contentStyle={tooltipStyle} formatter={countFormatter('Customers')} />
                <Bar dataKey="count" fill={CHART_COLORS[1]} radius={[4, 4, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Card>

        <Card title="Estimated LTV distribution">
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={data.ltv_distribution} margin={{ top: 8, right: 8, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" vertical={false} />
                <XAxis dataKey="range" tick={smallAxisTick} />
                <YAxis tick={axisTick} allowDecimals={false} />
                <Tooltip contentStyle={tooltipStyle} formatter={countFormatter('Customers')} />
                <Bar dataKey="count" fill={CHART_COLORS[2]} radius={[4, 4, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Card>
      </div>
    </>
  );
}
