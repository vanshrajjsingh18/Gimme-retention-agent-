/**
 * The confirmation card for one planned change.
 *
 * Confirm is bound to this card's action id — the server executes exactly the
 * plan shown here and nothing else. High-risk actions use a red button and
 * spell out that real customers are involved.
 */
import { useState } from 'react';

import { Badge, Spinner } from '../../components/ui';
import type { CopilotActionView, DryRun } from './types';

const STATUS_STYLE: Record<string, string> = {
  PENDING: 'bg-amber-50 text-amber-800 ring-amber-200',
  EXECUTING: 'bg-sky-50 text-sky-700 ring-sky-200',
  EXECUTED: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
  FAILED: 'bg-red-50 text-red-700 ring-red-200',
  CANCELLED: 'bg-slate-100 text-slate-600 ring-slate-200',
  EXPIRED: 'bg-slate-100 text-slate-600 ring-slate-200',
  SUPERSEDED: 'bg-slate-100 text-slate-500 ring-slate-200',
};

const STATUS_LABEL: Record<string, string> = {
  PENDING: 'Action required',
  EXECUTING: 'Executing…',
  EXECUTED: 'Done',
  FAILED: 'Failed',
  CANCELLED: 'Cancelled',
  EXPIRED: 'Expired',
  SUPERSEDED: 'Replaced by a newer plan',
};

export function RiskBadge({ risk }: { risk: string }) {
  if (risk === 'HIGH_RISK_WRITE') return <Badge className="bg-red-50 text-red-700 ring-red-200">High risk</Badge>;
  if (risk === 'WRITE') return <Badge className="bg-amber-50 text-amber-800 ring-amber-200">Change</Badge>;
  return <Badge className="bg-slate-100 text-slate-600 ring-slate-200">Read</Badge>;
}

function show(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—';
  if (typeof value === 'string') return value;
  if (Array.isArray(value) && value.every((v) => v && typeof v === 'object' && 'code' in v)) {
    return (value as { code: string; allocation: number; enabled?: boolean }[])
      .filter((v) => v.enabled !== false)
      .map((v) => `${v.code} ${v.allocation}%`)
      .join(', ') || '—';
  }
  return JSON.stringify(value);
}

