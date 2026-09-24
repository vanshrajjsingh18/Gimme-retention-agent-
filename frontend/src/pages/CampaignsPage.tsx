import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';

import { api } from '../api/client';
import {
  Badge,
  Card,
  EmptyState,
  ErrorState,
  LoadingState,
  Modal,
  PageHeader,
  Spinner,
  TableShell,
  notify,
} from '../components/ui';
import { useMutation, useQuery } from '../hooks/useApi';
import type { Campaign, Channel, Segment } from '../types';
import { formatCurrency, formatDateTime, formatNumber, formatPercent, humanize } from '../utils/format';
import { CAMPAIGN_STATUS_BADGE } from '../utils/theme';

interface CampaignOptions {
  objectives: string[];
  channels: Channel[];
  statuses: string[];
  attribution_windows: { hours: number; label: string }[];
}

export default function CampaignsPage() {
  const [creating, setCreating] = useState(false);
  const [statusFilter, setStatusFilter] = useState('');
  const { data, loading, error, refetch } = useQuery<Campaign[]>(
    `/api/v1/campaigns${statusFilter ? `?status=${statusFilter}` : ''}`,
    [statusFilter],
  );
  const { data: options } = useQuery<CampaignOptions>('/api/v1/campaigns/options');

  return (
    <>
      <PageHeader
        title="Campaigns"
        description="Every campaign passes a compliance check and human approval before it can send."
        actions={
          <button type="button" className="btn-primary" onClick={() => setCreating(true)}>
            New campaign
          </button>
        }
      />

      <Card className="mb-4" bodyClassName="px-5 py-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs font-medium text-slate-600">Status</span>
          <button
            type="button"
            onClick={() => setStatusFilter('')}
            aria-pressed={statusFilter === ''}
            className={`rounded-md px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${
              statusFilter === ''
                ? 'bg-brand-50 text-brand-700 ring-brand-300'
                : 'bg-white text-slate-600 ring-slate-200 hover:bg-slate-50'
            }`}
          >
            All
          </button>
          {(options?.statuses ?? []).map((status) => (
            <button
              key={status}
              type="button"
              onClick={() => setStatusFilter(status === statusFilter ? '' : status)}
              aria-pressed={statusFilter === status}
              className={`rounded-md px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${
                statusFilter === status
                  ? (CAMPAIGN_STATUS_BADGE[status] ?? 'bg-brand-50 text-brand-700 ring-brand-300')
                  : 'bg-white text-slate-600 ring-slate-200 hover:bg-slate-50'
              }`}
            >
              {humanize(status)}
            </button>
          ))}
        </div>
      </Card>

      {loading ? (
        <LoadingState label="Loading campaigns…" />
      ) : error ? (
        <ErrorState message={error} onRetry={refetch} />
      ) : !data || data.length === 0 ? (
        <Card>
          <EmptyState
            title={statusFilter ? 'No campaigns with this status' : 'No campaigns yet'}
            description="Create a campaign to reach a segment with a grounded, compliance-checked message."
            action={
              <button type="button" className="btn-primary" onClick={() => setCreating(true)}>
                New campaign
              </button>
            }
          />
        </Card>
      ) : (
        <Card bodyClassName="">
          <TableShell>
            <thead className="bg-slate-50">
              <tr>
                <th className="table-head">Campaign</th>
                <th className="table-head">Status</th>
                <th className="table-head">Audience</th>
                <th className="table-head text-right">Sent</th>
                <th className="table-head text-right">Open rate</th>
                <th className="table-head text-right">Conversions</th>
                <th className="table-head text-right">Revenue</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {data.map((campaign) => (
                <tr key={campaign.id} className="hover:bg-slate-50">
                  <td className="table-cell">
                    <Link
                      to={`/campaigns/${campaign.id}`}
                      className="font-medium text-brand-700 hover:text-brand-800"
                    >
                      {campaign.name}
                    </Link>
                    <p className="text-xs text-slate-500">
                      {humanize(campaign.objective)} · {campaign.channel}
                    </p>
                  </td>
                  <td className="table-cell">
                    <Badge
                      className={
                        CAMPAIGN_STATUS_BADGE[campaign.status] ??
                        'bg-slate-100 text-slate-700 ring-slate-200'
                      }
                    >
                      {humanize(campaign.status)}
                    </Badge>
                    {campaign.started_at && (
                      <p className="mt-0.5 text-xs text-slate-500">
                        {formatDateTime(campaign.started_at)}
                      </p>
                    )}
                  </td>
                  <td className="table-cell text-xs text-slate-600">
                    {campaign.segment_name ?? 'All customers'}
                  </td>
                  <td className="table-cell text-right tabular-nums">
                    {formatNumber(campaign.messages_sent)}
                  </td>
                  <td className="table-cell text-right tabular-nums">
                    {campaign.messages_sent > 0
                      ? formatPercent(
                          campaign.messages_opened /
                            (campaign.messages_delivered || campaign.messages_sent),
                        )
                      : '—'}
                  </td>
                  <td className="table-cell text-right tabular-nums">
                    {formatNumber(campaign.conversions)}
                  </td>
                  <td className="table-cell text-right tabular-nums font-medium">
                    {campaign.attributed_revenue > 0
                      ? formatCurrency(campaign.attributed_revenue)
                      : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </TableShell>
        </Card>
      )}

      {creating && (
        <CreateCampaignModal options={options} onClose={() => setCreating(false)} />
      )}
    </>
  );
}

function CreateCampaignModal({
  options,
  onClose,
}: {
  options: CampaignOptions | null;
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const { data: segments } = useQuery<Segment[]>('/api/v1/segments');
  // Two genuinely different things, so the choice comes first rather than
  // being a setting inside one of them. A standard campaign sends one message
  // to an audience; Smart Reorder produces a different message for each
  // customer at their own predicted time, and almost none of the fields below
  // mean the same thing for both.
  const [kind, setKind] = useState<'STANDARD' | 'SMART_REORDER'>('STANDARD');
  const [form, setForm] = useState({
    name: '',
    description: '',
    objective: 'RETENTION',
    channel: 'EMAIL' as Channel,
    segment_id: '',
    attribution_window_hours: 72,
  });

  const create = useMutation(async () => {
    if (kind === 'SMART_REORDER') {
      const automation = await api.post<{ id: number }>('/api/v1/automations', {
        name: form.name,
        description:
          form.description ||
          'Individual reorder reminders around each customer’s predicted next order.',
        kind: 'NUDGE',
        channel: form.channel,
        objective: 'REORDER',
        segment_id: form.segment_id ? Number(form.segment_id) : null,
        attribution_window_hours: form.attribution_window_hours,
      });
      return { id: automation.id, path: `/automations/${automation.id}` };
    }
    const campaign = await api.post<Campaign>('/api/v1/campaigns', {
      ...form,
      segment_id: form.segment_id ? Number(form.segment_id) : null,
    });
    return { id: campaign.id, path: `/campaigns/${campaign.id}` };
  });

  return (
    <Modal
      open
      title="New campaign"
      description="Set the objective and audience. You will write the message next."
      onClose={onClose}
      size="md"
      footer={
        <>
          <button type="button" className="btn-secondary" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="btn-primary"
            disabled={create.loading}
            onClick={async () => {
              if (!form.name.trim()) {
                notify('Give the campaign a name.', 'error');
                return;
              }
              const result = await create.run();
              if (result) {
                notify('Campaign created as a draft.');
                navigate(result.path);
              }
            }}
          >
            {create.loading && <Spinner className="h-4 w-4 text-white" />}
            Create draft
          </button>
        </>
      }
    >
      <div className="space-y-4">
        <fieldset>
          <legend className="label">Campaign type</legend>
          <div className="grid gap-2 sm:grid-cols-2">
            {[
              {
                value: 'STANDARD' as const,
                label: 'Standard campaign',
                description:
                  'One message to an audience, sent when you schedule it.',
              },
              {
                value: 'SMART_REORDER' as const,
                label: 'Smart Reorder Reminder',
                description:
                  'An individual reminder for each customer, around the time they usually order.',
              },
            ].map((option) => (
              <label
                key={option.value}
                className={`flex cursor-pointer gap-2 rounded-lg border px-3 py-2.5 ${
                  kind === option.value
                    ? 'border-brand-400 bg-brand-50/60'
                    : 'border-slate-200 hover:border-slate-300'
                }`}
              >
                <input
                  type="radio"
                  name="campaign-kind"
                  className="mt-0.5"
                  checked={kind === option.value}
                  onChange={() => setKind(option.value)}
                />
                <span className="min-w-0">
                  <span className="block text-sm font-medium text-slate-800">
                    {option.label}
                  </span>
                  <span className="mt-0.5 block text-xs text-slate-500">
                    {option.description}
                  </span>
                </span>
              </label>
            ))}
          </div>
          {/* Said here because it is the thing people get wrong about this
              campaign type: there is no one send time to choose. */}
          {kind === 'SMART_REORDER' && (
            <p className="mt-2 rounded-lg border border-brand-200 bg-brand-50 px-3 py-2 text-xs text-brand-800">
              Every customer gets their own send time, worked out from their own order
              history — there is no single schedule to set. You will pick the reminder
              timing and message next, then dry-run it to see each individual message
              before anything sends.
            </p>
          )}
        </fieldset>

        <div>
          <label className="label" htmlFor="campaign-name">
            Name
          </label>
          <input
            id="campaign-name"
            className="input"
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            placeholder="e.g. Winter win-back"
          />
        </div>

        <div>
          <label className="label" htmlFor="campaign-description">
            Description
          </label>
          <input
            id="campaign-description"
            className="input"
            value={form.description}
            onChange={(e) => setForm({ ...form, description: e.target.value })}
            placeholder="What is this campaign trying to achieve?"
          />
        </div>

        <div className="grid gap-3 sm:grid-cols-2">
          <div>
            <label className="label" htmlFor="campaign-objective">
              Objective
            </label>
            <select
              id="campaign-objective"
              className="input"
              value={form.objective}
              onChange={(e) => setForm({ ...form, objective: e.target.value })}
            >
              {(options?.objectives ?? []).map((o) => (
                <option key={o} value={o}>
                  {humanize(o)}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="label" htmlFor="campaign-channel">
              Channel
            </label>
            <select
              id="campaign-channel"
              className="input"
              value={form.channel}
              onChange={(e) => setForm({ ...form, channel: e.target.value as Channel })}
            >
              {(options?.channels ?? []).map((c) => (
                <option key={c} value={c}>
                  {humanize(c)}
                </option>
              ))}
            </select>
          </div>
        </div>

        <div>
          <label className="label" htmlFor="campaign-segment">
            Audience
          </label>
          <select
            id="campaign-segment"
            className="input"
            value={form.segment_id}
            onChange={(e) => setForm({ ...form, segment_id: e.target.value })}
          >
            <option value="">All customers</option>
            {(segments ?? []).map((s) => (
              <option key={s.id} value={s.id}>
                {s.name} ({formatNumber(s.member_count)} members)
              </option>
            ))}
          </select>
          <p className="mt-1 text-xs text-slate-500">
            Consent, age verification, suppression and frequency caps are applied on top of this.
          </p>
        </div>

        <div>
          <label className="label" htmlFor="campaign-window">
            Attribution window
          </label>
          <select
            id="campaign-window"
            className="input"
            value={form.attribution_window_hours}
            onChange={(e) =>
              setForm({ ...form, attribution_window_hours: Number(e.target.value) })
            }
          >
            {(options?.attribution_windows ?? [{ hours: 72, label: '72 hours' }]).map((w) => (
              <option key={w.hours} value={w.hours}>
                {w.label}
              </option>
            ))}
          </select>
        </div>

        {create.error && (
          <p className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
            {create.error}
          </p>
        )}
      </div>
    </Modal>
  );
}
