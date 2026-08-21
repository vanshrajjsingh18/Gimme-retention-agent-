import { useState } from 'react';
import { Link } from 'react-router-dom';

import GenerateMessagePanel from '../features/GenerateMessagePanel';
import {
  Badge,
  Card,
  EmptyState,
  LoadingState,
  PageHeader,
  TableShell,
} from '../components/ui';
import { useDebounced, useQuery } from '../hooks/useApi';
import type { CustomerSummary, Message, Page } from '../types';
import { formatCurrency, formatDateTime, formatDays, humanize } from '../utils/format';
import { LIFECYCLE_BADGE, MESSAGE_STATUS_BADGE } from '../utils/theme';

export default function MessageStudioPage() {
  const [selected, setSelected] = useState<CustomerSummary | null>(null);
  const [searchInput, setSearchInput] = useState('');
  const search = useDebounced(searchInput, 350);

  const { data: customers, loading } = useQuery<Page<CustomerSummary>>(
    `/api/v1/customers?page_size=10&sort_by=churn_score&sort_dir=desc${
      search ? `&search=${encodeURIComponent(search)}` : ''
    }`,
    [search],
  );

  const { data: llm } = useQuery<{ provider: string; model: string; message: string; mode: string }>(
    '/api/v1/messages/llm-status',
  );

  const { data: recent, refetch: refetchRecent } = useQuery<Message[]>(
    '/api/v1/messages?limit=15',
  );

  return (
    <>
      <PageHeader
        title="Message Studio"
        description="Generate grounded, personalised messages one customer at a time."
      />

      {llm && (
        <div className="mb-4 rounded-lg border border-slate-200 bg-white px-4 py-3">
          <div className="flex flex-wrap items-center gap-2">
            <Badge
              className={
                llm.mode === 'mock'
                  ? 'bg-amber-50 text-amber-800 ring-amber-200'
                  : 'bg-emerald-50 text-emerald-700 ring-emerald-200'
              }
            >
              {llm.mode === 'mock' ? 'Mock LLM' : 'Live LLM'}
            </Badge>
            <span className="text-sm text-slate-600">
              {llm.provider} · {llm.model}
            </span>
          </div>
          <p className="mt-1 text-xs text-slate-500">{llm.message}</p>
        </div>
      )}

      <div className="grid gap-4 lg:grid-cols-3">
        <Card
          title="Pick a customer"
          description="Sorted by churn risk — the customers most worth a message."
          className="lg:col-span-1"
          bodyClassName=""
        >
          <div className="px-5 py-3">
            <input
              type="search"
              className="input"
              placeholder="Search by name, email or ID"
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
              aria-label="Search customers"
            />
          </div>

          {loading ? (
            <LoadingState label="Loading…" />
          ) : !customers || customers.items.length === 0 ? (
            <EmptyState title="No customers found" description="Try a different search." />
          ) : (
            <ul className="max-h-[520px] divide-y divide-slate-100 overflow-y-auto">
              {customers.items.map((customer) => (
                <li key={customer.id}>
                  <button
                    type="button"
                    onClick={() => setSelected(customer)}
                    className={`flex w-full items-start gap-3 px-5 py-3 text-left transition-colors ${
                      selected?.id === customer.id ? 'bg-brand-50' : 'hover:bg-slate-50'
                    }`}
                  >
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-sm font-medium text-slate-800">
                        {customer.full_name || customer.external_id}
                      </p>
                      <p className="truncate text-xs text-slate-500">
                        {formatCurrency(customer.lifetime_revenue)} ·{' '}
                        {formatDays(customer.days_since_last_order)} since order
                      </p>
                      <div className="mt-1 flex flex-wrap gap-1">
                        <Badge className={LIFECYCLE_BADGE[customer.lifecycle_stage]}>
                          {humanize(customer.lifecycle_stage)}
                        </Badge>
                        <Badge className="bg-slate-100 text-slate-600 ring-slate-200">
                          {humanize(customer.recommended_action)}
                        </Badge>
                      </div>
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <div className="lg:col-span-2">
          {!selected ? (
            <Card>
              <EmptyState
                title="Select a customer to begin"
                description="The model receives only that customer's verified data plus your brand settings — it cannot invent offers, products, prices or facts about them."
              />
            </Card>
          ) : (
            <Card
              title={selected.full_name || selected.external_id}
              description={`${humanize(selected.lifecycle_stage)} · churn ${selected.churn_score.toFixed(
                0,
              )}/100 · recommended: ${humanize(selected.recommended_action)}`}
              actions={
                <Link
                  to={`/customers/${selected.id}`}
                  className="btn-secondary px-2.5 py-1 text-xs"
                >
                  Open profile
                </Link>
              }
            >
              <GenerateMessagePanel
                key={selected.id}
                customerId={selected.id}
                defaultObjective={selected.recommended_action}
                onGenerated={() => refetchRecent()}
              />
            </Card>
          )}
        </div>
      </div>

      <Card title="Recent messages" className="mt-4" bodyClassName="">
        {!recent || recent.length === 0 ? (
          <EmptyState title="No messages yet" description="Generated messages appear here." />
        ) : (
          <TableShell>
            <thead className="bg-slate-50">
              <tr>
                <th className="table-head">Subject / body</th>
                <th className="table-head">Channel</th>
                <th className="table-head">Status</th>
                <th className="table-head">Validation</th>
                <th className="table-head">Created</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {recent.map((message) => (
                <tr key={message.id} className="hover:bg-slate-50">
                  <td className="table-cell">
                    <p className="max-w-md truncate font-medium text-slate-800">
                      {message.subject || humanize(message.objective)}
                    </p>
                    <p className="max-w-md truncate text-xs text-slate-500">{message.body}</p>
                  </td>
                  <td className="table-cell">{message.channel}</td>
                  <td className="table-cell">
                    <Badge
                      className={
                        MESSAGE_STATUS_BADGE[message.status] ??
                        'bg-slate-100 text-slate-700 ring-slate-200'
                      }
                    >
                      {humanize(message.status)}
                    </Badge>
                  </td>
                  <td className="table-cell">
                    {message.validation_result?.valid ? (
                      <Badge className="bg-emerald-50 text-emerald-700 ring-emerald-200">
                        Passed
                      </Badge>
                    ) : (
                      <Badge className="bg-red-50 text-red-700 ring-red-200">
                        {message.validation_result?.errors?.length ?? 0} blocking
                      </Badge>
                    )}
                  </td>
                  <td className="table-cell text-xs text-slate-500">
                    {formatDateTime(message.created_at)}
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
