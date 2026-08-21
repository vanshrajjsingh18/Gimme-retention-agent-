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
  TableShell,
} from '../components/ui';
import { useQuery } from '../hooks/useApi';
import {
  axisTick,
  countFormatter,
  currencyFormatter,
  humanizeLabel,
  smallAxisTick,
  tooltipStyle,
} from '../utils/charts';
import { formatCurrency, formatDateTime, formatNumber, formatPercent, humanize } from '../utils/format';
import { CAMPAIGN_STATUS_BADGE, CHART_COLORS } from '../utils/theme';

interface CampaignAnalytics {
  totals: {
    campaigns: number;
    messages_sent: number;
    messages_delivered: number;
    messages_opened: number;
    messages_clicked: number;
    messages_replied: number;
    messages_failed: number;
    unsubscribes: number;
    conversions: number;
    attributed_revenue: number;
    delivery_rate: number;
    open_rate: number;
    click_rate: number;
    reply_rate: number;
    conversion_rate: number;
    unsubscribe_rate: number;
    revenue_per_message: number;
  };
  by_channel: {
    channel: string;
    campaigns: number;
    messages_sent: number;
    delivery_rate: number;
    open_rate: number;
    click_rate: number;
    conversions: number;
    attributed_revenue: number;
    revenue_per_message: number;
  }[];
  by_objective: {
    objective: string;
    campaigns: number;
    messages_sent: number;
    conversions: number;
    attributed_revenue: number;
    conversion_rate: number;
  }[];
  campaigns: {
    id: number;
    name: string;
    objective: string;
    channel: string;
    status: string;
    started_at: string | null;
    total_recipients: number;
    messages_sent: number;
    messages_delivered: number;
    messages_opened: number;
    messages_clicked: number;
    conversions: number;
    attributed_revenue: number;
    delivery_rate: number;
    open_rate: number;
    click_rate: number;
    conversion_rate: number;
    revenue_per_message: number;
    unsubscribe_rate: number;
  }[];
}

