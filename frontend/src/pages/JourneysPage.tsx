import { useState } from 'react';

import { api } from '../api/client';
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
  notify,
} from '../components/ui';
import { useMutation, useQuery } from '../hooks/useApi';
import type { Journey, Segment } from '../types';
import { formatDateTime, formatNumber, humanize } from '../utils/format';

interface Catalog {
  triggers: string[];
  delays: string[];
  conditions: string[];
  actions: string[];
}

type NodeDraft = {
  node_type: 'DELAY' | 'CONDITION' | 'ACTION';
  subtype: string;
  config: Record<string, unknown>;
};

const NODE_TYPE_STYLE: Record<string, string> = {
  DELAY: 'bg-amber-50 text-amber-800 ring-amber-200',
  CONDITION: 'bg-indigo-50 text-indigo-700 ring-indigo-200',
  ACTION: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
  TRIGGER: 'bg-brand-50 text-brand-700 ring-brand-200',
};

export default function JourneysPage() {
  const { data, loading, error, refetch } = useQuery<Journey[]>('/api/v1/journeys');
  const { data: catalog } = useQuery<Catalog>('/api/v1/journeys/catalog');
  const [creating, setCreating] = useState(false);
  const [inspecting, setInspecting] = useState<Journey | null>(null);

  const activate = useMutation(async (id: number) => {
    const result = await api.post<Journey>(`/api/v1/journeys/${id}/activate`);
    refetch();
    return result;
  });
  const pause = useMutation(async (id: number) => {
    const result = await api.post<Journey>(`/api/v1/journeys/${id}/pause`);
    refetch();
    return result;
  });
  const enrol = useMutation(async (id: number) =>
    api.post<{ enrolled: number }>(`/api/v1/journeys/${id}/enrol`),
  );
  const run = useMutation(async (id: number) => {
    const result = await api.post<Record<string, number>>(`/api/v1/journeys/${id}/run`);
    refetch();
    return result;
  });

  if (loading) return <LoadingState label="Loading journeys…" />;
  if (error) return <ErrorState message={error} onRetry={refetch} />;

  return (
    <>
      <PageHeader
        title="Journeys"
        description="Automated multi-step sequences. Every message still passes the same grounding and compliance checks as a campaign."
        actions={
          <button type="button" className="btn-primary" onClick={() => setCreating(true)}>
            New journey
          </button>
        }
      />

      {!data || data.length === 0 ? (
        <Card>
          <EmptyState
            title="No journeys yet"
            description="A journey reacts to a trigger — a first order, a customer becoming at risk — and runs a sequence of waits, conditions and actions."
            action={
              <button type="button" className="btn-primary" onClick={() => setCreating(true)}>
                New journey
              </button>
            }
          />
        </Card>
      ) : (
        <div className="grid gap-4 lg:grid-cols-2">
          {data.map((journey) => (
            <Card
              key={journey.id}
              title={journey.name}
              description={journey.description}
              actions={
                <Badge
                  className={
                    journey.status === 'ACTIVE'
                      ? 'bg-emerald-50 text-emerald-700 ring-emerald-200'
                      : journey.status === 'PAUSED'
                        ? 'bg-amber-50 text-amber-800 ring-amber-200'
                        : 'bg-slate-100 text-slate-600 ring-slate-200'
                  }
                >
                  {humanize(journey.status)}
                </Badge>
              }
            >
              <div className="mb-3 flex flex-wrap items-center gap-1.5">
                <Badge className={NODE_TYPE_STYLE.TRIGGER}>
                  {humanize(journey.trigger_type)}
                </Badge>
                {journey.nodes.map((node) => (
                  <span key={node.id} className="flex items-center gap-1.5">
                    <span className="text-slate-300">→</span>
                    <Badge className={NODE_TYPE_STYLE[node.node_type] ?? ''}>
                      {humanize(node.subtype)}
                    </Badge>
                  </span>
                ))}
              </div>

              <dl className="mb-4 grid grid-cols-2 gap-3 text-sm">
                <div>
                  <dt className="text-xs text-slate-500">Entered</dt>
                  <dd className="font-medium tabular-nums text-slate-900">
                    {formatNumber(journey.total_entered)}
                  </dd>
                </div>
                <div>
                  <dt className="text-xs text-slate-500">Completed</dt>
                  <dd className="font-medium tabular-nums text-slate-900">
                    {formatNumber(journey.total_completed)}
                  </dd>
                </div>
              </dl>

              <div className="flex flex-wrap gap-2">
                {journey.status === 'ACTIVE' ? (
                  <button
                    type="button"
                    className="btn-secondary px-2.5 py-1 text-xs"
                    onClick={() => pause.run(journey.id)}
                  >
                    Pause
                  </button>
                ) : (
                  <button
                    type="button"
                    className="btn-secondary px-2.5 py-1 text-xs"
                    onClick={async () => {
                      const result = await activate.run(journey.id);
                      if (result) notify(`"${journey.name}" is now active.`);
                      else if (activate.error) notify(activate.error, 'error');
                    }}
                  >
                    Activate
                  </button>
                )}
                <button
                  type="button"
                  className="btn-secondary px-2.5 py-1 text-xs"
                  disabled={journey.status !== 'ACTIVE' || enrol.loading}
                  onClick={async () => {
                    const result = await enrol.run(journey.id);
                    if (result) {
                      notify(`Enrolled ${result.enrolled} customers.`);
                      refetch();
                    }
                  }}
                >
                  Enrol eligible
                </button>
                <button
                  type="button"
                  className="btn-primary px-2.5 py-1 text-xs"
                  disabled={journey.status !== 'ACTIVE' || run.loading}
                  onClick={async () => {
                    const stats = await run.run(journey.id);
                    if (stats) {
                      notify(
                        `Advanced ${stats.advanced} customers · ${stats.actions} actions · ` +
                          `${stats.waiting} waiting · ${stats.completed} completed.`,
                      );
                    }
                  }}
                >
                  {run.loading && <Spinner className="h-4 w-4 text-white" />}
                  Run now
                </button>
                <button
                  type="button"
                  className="btn-ghost px-2.5 py-1 text-xs"
                  onClick={() => setInspecting(journey)}
                >
                  Execution log
                </button>
              </div>
            </Card>
          ))}
        </div>
      )}

      {creating && catalog && (
        <CreateJourneyModal
          catalog={catalog}
          onClose={() => setCreating(false)}
          onCreated={() => {
            setCreating(false);
            refetch();
          }}
        />
      )}

      {inspecting && (
        <ExecutionLogModal journey={inspecting} onClose={() => setInspecting(null)} />
      )}
    </>
  );
}

