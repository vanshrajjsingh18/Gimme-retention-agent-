import { Card, EmptyState, ErrorState, LoadingState, PageHeader } from '../components/ui';
import { useQuery } from '../hooks/useApi';
import { formatNumber, formatPercent } from '../utils/format';

interface CohortAnalytics {
  cohorts: {
    cohort: string;
    size: number;
    months: { month: number; customers: number; rate: number }[];
  }[];
  max_months: number;
}

/** Blue ramp: stronger colour means a higher share of the cohort still ordering. */
function cellStyle(rate: number): React.CSSProperties {
  if (rate <= 0) return { backgroundColor: '#f8fafc', color: '#94a3b8' };
  const alpha = 0.12 + Math.min(rate, 1) * 0.8;
  return {
    backgroundColor: `rgba(27, 110, 245, ${alpha})`,
    color: alpha > 0.55 ? '#ffffff' : '#1e293b',
  };
}

export default function CohortsPage() {
  const { data, loading, error, refetch } = useQuery<CohortAnalytics>('/api/v1/analytics/cohorts');

  if (loading) return <LoadingState label="Building cohorts…" />;
  if (error) return <ErrorState message={error} onRetry={refetch} />;
  if (!data) return null;

  const months = Array.from({ length: data.max_months + 1 }, (_, i) => i);

  // Weighted average retention per month across every cohort that has reached it.
  const averages = months.map((month) => {
    const eligible = data.cohorts.filter((c) => c.months.some((m) => m.month === month));
    const size = eligible.reduce((sum, c) => sum + c.size, 0);
    const retained = eligible.reduce(
      (sum, c) => sum + (c.months.find((m) => m.month === month)?.customers ?? 0),
      0,
    );
    return { month, rate: size ? retained / size : 0, cohorts: eligible.length };
  });

  return (
    <>
      <PageHeader
        title="Cohort retention"
        description="Customers grouped by the month of their first order. A customer counts as retained in month N if they placed an order that month."
      />

      {data.cohorts.length === 0 ? (
        <Card>
          <EmptyState
            title="Not enough order history"
            description="Cohorts appear once customers have placed their first orders."
          />
        </Card>
      ) : (
        <>
          <Card title="Retention heatmap" bodyClassName="px-5 py-4 overflow-x-auto">
            <table className="min-w-full border-separate border-spacing-1">
              <thead>
                <tr>
                  <th className="px-2 py-1 text-left text-xs font-semibold text-slate-500">
                    Cohort
                  </th>
                  <th className="px-2 py-1 text-right text-xs font-semibold text-slate-500">
                    Size
                  </th>
                  {months.map((month) => (
                    <th
                      key={month}
                      className="min-w-[56px] px-2 py-1 text-center text-xs font-semibold text-slate-500"
                    >
                      M{month}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {data.cohorts.map((cohort) => (
                  <tr key={cohort.cohort}>
                    <td className="whitespace-nowrap px-2 py-1 text-xs font-medium text-slate-700">
                      {cohort.cohort}
                    </td>
                    <td className="px-2 py-1 text-right text-xs tabular-nums text-slate-600">
                      {formatNumber(cohort.size)}
                    </td>
                    {months.map((month) => {
                      const cell = cohort.months.find((m) => m.month === month);
                      if (!cell) {
                        return (
                          <td key={month}>
                            <div className="h-9 rounded bg-slate-50" />
                          </td>
                        );
                      }
                      return (
                        <td key={month}>
                          <div
                            className="flex h-9 items-center justify-center rounded text-xs font-medium tabular-nums"
                            style={cellStyle(cell.rate)}
                            title={`${cohort.cohort} month ${month}: ${cell.customers} of ${cohort.size} customers ordered`}
                          >
                            {formatPercent(cell.rate, 0)}
                          </div>
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
              <tfoot>
                <tr>
                  <td className="px-2 pt-2 text-xs font-semibold text-slate-600">Average</td>
                  <td />
                  {averages.map((avg) => (
                    <td key={avg.month} className="pt-2">
                      <div
                        className="flex h-9 items-center justify-center rounded border border-slate-200 text-xs font-semibold tabular-nums text-slate-700"
                        title={`Weighted across ${avg.cohorts} cohort${
                          avg.cohorts === 1 ? '' : 's'
                        }`}
                      >
                        {formatPercent(avg.rate, 0)}
                      </div>
                    </td>
                  ))}
                </tr>
              </tfoot>
            </table>

            <p className="mt-4 text-xs text-slate-500">
              Blank cells are months a cohort has not reached yet. Month 0 is always 100% by
              definition — it is the month the customer first ordered.
            </p>
          </Card>

          <Card title="How to read this" className="mt-4">
            <ul className="space-y-2 text-sm text-slate-600">
              <li>
                <span className="font-medium text-slate-800">Rows</span> are acquisition cohorts —
                everyone whose first order fell in that month.
              </li>
              <li>
                <span className="font-medium text-slate-800">Columns</span> are months since that
                first order, not calendar months.
              </li>
              <li>
                A healthy business shows the month-1 drop flattening out by month 3. A steadily
                falling average row means the retention problem is getting worse over time, not
                just that one cohort was weak.
              </li>
              <li>
                Compare a recent cohort against an older one at the same month number — that
                controls for cohorts having had different amounts of time to return.
              </li>
            </ul>
          </Card>
        </>
      )}
    </>
  );
}