export default function CampaignAnalyticsPage() {
  const { data, loading, error, refetch } = useQuery<CampaignAnalytics>(
    '/api/v1/analytics/campaigns',
  );

  if (loading) return <LoadingState label="Loading campaign analytics…" />;
  if (error) return <ErrorState message={error} onRetry={refetch} />;
  if (!data) return null;

  const { totals } = data;
  const sentCampaigns = data.campaigns.filter((c) => c.messages_sent > 0);

  if (totals.messages_sent === 0) {
    return (
      <>
        <PageHeader title="Campaign analytics" />
        <Card>
          <EmptyState
            title="No campaigns have sent yet"
            description="Run a campaign to see delivery, engagement, conversion and revenue figures here."
            action={
              <Link to="/campaigns" className="btn-primary">
                Go to campaigns
              </Link>
            }
          />
        </Card>
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Campaign analytics"
        description="Delivery, engagement and attributed revenue across every campaign."
      />

      <section className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatTile
          label="Messages sent"
          value={formatNumber(totals.messages_sent)}
          sublabel={`${formatNumber(totals.campaigns)} campaigns`}
        />
        <StatTile
          label="Open rate"
          value={formatPercent(totals.open_rate)}
          sublabel={`${formatNumber(totals.messages_opened)} opens`}
        />
        <StatTile
          label="Conversion rate"
          value={formatPercent(totals.conversion_rate)}
          tone="positive"
          sublabel={`${formatNumber(totals.conversions)} conversions`}
        />
        <StatTile
          label="Attributed revenue"
          value={formatCurrency(totals.attributed_revenue)}
          tone="positive"
          sublabel={`${formatCurrency(totals.revenue_per_message, true)} per message`}
        />
      </section>

      <section className="mt-4 grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatTile label="Delivery rate" value={formatPercent(totals.delivery_rate)} />
        <StatTile label="Click rate" value={formatPercent(totals.click_rate)} />
        <StatTile
          label="Unsubscribe rate"
          value={formatPercent(totals.unsubscribe_rate, 2)}
          tone={totals.unsubscribe_rate > 0.005 ? 'warning' : 'default'}
          sublabel={`${formatNumber(totals.unsubscribes)} opt-outs`}
        />
        <StatTile
          label="Failed"
          value={formatNumber(totals.messages_failed)}
          tone={totals.messages_failed > 0 ? 'warning' : 'default'}
        />
      </section>

      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <Card title="Performance by channel">
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={data.by_channel} margin={{ top: 8, right: 8, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" vertical={false} />
                <XAxis dataKey="channel" tick={axisTick} />
                <YAxis tick={axisTick} allowDecimals={false} />
                <Tooltip contentStyle={tooltipStyle} formatter={countFormatter('Messages sent')} />
                <Bar dataKey="messages_sent" radius={[4, 4, 0, 0]}>
                  {data.by_channel.map((entry, index) => (
                    <Cell key={entry.channel} fill={CHART_COLORS[index % CHART_COLORS.length]} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
          <div className="mt-3 overflow-x-auto border-t border-slate-100 pt-3">
            <table className="min-w-full text-xs">
              <thead>
                <tr className="text-slate-500">
                  <th className="py-1 text-left font-medium">Channel</th>
                  <th className="py-1 text-right font-medium">Sent</th>
                  <th className="py-1 text-right font-medium">Open</th>
                  <th className="py-1 text-right font-medium">Click</th>
                  <th className="py-1 text-right font-medium">Rev/msg</th>
                </tr>
              </thead>
              <tbody className="text-slate-700">
                {data.by_channel.map((c) => (
                  <tr key={c.channel}>
                    <td className="py-1">{c.channel}</td>
                    <td className="py-1 text-right tabular-nums">
                      {formatNumber(c.messages_sent)}
                    </td>
                    <td className="py-1 text-right tabular-nums">{formatPercent(c.open_rate)}</td>
                    <td className="py-1 text-right tabular-nums">{formatPercent(c.click_rate)}</td>
                    <td className="py-1 text-right tabular-nums">
                      {formatCurrency(c.revenue_per_message, true)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>

        <Card title="Revenue by objective">
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart
                data={data.by_objective}
                layout="vertical"
                margin={{ top: 8, right: 16, left: 8 }}
              >
                <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" horizontal={false} />
                <XAxis type="number" tick={axisTick} />
                <YAxis
                  type="category"
                  dataKey="objective"
                  tick={smallAxisTick}
                  width={110}
                  tickFormatter={humanizeLabel}
                />
                <Tooltip
                  contentStyle={tooltipStyle}
                  formatter={currencyFormatter('Attributed revenue')}
                  labelFormatter={humanizeLabel}
                />
                <Bar dataKey="attributed_revenue" fill={CHART_COLORS[1]} radius={[0, 4, 4, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Card>
      </div>

      <Card title="Campaign performance" className="mt-4" bodyClassName="">
        <TableShell>
          <thead className="bg-slate-50">
            <tr>
              <th className="table-head">Campaign</th>
              <th className="table-head">Status</th>
              <th className="table-head text-right">Sent</th>
              <th className="table-head text-right">Delivered</th>
              <th className="table-head text-right">Opened</th>
              <th className="table-head text-right">Clicked</th>
              <th className="table-head text-right">Conv.</th>
              <th className="table-head text-right">Revenue</th>
              <th className="table-head text-right">Rev/msg</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {sentCampaigns.map((c) => (
              <tr key={c.id} className="hover:bg-slate-50">
                <td className="table-cell">
                  <Link
                    to={`/campaigns/${c.id}`}
                    className="font-medium text-brand-700 hover:text-brand-800"
                  >
                    {c.name}
                  </Link>
                  <p className="text-xs text-slate-500">
                    {humanize(c.objective)} · {c.channel} · {formatDateTime(c.started_at)}
                  </p>
                </td>
                <td className="table-cell">
                  <Badge
                    className={
                      CAMPAIGN_STATUS_BADGE[c.status] ?? 'bg-slate-100 text-slate-700 ring-slate-200'
                    }
                  >
                    {humanize(c.status)}
                  </Badge>
                </td>
                <td className="table-cell text-right tabular-nums">
                  {formatNumber(c.messages_sent)}
                </td>
                <td className="table-cell text-right tabular-nums">
                  {formatPercent(c.delivery_rate)}
                </td>
                <td className="table-cell text-right tabular-nums">{formatPercent(c.open_rate)}</td>
                <td className="table-cell text-right tabular-nums">{formatPercent(c.click_rate)}</td>
                <td className="table-cell text-right tabular-nums">{formatNumber(c.conversions)}</td>
                <td className="table-cell text-right tabular-nums font-medium">
                  {formatCurrency(c.attributed_revenue)}
                </td>
                <td className="table-cell text-right tabular-nums">
                  {formatCurrency(c.revenue_per_message, true)}
                </td>
              </tr>
            ))}
          </tbody>
        </TableShell>
      </Card>
    </>
  );
}