function CreateJourneyModal({
  catalog,
  onClose,
  onCreated,
}: {
  catalog: Catalog;
  onClose: () => void;
  onCreated: () => void;
}) {
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [trigger, setTrigger] = useState(catalog.triggers[0]);
  const [nodes, setNodes] = useState<NodeDraft[]>([
    { node_type: 'DELAY', subtype: 'WAIT_DAYS', config: { days: 3 } },
    { node_type: 'ACTION', subtype: 'SEND_EMAIL', config: {} },
  ]);

  const { data: segments } = useQuery<Segment[]>('/api/v1/segments');

  const create = useMutation(async () =>
    api.post<Journey>('/api/v1/journeys', {
      name,
      description,
      trigger_type: trigger,
      trigger_config: {},
      nodes,
    }),
  );

  function updateNode(index: number, patch: Partial<NodeDraft>) {
    setNodes((current) =>
      current.map((node, i) => (i === index ? { ...node, ...patch } : node)),
    );
  }

  const subtypesFor = (type: NodeDraft['node_type']) =>
    type === 'DELAY' ? catalog.delays : type === 'CONDITION' ? catalog.conditions : catalog.actions;

  return (
    <Modal
      open
      title="New journey"
      description="Steps run in order. A failed condition exits the customer from the journey."
      onClose={onClose}
      size="lg"
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
              if (!name.trim()) {
                notify('Give the journey a name.', 'error');
                return;
              }
              const result = await create.run();
              if (result) {
                notify('Journey created as a draft.');
                onCreated();
              }
            }}
          >
            {create.loading && <Spinner className="h-4 w-4 text-white" />}
            Create journey
          </button>
        </>
      }
    >
      <div className="space-y-4">
        <div className="grid gap-3 sm:grid-cols-2">
          <div>
            <label className="label" htmlFor="journey-name">
              Name
            </label>
            <input
              id="journey-name"
              className="input"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Second order nudge"
            />
          </div>
          <div>
            <label className="label" htmlFor="journey-trigger">
              Trigger
            </label>
            <select
              id="journey-trigger"
              className="input"
              value={trigger}
              onChange={(e) => setTrigger(e.target.value)}
            >
              {catalog.triggers.map((t) => (
                <option key={t} value={t}>
                  {humanize(t)}
                </option>
              ))}
            </select>
          </div>
        </div>

        <div>
          <label className="label" htmlFor="journey-description">
            Description
          </label>
          <input
            id="journey-description"
            className="input"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </div>

        <div>
          <SectionTitle>Steps</SectionTitle>
          <ul className="space-y-2">
            {nodes.map((node, index) => (
              <li key={index} className="rounded-lg border border-slate-200 bg-white p-3">
                <div className="grid gap-2 sm:grid-cols-[130px_1fr_auto]">
                  <select
                    className="input text-sm"
                    value={node.node_type}
                    onChange={(e) => {
                      const type = e.target.value as NodeDraft['node_type'];
                      updateNode(index, {
                        node_type: type,
                        subtype: subtypesFor(type)[0],
                        config: {},
                      });
                    }}
                    aria-label="Step type"
                  >
                    <option value="DELAY">Wait</option>
                    <option value="CONDITION">Condition</option>
                    <option value="ACTION">Action</option>
                  </select>

                  <select
                    className="input text-sm"
                    value={node.subtype}
                    onChange={(e) => updateNode(index, { subtype: e.target.value, config: {} })}
                    aria-label="Step"
                  >
                    {subtypesFor(node.node_type).map((s) => (
                      <option key={s} value={s}>
                        {humanize(s)}
                      </option>
                    ))}
                  </select>

                  <button
                    type="button"
                    className="btn-ghost self-center px-2 py-1 text-xs text-red-600 hover:bg-red-50"
                    onClick={() => setNodes((c) => c.filter((_, i) => i !== index))}
                  >
                    Remove
                  </button>
                </div>

                <NodeConfig
                  node={node}
                  segments={segments ?? []}
                  onChange={(config) => updateNode(index, { config })}
                />
              </li>
            ))}
          </ul>

          <button
            type="button"
            className="btn-secondary mt-2 px-2.5 py-1 text-xs"
            onClick={() =>
              setNodes((c) => [
                ...c,
                { node_type: 'ACTION', subtype: catalog.actions[0], config: {} },
              ])
            }
          >
            + Step
          </button>
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

