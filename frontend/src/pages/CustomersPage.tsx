import { useEffect, useMemo, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';

import {
  Badge,
  Card,
  EmptyState,
  ErrorState,
  LoadingState,
  PageHeader,
  Pagination,
  TableShell,
} from '../components/ui';
import { useDebounced, useQuery } from '../hooks/useApi';
import type { CustomerSummary, Page, Segment } from '../types';
import { formatCurrency, formatDays, formatNumber, humanize } from '../utils/format';
import { LIFECYCLE_BADGE, RISK_BADGE } from '../utils/theme';

interface FilterOptions {
  lifecycle_stages: string[];
  churn_risk_bands: string[];
  cities: string[];
  rfm_segments: string[];
  recommended_actions: string[];
  sort_fields: string[];
}

const SORT_LABELS: Record<string, string> = {
  lifetime_revenue: 'Lifetime revenue',
  churn_score: 'Churn score',
  days_since_last_order: 'Days since last order',
  total_orders: 'Total orders',
  estimated_ltv: 'Estimated LTV',
  engagement_score: 'Engagement score',
  created_at: 'Date added',
  full_name: 'Name',
};

export default function CustomersPage() {
  const [params, setParams] = useSearchParams();
  const [searchInput, setSearchInput] = useState(params.get('search') ?? '');
  const search = useDebounced(searchInput, 350);

  const page = Number(params.get('page') ?? 1);
  const stages = params.getAll('lifecycle_stage');
  const bands = params.getAll('churn_risk_band');
  const segmentId = params.get('segment_id') ?? '';
  const city = params.get('city') ?? '';
  const consent = params.get('marketing_consent') ?? '';
  const minRevenue = params.get('min_revenue') ?? '';
  const minDays = params.get('min_days_since_order') ?? '';
  const sortBy = params.get('sort_by') ?? 'lifetime_revenue';
  const sortDir = params.get('sort_dir') ?? 'desc';

  // Reset to page 1 whenever the debounced search term changes.
  useEffect(() => {
    setParams(
      (current) => {
        const next = new URLSearchParams(current);
        if (search) next.set('search', search);
        else next.delete('search');
        next.set('page', '1');
        return next;
      },
      { replace: true },
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search]);

  const query = useMemo(() => {
    const q = new URLSearchParams();
    q.set('page', String(page));
    q.set('page_size', '25');
    q.set('sort_by', sortBy);
    q.set('sort_dir', sortDir);
    if (search) q.set('search', search);
    stages.forEach((s) => q.append('lifecycle_stage', s));
    bands.forEach((b) => q.append('churn_risk_band', b));
    if (segmentId) q.set('segment_id', segmentId);
    if (city) q.set('city', city);
    if (consent) q.set('marketing_consent', consent);
    if (minRevenue) q.set('min_revenue', minRevenue);
    if (minDays) q.set('min_days_since_order', minDays);
    return q.toString();
  }, [page, sortBy, sortDir, search, stages.join(), bands.join(), segmentId, city, consent, minRevenue, minDays]);

  const { data, loading, error, refetch } = useQuery<Page<CustomerSummary>>(
    `/api/v1/customers?${query}`,
  );
  const { data: options } = useQuery<FilterOptions>('/api/v1/customers/filters');
  const { data: segments } = useQuery<Segment[]>('/api/v1/segments');

  function update(key: string, value: string | null) {
    setParams((current) => {
      const next = new URLSearchParams(current);
      if (value) next.set(key, value);
      else next.delete(key);
      next.set('page', '1');
      return next;
    });
  }

  function toggleMulti(key: string, value: string) {
    setParams((current) => {
      const next = new URLSearchParams(current);
      const existing = next.getAll(key);
      next.delete(key);
      const updated = existing.includes(value)
        ? existing.filter((v) => v !== value)
        : [...existing, value];
      updated.forEach((v) => next.append(key, v));
      next.set('page', '1');
      return next;
    });
  }

  const activeFilterCount =
    stages.length + bands.length + [segmentId, city, consent, minRevenue, minDays].filter(Boolean).length;

  return (
    <>
      <PageHeader
        title="Customers"
        description="Search, filter and open any customer's full profile."
        actions={
          activeFilterCount > 0 ? (
            <button
              type="button"
              className="btn-secondary"
              onClick={() => setParams(new URLSearchParams())}
            >
              Clear {activeFilterCount} filter{activeFilterCount === 1 ? '' : 's'}
            </button>
          ) : undefined
        }
      />

      <Card className="mb-4" bodyClassName="px-5 py-4">
        <div className="grid gap-4 lg:grid-cols-4">
          <div className="lg:col-span-2">
            <label className="label" htmlFor="customer-search">
              Search
            </label>
            <input
              id="customer-search"
              type="search"
              className="input"
              placeholder="Name, email, phone or customer ID"
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
            />
          </div>
          <div>
            <label className="label" htmlFor="segment-filter">
              Segment
            </label>
            <select
              id="segment-filter"
              className="input"
              value={segmentId}
              onChange={(e) => update('segment_id', e.target.value || null)}
            >
              <option value="">All customers</option>
              {(segments ?? []).map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name} ({formatNumber(s.member_count)})
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="label" htmlFor="sort-by">
              Sort by
            </label>
            <div className="flex gap-2">
              <select
                id="sort-by"
                className="input"
                value={sortBy}
                onChange={(e) => update('sort_by', e.target.value)}
              >
                {(options?.sort_fields ?? Object.keys(SORT_LABELS)).map((f) => (
                  <option key={f} value={f}>
                    {SORT_LABELS[f] ?? humanize(f)}
                  </option>
                ))}
              </select>
              <button
                type="button"
                className="btn-secondary shrink-0 px-3"
                onClick={() => update('sort_dir', sortDir === 'desc' ? 'asc' : 'desc')}
                title={sortDir === 'desc' ? 'Descending' : 'Ascending'}
              >
                {sortDir === 'desc' ? '↓' : '↑'}
              </button>
            </div>
          </div>
        </div>

        <div className="mt-4 space-y-3 border-t border-slate-200 pt-4">
          <FilterChips
            label="Lifecycle stage"
            values={options?.lifecycle_stages ?? []}
            selected={stages}
            onToggle={(v) => toggleMulti('lifecycle_stage', v)}
            badgeMap={LIFECYCLE_BADGE}
          />
          <FilterChips
            label="Churn risk"
            values={options?.churn_risk_bands ?? []}
            selected={bands}
            onToggle={(v) => toggleMulti('churn_risk_band', v)}
            badgeMap={RISK_BADGE}
          />
          <div className="grid gap-3 sm:grid-cols-4">
            <div>
              <label className="label" htmlFor="city-filter">
                City
              </label>
              <select
                id="city-filter"
                className="input"
                value={city}
                onChange={(e) => update('city', e.target.value || null)}
              >
                <option value="">Any city</option>
                {(options?.cities ?? []).map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="label" htmlFor="consent-filter">
                Marketing consent
              </label>
              <select
                id="consent-filter"
                className="input"
                value={consent}
                onChange={(e) => update('marketing_consent', e.target.value || null)}
              >
                <option value="">Any</option>
                <option value="true">Consented</option>
                <option value="false">Not consented</option>
              </select>
            </div>
            <div>
              <label className="label" htmlFor="min-revenue">
                Min lifetime revenue
              </label>
              <input
                id="min-revenue"
                type="number"
                min="0"
                className="input"
                placeholder="e.g. 500"
                value={minRevenue}
                onChange={(e) => update('min_revenue', e.target.value || null)}
              />
            </div>
            <div>
              <label className="label" htmlFor="min-days">
                Min days since order
              </label>
              <input
                id="min-days"
                type="number"
                min="0"
                className="input"
                placeholder="e.g. 60"
                value={minDays}
                onChange={(e) => update('min_days_since_order', e.target.value || null)}
              />
            </div>
          </div>
        </div>
      </Card>

      <Card bodyClassName="">
        {loading ? (
          <LoadingState label="Loading customers…" />
        ) : error ? (
          <div className="p-5">
            <ErrorState message={error} onRetry={refetch} />
          </div>
        ) : !data || data.items.length === 0 ? (
          <EmptyState
            title="No customers match these filters"
            description="Try widening the filters, or clear them to see the whole base."
            action={
              <button
                type="button"
                className="btn-secondary"
                onClick={() => setParams(new URLSearchParams())}
              >
                Clear filters
              </button>
            }
          />
        ) : (
          <>
            <TableShell>
              <thead className="bg-slate-50">
                <tr>
                  <th className="table-head">Customer</th>
                  <th className="table-head">Stage</th>
                  <th className="table-head text-right">Revenue</th>
                  <th className="table-head text-right">Orders</th>
                  <th className="table-head text-right">Last order</th>
                  <th className="table-head">Churn risk</th>
                  <th className="table-head">RFM</th>
                  <th className="table-head">Next best action</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100 bg-white">
                {data.items.map((customer) => (
                  <tr key={customer.id} className="hover:bg-slate-50">
                    <td className="table-cell">
                      <Link
                        to={`/customers/${customer.id}`}
                        className="font-medium text-brand-700 hover:text-brand-800"
                      >
                        {customer.full_name || customer.external_id}
                      </Link>
                      <p className="text-xs text-slate-500">{customer.email ?? customer.external_id}</p>
                      {(customer.is_suppressed || !customer.marketing_consent) && (
                        <div className="mt-1 flex gap-1">
                          {customer.is_suppressed && (
                            <Badge className="bg-slate-100 text-slate-600 ring-slate-200">
                              Suppressed
                            </Badge>
                          )}
                          {!customer.marketing_consent && (
                            <Badge className="bg-slate-100 text-slate-600 ring-slate-200">
                              No consent
                            </Badge>
                          )}
                        </div>
                      )}
                    </td>
                    <td className="table-cell">
                      <Badge className={LIFECYCLE_BADGE[customer.lifecycle_stage]}>
                        {humanize(customer.lifecycle_stage)}
                      </Badge>
                    </td>
                    <td className="table-cell text-right tabular-nums">
                      {formatCurrency(customer.lifetime_revenue)}
                    </td>
                    <td className="table-cell text-right tabular-nums">
                      {formatNumber(customer.completed_orders)}
                    </td>
                    <td className="table-cell text-right tabular-nums">
                      {formatDays(customer.days_since_last_order)}
                    </td>
                    <td className="table-cell">
                      <div className="flex items-center gap-2">
                        <Badge className={RISK_BADGE[customer.churn_risk_band]}>
                          {customer.churn_score.toFixed(0)}
                        </Badge>
                        <span className="text-xs text-slate-500">
                          {humanize(customer.churn_risk_band)}
                        </span>
                      </div>
                    </td>
                    <td className="table-cell">
                      <span className="font-mono text-xs text-slate-600">
                        {customer.rfm_cell ?? '—'}
                      </span>
                      <p className="text-xs text-slate-500">{customer.rfm_segment ?? ''}</p>
                    </td>
                    <td className="table-cell text-xs text-slate-600">
                      {humanize(customer.recommended_action)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </TableShell>
            <Pagination
              page={data.page}
              totalPages={data.total_pages}
              total={data.total}
              pageSize={data.page_size}
              onChange={(p) => update('page', String(p))}
            />
          </>
        )}
      </Card>
    </>
  );
}

function FilterChips({
  label,
  values,
  selected,
  onToggle,
  badgeMap,
}: {
  label: string;
  values: string[];
  selected: string[];
  onToggle: (value: string) => void;
  badgeMap: Record<string, string>;
}) {
  if (values.length === 0) return null;
  return (
    <div>
      <p className="label">{label}</p>
      <div className="flex flex-wrap gap-1.5">
        {values.map((value) => {
          const active = selected.includes(value);
          return (
            <button
              key={value}
              type="button"
              onClick={() => onToggle(value)}
              aria-pressed={active}
              className={`rounded-md px-2 py-0.5 text-xs font-medium ring-1 ring-inset transition-colors ${
                active
                  ? (badgeMap[value] ?? 'bg-brand-50 text-brand-700 ring-brand-300')
                  : 'bg-white text-slate-600 ring-slate-200 hover:bg-slate-50'
              }`}
            >
              {humanize(value)}
            </button>
          );
        })}
      </div>
    </div>
  );
}
