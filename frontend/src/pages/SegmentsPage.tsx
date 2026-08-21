import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';

import { api, downloadFile } from '../api/client';
import RuleBuilder, { emptyGroup } from '../features/RuleBuilder';
import {
  Badge,
  Card,
  EmptyState,
  ErrorState,
  LoadingState,
  Modal,
  PageHeader,
  SectionTitle,
  Spinner,
  TableShell,
  notify,
} from '../components/ui';
import { useMutation, useQuery } from '../hooks/useApi';
import type { FieldDefinition, RuleNode, Segment, SegmentPreview } from '../types';
import { formatCurrency, formatDateTime, formatDays, formatNumber, formatPercent } from '../utils/format';
import { LIFECYCLE_BADGE, RISK_BADGE } from '../utils/theme';

export default function SegmentsPage() {
  const [editing, setEditing] = useState<Segment | 'new' | null>(null);
  const { data: segments, loading, error, refetch } = useQuery<Segment[]>('/api/v1/segments');

  const refreshAll = useMutation(async () => api.post('/api/v1/segments/refresh-all'));

  return (
    <>
      <PageHeader
        title="Segments"
        description="Dynamic segments re-evaluate whenever customer data changes."
        actions={
          <>
            <button
              type="button"
              className="btn-secondary"
              onClick={async () => {
                const result = await refreshAll.run();
                if (result) {
                  notify('All segments re-evaluated.');
                  refetch();
                }
              }}
              disabled={refreshAll.loading}
            >
              {refreshAll.loading && <Spinner className="h-4 w-4" />}
              Re-evaluate all
            </button>
            <button type="button" className="btn-primary" onClick={() => setEditing('new')}>
              New segment
            </button>
          </>
        }
      />

      {loading ? (
        <LoadingState label="Loading segments…" />
      ) : error ? (
        <ErrorState message={error} onRetry={refetch} />
      ) : !segments || segments.length === 0 ? (
        <Card>
          <EmptyState
            title="No segments yet"
            description="Create a segment to target a specific group of customers."
            action={
              <button type="button" className="btn-primary" onClick={() => setEditing('new')}>
                New segment
              </button>
            }
          />
        </Card>
      ) : (
        <Card bodyClassName="">
          <TableShell>
            <thead className="bg-slate-50">
              <tr>
                <th className="table-head">Segment</th>
                <th className="table-head">Rule</th>
                <th className="table-head text-right">Members</th>
                <th className="table-head">Last evaluated</th>
                <th className="table-head" />
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {segments.map((segment) => (
                <tr key={segment.id} className="hover:bg-slate-50">
                  <td className="table-cell">
                    <div className="flex items-center gap-2">
                      <span className="font-medium text-slate-900">{segment.name}</span>
                      {segment.is_system && (
                        <Badge className="bg-slate-100 text-slate-600 ring-slate-200">
                          Built-in
                        </Badge>
                      )}
                      {segment.segment_type === 'MANUAL' && (
                        <Badge className="bg-indigo-50 text-indigo-700 ring-indigo-200">
                          Manual
                        </Badge>
                      )}
                    </div>
                    <p className="mt-0.5 max-w-md text-xs text-slate-500">{segment.description}</p>
                  </td>
                  <td className="table-cell">
                    <p className="max-w-md whitespace-normal text-xs text-slate-600">
                      {segment.rule_description}
                    </p>
                  </td>
                  <td className="table-cell text-right tabular-nums font-medium">
                    <Link
                      to={`/customers?segment_id=${segment.id}`}
                      className="text-brand-700 hover:text-brand-800"
                    >
                      {formatNumber(segment.member_count)}
                    </Link>
                  </td>
                  <td className="table-cell text-xs text-slate-500">
                    {formatDateTime(segment.last_evaluated_at)}
                  </td>
                  <td className="table-cell">
                    <div className="flex justify-end gap-1">
                      <button
                        type="button"
                        className="btn-ghost px-2 py-1 text-xs"
                        onClick={() => setEditing(segment)}
                      >
                        {segment.is_system ? 'View' : 'Edit'}
                      </button>
                      <button
                        type="button"
                        className="btn-ghost px-2 py-1 text-xs"
                        onClick={() =>
                          downloadFile(
                            `/api/v1/segments/${segment.id}/export.csv`,
                            `segment-${segment.id}.csv`,
                          ).catch(() => notify('Could not export this segment.', 'error'))
                        }
                      >
                        Export
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </TableShell>
        </Card>
      )}

      {editing && (
        <SegmentEditor
          segment={editing === 'new' ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            refetch();
          }}
        />
      )}
    </>
  );
}

function SegmentEditor({
  segment,
  onClose,
  onSaved,
}: {
  segment: Segment | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [name, setName] = useState(segment?.name ?? '');
  const [description, setDescription] = useState(segment?.description ?? '');
  const [rule, setRule] = useState<RuleNode>(segment?.rule_definition ?? emptyGroup());
  const [preview, setPreview] = useState<SegmentPreview | null>(null);

  const { data: fieldData } = useQuery<{ fields: FieldDefinition[] }>('/api/v1/segments/fields');
  const readOnly = Boolean(segment?.is_system);

  const runPreview = useMutation(async (candidate: RuleNode) => {
    const result = await api.post<SegmentPreview>('/api/v1/segments/preview', {
      rule_definition: candidate,
      limit: 8,
    });
    setPreview(result);
    return result;
  });

  const save = useMutation(async () => {
    if (segment) {
      return api.patch<Segment>(`/api/v1/segments/${segment.id}`, {
        name,
        description,
        rule_definition: rule,
      });
    }
    return api.post<Segment>('/api/v1/segments', {
      name,
      description,
      segment_type: 'DYNAMIC',
      rule_definition: rule,
    });
  });

  const duplicate = useMutation(async () =>
    api.post<Segment>(`/api/v1/segments/${segment!.id}/duplicate`),
  );

  // Preview the rule the editor opens with, so the count is never stale.
  useEffect(() => {
    runPreview.run(rule);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <Modal
      open
      title={segment ? segment.name : 'New segment'}
      description={
        readOnly
          ? 'Built-in segments have fixed rules. Duplicate it to create an editable copy.'
          : 'Build a rule, preview who it matches, then save.'
      }
      onClose={onClose}
      size="xl"
      footer={
        <>
          <button type="button" className="btn-secondary" onClick={onClose}>
            Cancel
          </button>
          {readOnly ? (
            <button
              type="button"
              className="btn-primary"
              onClick={async () => {
                const result = await duplicate.run();
                if (result) {
                  notify(`Created "${result.name}".`);
                  onSaved();
                }
              }}
              disabled={duplicate.loading}
            >
              {duplicate.loading && <Spinner className="h-4 w-4 text-white" />}
              Duplicate as editable
            </button>
          ) : (
            <button
              type="button"
              className="btn-primary"
              onClick={async () => {
                if (!name.trim()) {
                  notify('Give the segment a name.', 'error');
                  return;
                }
                const result = await save.run();
                if (result) {
                  notify(segment ? 'Segment updated.' : 'Segment created.');
                  onSaved();
                }
              }}
              disabled={save.loading}
            >
              {save.loading && <Spinner className="h-4 w-4 text-white" />}
              {segment ? 'Save changes' : 'Create segment'}
            </button>
          )}
        </>
      }
    >
      <div className="space-y-5">
        <div className="grid gap-3 sm:grid-cols-2">
          <div>
            <label className="label" htmlFor="segment-name">
              Name
            </label>
            <input
              id="segment-name"
              className="input"
              value={name}
              onChange={(e) => setName(e.target.value)}
              disabled={readOnly}
              placeholder="e.g. High value at risk"
            />
          </div>
          <div>
            <label className="label" htmlFor="segment-description">
              Description
            </label>
            <input
              id="segment-description"
              className="input"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              disabled={readOnly}
              placeholder="What is this segment for?"
            />
          </div>
        </div>

        <div>
          <SectionTitle>Rule</SectionTitle>
          {readOnly ? (
            <p className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-600">
              {segment?.rule_description}
            </p>
          ) : (
            <RuleBuilder
              rule={rule}
              fields={fieldData?.fields ?? []}
              onChange={(next) => {
                setRule(next);
                runPreview.run(next);
              }}
            />
          )}
        </div>

        {save.error && (
          <p className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
            {save.error}
          </p>
        )}

        <div>
          <SectionTitle>Preview</SectionTitle>
          {runPreview.error ? (
            <p className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
              {runPreview.error}
            </p>
          ) : runPreview.loading && !preview ? (
            <LoadingState label="Evaluating rule…" />
          ) : preview ? (
            <>
              <div className="mb-3 flex flex-wrap items-baseline gap-x-2 gap-y-1">
                <span className="text-2xl font-semibold tabular-nums text-slate-900">
                  {formatNumber(preview.matched_customers)}
                </span>
                <span className="text-sm text-slate-500">
                  of {formatNumber(preview.total_customers)} customers match (
                  {formatPercent(preview.match_rate)})
                </span>
                {runPreview.loading && <Spinner className="h-4 w-4" />}
              </div>

              {preview.sample.length === 0 ? (
                <p className="rounded-lg border border-dashed border-slate-300 px-3 py-4 text-center text-xs text-slate-500">
                  No customers match this rule yet.
                </p>
              ) : (
                <div className="overflow-hidden rounded-lg border border-slate-200">
                  <TableShell>
                    <thead className="bg-slate-50">
                      <tr>
                        <th className="table-head">Customer</th>
                        <th className="table-head">Stage</th>
                        <th className="table-head text-right">Revenue</th>
                        <th className="table-head text-right">Last order</th>
                        <th className="table-head">Churn</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100 bg-white">
                      {preview.sample.map((customer) => (
                        <tr key={customer.id}>
                          <td className="table-cell">
                            <Link
                              to={`/customers/${customer.id}`}
                              className="font-medium text-brand-700 hover:text-brand-800"
                            >
                              {customer.full_name}
                            </Link>
                          </td>
                          <td className="table-cell">
                            <Badge
                              className={
                                LIFECYCLE_BADGE[
                                  customer.lifecycle_stage as keyof typeof LIFECYCLE_BADGE
                                ] ?? 'bg-slate-100 text-slate-700 ring-slate-200'
                              }
                            >
                              {customer.lifecycle_stage}
                            </Badge>
                          </td>
                          <td className="table-cell text-right tabular-nums">
                            {formatCurrency(customer.lifetime_revenue)}
                          </td>
                          <td className="table-cell text-right tabular-nums">
                            {formatDays(customer.days_since_last_order)}
                          </td>
                          <td className="table-cell">
                            <Badge
                              className={
                                RISK_BADGE[
                                  customer.churn_risk_band as keyof typeof RISK_BADGE
                                ] ?? 'bg-slate-100 text-slate-700 ring-slate-200'
                              }
                            >
                              {customer.churn_score.toFixed(0)}
                            </Badge>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </TableShell>
                </div>
              )}
            </>
          ) : null}
        </div>
      </div>
    </Modal>
  );
}