function NodeConfig({
  node,
  segments,
  onChange,
}: {
  node: NodeDraft;
  segments: Segment[];
  onChange: (config: Record<string, unknown>) => void;
}) {
  if (node.subtype === 'WAIT_HOURS' || node.subtype === 'WAIT_DAYS') {
    const unit = node.subtype === 'WAIT_HOURS' ? 'hours' : 'days';
    return (
      <div className="mt-2">
        <label className="label" htmlFor={`wait-${node.subtype}`}>
          Wait for ({unit})
        </label>
        <input
          id={`wait-${node.subtype}`}
          type="number"
          min={1}
          className="input max-w-[140px] text-sm"
          value={String(node.config[unit] ?? (unit === 'days' ? 3 : 24))}
          onChange={(e) => onChange({ [unit]: Number(e.target.value) })}
        />
      </div>
    );
  }

  if (node.subtype === 'WAIT_UNTIL_DATE') {
    return (
      <div className="mt-2">
        <label className="label" htmlFor="wait-date">
          Wait until
        </label>
        <input
          id="wait-date"
          type="date"
          className="input max-w-[200px] text-sm"
          value={String(node.config.date ?? '')}
          onChange={(e) => onChange({ date: e.target.value })}
        />
      </div>
    );
  }

  if (node.subtype === 'IN_SEGMENT' || node.subtype.includes('SEGMENT')) {
    return (
      <div className="mt-2">
        <label className="label" htmlFor="node-segment">
          Segment
        </label>
        <select
          id="node-segment"
          className="input text-sm"
          value={String(node.config.segment_id ?? '')}
          onChange={(e) => onChange({ ...node.config, segment_id: Number(e.target.value) })}
        >
          <option value="">Select a segment…</option>
          {segments.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}
            </option>
          ))}
        </select>
      </div>
    );
  }

  if (node.subtype === 'HAS_ORDERED' || node.subtype === 'HAS_NOT_ORDERED') {
    return (
      <div className="mt-2">
        <label className="label" htmlFor="within-days">
          Within the last (days)
        </label>
        <input
          id="within-days"
          type="number"
          min={1}
          className="input max-w-[140px] text-sm"
          value={String(node.config.within_days ?? 30)}
          onChange={(e) => onChange({ within_days: Number(e.target.value) })}
        />
      </div>
    );
  }

  if (node.subtype === 'CREATE_INTERNAL_ALERT') {
    return (
      <div className="mt-2">
        <label className="label" htmlFor="alert-message">
          Alert message
        </label>
        <input
          id="alert-message"
          className="input text-sm"
          value={String(node.config.message ?? '')}
          onChange={(e) => onChange({ message: e.target.value })}
          placeholder="e.g. High-value customer needs a call"
        />
      </div>
    );
  }

  return null;
}

