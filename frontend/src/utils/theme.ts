import type { ChurnRiskBand, LifecycleStage } from '../types';

/**
 * One place for the colour meaning of every domain value, so a stage or risk
 * band looks the same in a badge, a table and a chart.
 */

export const LIFECYCLE_COLORS: Record<LifecycleStage, string> = {
  NEW: '#59b0ff',
  ACTIVATING: '#328eff',
  REGULAR: '#1b6ef5',
  HIGH_VALUE: '#7c3aed',
  VIP: '#a21caf',
  REACTIVATED: '#0f9d58',
  AT_RISK: '#e8710a',
  DORMANT: '#b45309',
  CHURNED: '#d93025',
};

export const LIFECYCLE_BADGE: Record<LifecycleStage, string> = {
  NEW: 'bg-sky-50 text-sky-700 ring-sky-200',
  ACTIVATING: 'bg-blue-50 text-blue-700 ring-blue-200',
  REGULAR: 'bg-indigo-50 text-indigo-700 ring-indigo-200',
  HIGH_VALUE: 'bg-violet-50 text-violet-700 ring-violet-200',
  VIP: 'bg-fuchsia-50 text-fuchsia-700 ring-fuchsia-200',
  REACTIVATED: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
  AT_RISK: 'bg-orange-50 text-orange-700 ring-orange-200',
  DORMANT: 'bg-amber-50 text-amber-800 ring-amber-200',
  CHURNED: 'bg-red-50 text-red-700 ring-red-200',
};

export const RISK_COLORS: Record<ChurnRiskBand, string> = {
  LOW: '#0f9d58',
  MEDIUM: '#d9a300',
  HIGH: '#e8710a',
  CRITICAL: '#d93025',
};

export const RISK_BADGE: Record<ChurnRiskBand, string> = {
  LOW: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
  MEDIUM: 'bg-yellow-50 text-yellow-800 ring-yellow-200',
  HIGH: 'bg-orange-50 text-orange-700 ring-orange-200',
  CRITICAL: 'bg-red-50 text-red-700 ring-red-200',
};

export const CAMPAIGN_STATUS_BADGE: Record<string, string> = {
  DRAFT: 'bg-slate-100 text-slate-700 ring-slate-200',
  AI_GENERATED: 'bg-sky-50 text-sky-700 ring-sky-200',
  VALIDATED: 'bg-sky-50 text-sky-700 ring-sky-200',
  COMPLIANCE_CHECKED: 'bg-indigo-50 text-indigo-700 ring-indigo-200',
  AWAITING_APPROVAL: 'bg-amber-50 text-amber-800 ring-amber-200',
  APPROVED: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
  SCHEDULED: 'bg-blue-50 text-blue-700 ring-blue-200',
  RUNNING: 'bg-blue-50 text-blue-700 ring-blue-200',
  PAUSED: 'bg-slate-100 text-slate-700 ring-slate-200',
  COMPLETED: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
  FAILED: 'bg-red-50 text-red-700 ring-red-200',
  CANCELLED: 'bg-slate-100 text-slate-500 ring-slate-200',
};

export const MESSAGE_STATUS_BADGE: Record<string, string> = {
  DRAFT: 'bg-slate-100 text-slate-700 ring-slate-200',
  GENERATED: 'bg-sky-50 text-sky-700 ring-sky-200',
  VALIDATION_FAILED: 'bg-red-50 text-red-700 ring-red-200',
  EDITED: 'bg-indigo-50 text-indigo-700 ring-indigo-200',
  APPROVED: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
  REJECTED: 'bg-slate-100 text-slate-500 ring-slate-200',
  SENT: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
  FAILED: 'bg-red-50 text-red-700 ring-red-200',
};

export const AUTOMATION_STATUS_BADGE: Record<string, string> = {
  DRAFT: 'bg-slate-100 text-slate-700 ring-slate-200',
  ACTIVE: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
  PAUSED: 'bg-amber-50 text-amber-800 ring-amber-200',
  COMPLETED: 'bg-slate-100 text-slate-500 ring-slate-200',
};

export const SEND_STATUS_BADGE: Record<string, string> = {
  SCHEDULED: 'bg-slate-100 text-slate-700 ring-slate-200',
  QUEUED: 'bg-sky-50 text-sky-700 ring-sky-200',
  SENT: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
  DELIVERED: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
  FAILED: 'bg-red-50 text-red-700 ring-red-200',
  SKIPPED: 'bg-amber-50 text-amber-800 ring-amber-200',
  PREVIEW: 'bg-indigo-50 text-indigo-700 ring-indigo-200',
};

/** Plain-English explanations for the reasons a send was withheld. */
export const SKIP_REASON_LABEL: Record<string, string> = {
  NO_CONSENT: 'No marketing consent',
  SUPPRESSED: 'Suppressed (opted out)',
  AGE_NOT_VERIFIED: 'Age not verified',
  MISSING_CONTACT: 'No contact details',
  FREQUENCY_CAP: 'Frequency cap reached',
  QUIET_HOURS: 'Outside send window',
  DEDUPED: 'Already messaged today',
  ALREADY_ORDERED: 'Already ordered',
  PENDING_ORDER: 'Order in flight',
  LEFT_SEGMENT: 'No longer matches segment',
  VALIDATION_FAILED: 'Failed content checks',
};

export const AUTOMATION_KIND_LABEL: Record<string, string> = {
  COHORT_BULK: 'Cohort bulk send',
  SEQUENCE: 'Recurring sequence',
  NUDGE: 'Behavioural nudge',
};

/** Categorical palette for charts with an arbitrary number of series. */
export const CHART_COLORS = [
  '#1b6ef5',
  '#0f9d58',
  '#7c3aed',
  '#e8710a',
  '#0891b2',
  '#d93025',
  '#a21caf',
  '#65a30d',
  '#b45309',
  '#475569',
];

export function riskColor(band: string): string {
  return RISK_COLORS[band as ChurnRiskBand] ?? '#475569';
}

export function lifecycleColor(stage: string): string {
  return LIFECYCLE_COLORS[stage as LifecycleStage] ?? '#475569';
}