function Changes({ before, after }: { before: Record<string, unknown>; after: Record<string, unknown> }) {
  const keys = Object.keys(after ?? {}).filter(
    (k) => !['id', 'type'].includes(k) && JSON.stringify(before?.[k]) !== JSON.stringify(after[k]),
  );
  if (!before || !Object.keys(before).length || !keys.length) return null;
  return (
    <div className="mt-3">
      <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Changes</p>
      <dl className="mt-1 space-y-1 text-xs">
        {keys.map((k) => (
          <div key={k} className="grid grid-cols-[8rem_1fr] gap-2">
            <dt className="font-medium text-slate-600">{k.replace(/_/g, ' ')}</dt>
            <dd className="min-w-0 break-words text-slate-700">
              <span className="text-slate-400 line-through">{show(before[k])}</span>
              <span className="mx-1 text-slate-400">→</span>
              <span className="font-medium">{show(after[k])}</span>
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

export function DryRunSummary({ dry }: { dry: DryRun }) {
  const coupons = Object.entries(dry.coupon_allocation);
  const couponTotal = coupons.reduce((s, [, n]) => s + n, 0) || 1;
  const check = dry.message_check;
  return (
    <div className="mt-3 space-y-3">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        {[
          ['Analysed', dry.analysed],
          ['Final audience', dry.final_audience],
          [`Today`, dry.messages_today],
          ['Tonight', dry.messages_tonight],
        ].map(([label, value]) => (
          <div key={label as string} className="rounded-lg bg-slate-50 px-3 py-2 ring-1 ring-inset ring-slate-200">
            <p className="text-[11px] uppercase tracking-wide text-slate-500">{label}</p>
            <p className="text-lg font-semibold text-slate-900">{Number(value).toLocaleString()}</p>
          </div>
        ))}
      </div>
      {dry.campaign.timing && (
        <p className="text-xs text-slate-600">
          {dry.campaign.channel} · {dry.campaign.timing.minutes_before_predicted_order} min before each predicted order ·
          minimum confidence {dry.campaign.timing.min_confidence} · at most one reminder per {dry.campaign.timing.min_gap_days} days ·
          next {dry.horizon_days} days
        </p>
      )}
      {Object.keys(dry.excluded).length > 0 && (
        <div>
          <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Excluded</p>
          <ul className="mt-1 space-y-0.5 text-xs text-slate-700">
            {Object.entries(dry.excluded).map(([reason, n]) => (
              <li key={reason} className="flex justify-between gap-3">
                <span className="min-w-0 truncate" title={reason}>{reason}</span>
                <span className="font-medium tabular-nums">{n}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {coupons.length > 0 && (
        <div>
          <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Coupon allocation</p>
          <ul className="mt-1 space-y-1 text-xs">
            {coupons.map(([code, n]) => (
              <li key={code} className="flex items-center gap-2">
                <span className="w-24 font-mono text-slate-700">{code}</span>
                <span className="h-1.5 flex-1 rounded-full bg-slate-100">
                  <span className="block h-1.5 rounded-full bg-brand-500" style={{ width: `${(n / couponTotal) * 100}%` }} />
                </span>
                <span className="w-8 text-right tabular-nums text-slate-700">{n}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {dry.schedule_sample.length > 0 && (
        <div className="overflow-x-auto rounded-lg ring-1 ring-slate-200">
          <table className="min-w-full divide-y divide-slate-200 text-xs">
            <thead className="bg-slate-50 text-left text-slate-600">
              <tr>
                <th className="px-3 py-1.5 font-semibold">Customer</th>
                <th className="px-3 py-1.5 font-semibold">Sends</th>
                <th className="px-3 py-1.5 font-semibold">Predicted order</th>
                <th className="px-3 py-1.5 font-semibold">Coupon</th>
                <th className="px-3 py-1.5 font-semibold">Product</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 bg-white">
              {dry.schedule_sample.map((s) => (
                <tr key={s.customer_id} title={s.message ?? ''}>
                  <td className="whitespace-nowrap px-3 py-1.5 text-slate-800">{s.customer}</td>
                  <td className="whitespace-nowrap px-3 py-1.5 text-slate-700">
                    {s.send_at_local}
                    {s.moved_for_send_window && (
                      <span className="ml-1 text-[10px] text-amber-700" title="Outside the send window, so moved to the nearest allowed time">
                        (send window)
                      </span>
                    )}
                  </td>
                  <td className="whitespace-nowrap px-3 py-1.5 text-slate-500">{s.predicted_order_local}</td>
                  <td className="whitespace-nowrap px-3 py-1.5 font-mono text-slate-700">{s.coupon_code ?? '—'}</td>
                  <td className="whitespace-nowrap px-3 py-1.5 text-slate-500">{s.product ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {dry.schedule_sample[0]?.message && (
        <div className="rounded-lg bg-white px-3 py-2 text-xs text-slate-700 ring-1 ring-inset ring-slate-200">
          <p className="mb-1 text-[11px] uppercase tracking-wide text-slate-500">
            Example — {dry.schedule_sample[0].customer}
          </p>
          “{dry.schedule_sample[0].message}”
        </div>
      )}
      <p className={`text-xs ${check.sendable_without_review ? 'text-emerald-700' : 'text-amber-800'}`}>
        {check.sendable_without_review
          ? 'Copy passes compliance checks'
          : `Compliance: ${[...check.blocking, ...check.needs_confirmation].join('; ')}`}
        {check.sms ? ` · ${check.sms.characters} chars, ${check.sms.segments} SMS segment${check.sms.segments === 1 ? '' : 's'}` : ''}
        {check.warnings.length ? ` · ${check.warnings.join(' ')}` : ''}
      </p>
    </div>
  );
}

export default function ActionCard({
  action,
  busy,
  onConfirm,
  onCancel,
  onRetry,
}: {
  action: CopilotActionView;
  busy: boolean;
  onConfirm: (id: number) => void;
  onCancel: (id: number) => void;
  onRetry: (id: number) => void;
}) {
  const [details, setDetails] = useState(false);
  const preview = action.preview ?? ({} as CopilotActionView['preview']);
  const highRisk = action.risk === 'HIGH_RISK_WRITE';
  const pending = action.status === 'PENDING';
  const matched = (preview.before as { matched?: { name: string; kind: string; status: string; channel: string }[] })?.matched;
  const result = (preview.result ?? {}) as Record<string, unknown>;

  return (
    <div
      className={`rounded-xl border bg-white p-4 shadow-sm ${
        pending ? (highRisk ? 'border-red-300 ring-2 ring-red-100' : 'border-amber-300 ring-2 ring-amber-100') : 'border-slate-200'
      }`}
      data-testid={`action-card-${action.id}`}
    >
      <div className="flex flex-wrap items-center gap-2">
        <Badge className={STATUS_STYLE[action.status] ?? STATUS_STYLE.PENDING}>{STATUS_LABEL[action.status] ?? action.status}</Badge>
        <RiskBadge risk={action.risk} />
        <span className="text-xs text-slate-400">Action #{action.id}</span>
      </div>
      <p className="mt-2 text-sm font-semibold text-slate-900">{action.summary}</p>
      {highRisk && pending && (
        <p className="mt-1 text-xs font-medium text-red-700">
          This affects real customers or live sending. It runs only when you press Confirm.
        </p>
      )}

      {preview.dry_run && <DryRunSummary dry={preview.dry_run} />}
      {matched && (
        <div className="mt-3">
          <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">
            This will modify {matched.length} campaign{matched.length === 1 ? '' : 's'}
          </p>
          <ul className="mt-1 space-y-0.5 text-xs text-slate-700">
            {matched.map((m, i) => (
              <li key={i}>{m.name} · {m.kind} · {m.status} · {m.channel}</li>
            ))}
          </ul>
        </div>
      )}
      {!preview.dry_run && typeof result.members === 'number' && (
        <p className="mt-2 text-xs text-slate-600">
          {String(result.name ?? 'Segment')}: <strong>{result.members as number}</strong> customers
        </p>
      )}
      {Array.isArray(result.coupon_codes_added_to_brand_settings) && (result.coupon_codes_added_to_brand_settings as string[]).length > 0 && (
        <p className="mt-2 text-xs text-slate-600">
          Adds {(result.coupon_codes_added_to_brand_settings as string[]).join(', ')} to Brand Settings’ verified coupon codes.
        </p>
      )}
      <Changes before={preview.before} after={preview.after} />

      {details && (
        <pre className="mt-3 max-h-64 overflow-auto rounded-lg bg-slate-50 p-3 text-[11px] text-slate-700 ring-1 ring-inset ring-slate-200">
          {JSON.stringify({ tool: action.tool_name, arguments: action.arguments }, null, 2)}
        </pre>
      )}

      {action.status === 'FAILED' && action.error && (
        <p className="mt-3 rounded-lg bg-red-50 px-3 py-2 text-xs text-red-700">{action.error}</p>
      )}
      {action.status === 'EXECUTED' && (
        <p className="mt-3 text-xs text-emerald-700">Executed and recorded as receipt #{action.execution_id}.</p>
      )}

      <div className="mt-4 flex flex-wrap items-center gap-2">
        <button type="button" className="btn-ghost px-2.5 py-1.5 text-xs" onClick={() => setDetails((d) => !d)}>
          {details ? 'Hide details' : 'Review changes'}
        </button>
        {pending && (
          <>
            <button
              type="button"
              className={`${highRisk ? 'btn-danger' : 'btn-primary'} px-3 py-1.5 text-xs`}
              disabled={busy}
              onClick={() => onConfirm(action.id)}
            >
              {busy ? <Spinner className="h-3.5 w-3.5 text-white" /> : null}
              {highRisk ? 'Confirm — go live' : 'Confirm'}
            </button>
            <button type="button" className="btn-secondary px-3 py-1.5 text-xs" disabled={busy} onClick={() => onCancel(action.id)}>
              Cancel
            </button>
          </>
        )}
        {action.status === 'FAILED' && (
          <button type="button" className="btn-secondary px-3 py-1.5 text-xs" disabled={busy} onClick={() => onRetry(action.id)}>
            Retry
          </button>
        )}
      </div>
    </div>
  );
}