function ExecutionLogModal({ journey, onClose }: { journey: Journey; onClose: () => void }) {
  const { data, loading } = useQuery<{
    status_counts: Record<string, number>;
    executions: {
      customer_id: number;
      customer_name: string;
      action: string;
      outcome: string;
      detail: string;
      executed_at: string;
    }[];
  }>(`/api/v1/journeys/${journey.id}/executions`);

  return (
    <Modal open title={`${journey.name} — execution log`} onClose={onClose} size="lg">
      {loading ? (
        <LoadingState />
      ) : !data ? null : (
        <>
          <div className="mb-4 flex flex-wrap gap-2">
            {Object.entries(data.status_counts).map(([status, count]) => (
              <Badge key={status} className="bg-slate-100 text-slate-700 ring-slate-200">
                {humanize(status)}: {formatNumber(count)}
              </Badge>
            ))}
          </div>

          {data.executions.length === 0 ? (
            <EmptyState
              title="Nothing has run yet"
              description="Activate the journey, enrol customers, then run it."
            />
          ) : (
            <ul className="divide-y divide-slate-100">
              {data.executions.map((e, index) => (
                <li key={index} className="flex items-start justify-between gap-3 py-2.5">
                  <div className="min-w-0">
                    <p className="truncate text-sm text-slate-800">
                      {e.customer_name} · {humanize(e.action)}
                    </p>
                    <p className="text-xs text-slate-500">{e.detail}</p>
                    <p className="text-xs text-slate-400">{formatDateTime(e.executed_at)}</p>
                  </div>
                  <Badge
                    className={
                      e.outcome === 'OK'
                        ? 'bg-emerald-50 text-emerald-700 ring-emerald-200'
                        : e.outcome === 'BLOCKED' || e.outcome === 'FAILED'
                          ? 'bg-red-50 text-red-700 ring-red-200'
                          : 'bg-slate-100 text-slate-600 ring-slate-200'
                    }
                  >
                    {humanize(e.outcome)}
                  </Badge>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </Modal>
  );
}
