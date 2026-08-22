import { useState } from 'react';
import { Link } from 'react-router-dom';

import { api } from '../api/client';
import AutomationForm from '../features/AutomationForm';
import {
  Badge,
  Card,
  EmptyState,
  ErrorState,
  LoadingState,
  Modal,
  PageHeader,
  TableShell,
  notify,
} from '../components/ui';
import { useQuery } from '../hooks/useApi';
import type { Automation, AutomationKind } from '../types';
import { formatBusinessTime, formatNumber } from '../utils/format';
import { AUTOMATION_KIND_LABEL, AUTOMATION_STATUS_BADGE } from '../utils/theme';

const KINDS: { key: AutomationKind | 'ALL'; label: string; blurb: string }[] = [
  { key: 'ALL', label: 'All', blurb: '' },
  {
    key: 'COHORT_BULK',
    label: 'Cohort sends',
    blurb: 'One-off or recurring sends to whoever matches a segment at send time.',
  },
  {
    key: 'SEQUENCE',
    label: 'Sequences',
    blurb: 'A series of steps timed from each customer’s own enrollment.',
  },
  {
    key: 'NUDGE',
    label: 'Behavioural nudges',
    blurb: 'A standing message at the day and time each customer usually orders.',
  },
];

export default function AutomationsPage() {
  const [kind, setKind] = useState<AutomationKind | 'ALL'>('ALL');
  const [creating, setCreating] = useState<AutomationKind | null>(null);

  const path = kind === 'ALL' ? '/api/v1/automations' : `/api/v1/automations?kind=${kind}`;
  const { data, loading, error, refetch } = useQuery<Automation[]>(path, [kind]);

  const active = KINDS.find((entry) => entry.key === kind);

  return (
    <>
      <PageHeader
        title="Automations"
        description="Recurring texting built on the existing TNZ integration. Every send re-checks consent, respects NZ business hours, and never doubles up with another automation on the same day."
        actions={
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              className="btn-secondary"
              onClick={() => setCreating('NUDGE')}
            >
              New nudge
            </button>
            <button
              type="button"
              className="btn-secondary"
              onClick={() => setCreating('SEQUENCE')}
            >
              New sequence
            </button>
            <button type="button" className="btn-primary" onClick={() => setCreating('COHORT_BULK')}>
              New cohort send
            </button>
          </div>
        }
      />

      <div className="mb-4 flex flex-wrap gap-2">
        {KINDS.map((entry) => (
          <button
            key={entry.key}
            type="button"
            onClick={() => setKind(entry.key)}
            className={
              kind === entry.key
                ? 'rounded-full bg-brand-600 px-3 py-1.5 text-sm font-medium text-white'
                : 'rounded-full bg-white px-3 py-1.5 text-sm font-medium text-slate-600 ring-1 ring-slate-200 hover:bg-slate-50'
            }
          >
            {entry.label}
          </button>
        ))}
      </div>
      {active?.blurb && <p className="mb-4 text-sm text-slate-500">{active.blurb}</p>}

      {loading ? (
        <LoadingState label="Loading automations…" />
      ) : error ? (
        <ErrorState message={error} onRetry={refetch} />
      ) : !data || data.length === 0 ? (
        <Card>
          <EmptyState
            title="No automations yet"
            description="A cohort send is the simplest place to start: pick a segment, preview exactly who would receive it, then approve."
            action={
              <button
                type="button"
                className="btn-primary"
                onClick={() => setCreating('COHORT_BULK')}
              >
                New cohort send
              </button>
            }
          />
        </Card>
      ) : (
        <Card bodyClassName="">
          <TableShell>
            <thead className="bg-slate-50">
              <tr>
                <th className="table-head">Automation</th>
                <th className="table-head">Type</th>
                <th className="table-head">Status</th>
                <th className="table-head text-right">Sent</th>
                <th className="table-head text-right">Skipped</th>
                <th className="table-head">Next run</th>
                <th className="table-head" />
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {data.map((automation) => (
                <tr key={automation.id} className="hover:bg-slate-50">
                  <td className="table-cell">
                    <Link
                      to={`/automations/${automation.id}`}
                      className="font-medium text-brand-700 hover:underline"
                    >
                      {automation.name}
                    </Link>
                    {automation.description && (
                      <p className="mt-0.5 max-w-md text-xs text-slate-500">
                        {automation.description}
                      </p>
                    )}
                  </td>
                  <td className="table-cell text-sm text-slate-600">
                    {AUTOMATION_KIND_LABEL[automation.kind] ?? automation.kind}
                  </td>
                  <td className="table-cell">
                    <Badge className={AUTOMATION_STATUS_BADGE[automation.status]}>
                      {automation.status}
                    </Badge>
                    {automation.require_approval && !automation.approved_at && (
                      <p className="mt-1 text-xs text-amber-700">Needs approval</p>
                    )}
                  </td>
                  <td className="table-cell text-right tabular-nums">
                    {formatNumber(automation.total_sent)}
                  </td>
                  <td className="table-cell text-right tabular-nums text-slate-500">
                    {formatNumber(automation.total_skipped)}
                  </td>
                  <td className="table-cell text-sm text-slate-600">
                    {automation.next_run_at ? formatBusinessTime(automation.next_run_at) : '—'}
                  </td>
                  <td className="table-cell text-right">
                    <Link
                      to={`/automations/${automation.id}`}
                      className="text-sm font-medium text-brand-700 hover:underline"
                    >
                      Open
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </TableShell>
        </Card>
      )}

      {creating && (
        <Modal
          open
          size="lg"
          title={`New ${AUTOMATION_KIND_LABEL[creating].toLowerCase()}`}
          description="Created as a draft. Preview it before approving — nothing sends until you activate it."
          onClose={() => setCreating(null)}
        >
          <AutomationForm
            kind={creating}
            onCancel={() => setCreating(null)}
            onCreated={(automation) => {
              setCreating(null);
              notify(`Created '${automation.name}'. Preview it before approving.`);
              refetch();
            }}
            submit={(payload) => api.post<Automation>('/api/v1/automations', payload)}
          />
        </Modal>
      )}
    </>
  );
}
