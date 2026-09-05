import { useState } from 'react';

import { Spinner, notify } from '../components/ui';
import { useQuery } from '../hooks/useApi';
import type { Automation, AutomationKind, Segment } from '../types';

const WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];

type StepDraft = { name: string; offset_days: number; message_template: string };

const STARTER_STEPS: StepDraft[] = [
  { name: 'Day 0', offset_days: 0, message_template: '' },
  { name: 'Day 7', offset_days: 7, message_template: '' },
];

export default function AutomationForm({
  kind,
  onCreated,
  onCancel,
  submit,
}: {
  kind: AutomationKind;
  onCreated: (automation: Automation) => void;
  onCancel: () => void;
  submit: (payload: Record<string, unknown>) => Promise<Automation>;
}) {
  const { data: segments } = useQuery<Segment[]>('/api/v1/segments');

  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [segmentId, setSegmentId] = useState<string>('');
  const [recurrence, setRecurrence] = useState('ONCE');
  const [recurrenceDay, setRecurrenceDay] = useState(0);
  const [sendTime, setSendTime] = useState('10:00');
  const [enrollmentMode, setEnrollmentMode] = useState('ROLLING');
  const [messageTemplate, setMessageTemplate] = useState('');
  const [stopOnOrder, setStopOnOrder] = useState(true);
  const [endsAt, setEndsAt] = useState('');
  const [steps, setSteps] = useState<StepDraft[]>(STARTER_STEPS);
  const [triggerType, setTriggerType] = useState('SEGMENT_ENTRY');
  const [variants, setVariants] = useState<string[]>([]);
  const [minOrders, setMinOrders] = useState(3);
  const [minGapDays, setMinGapDays] = useState(7);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const isSequence = kind === 'SEQUENCE';
  const isNudge = kind === 'NUDGE';

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    setSaving(true);
    try {
      const automation = await submit({
        name: name.trim(),
        description: description.trim(),
        kind,
        channel: 'SMS',
        segment_id: segmentId ? Number(segmentId) : null,
        recurrence: isSequence || isNudge ? 'ONCE' : recurrence,
        recurrence_day: recurrence === 'ONCE' ? null : recurrenceDay,
        send_time_local: sendTime,
        enrollment_mode: enrollmentMode,
        message_template: messageTemplate,
        stop_on_order: stopOnOrder,
        ends_at: isNudge || !endsAt ? null : new Date(endsAt).toISOString(),
        steps: isSequence ? steps : [],
        trigger_type: isSequence ? triggerType : 'SEGMENT_ENTRY',
        message_variants: isSequence || isNudge ? [] : variants.filter((v) => v.trim()),
        config: isNudge ? { min_orders: minOrders, min_gap_days: minGapDays } : {},
      });
      onCreated(automation);
    } catch (caught) {
      const message = (caught as Error).message;
      setError(message);
      notify(message, 'error');
    } finally {
      setSaving(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      <div>
        <label className="label" htmlFor="automation-name">
          Name
        </label>
        <input
          id="automation-name"
          className="input"
          value={name}
          onChange={(event) => setName(event.target.value)}
          required
          maxLength={200}
          placeholder="Winter reorder reminder"
        />
      </div>

      <div>
        <label className="label" htmlFor="automation-description">
          Description
        </label>
        <input
          id="automation-description"
          className="input"
          value={description}
          onChange={(event) => setDescription(event.target.value)}
          placeholder="What this automation is for"
        />
      </div>

      <div>
        <label className="label" htmlFor="automation-segment">
          Audience
        </label>
        <select
          id="automation-segment"
          className="input"
          value={segmentId}
          onChange={(event) => setSegmentId(event.target.value)}
          required
        >
          <option value="">Choose a segment…</option>
          {(segments ?? []).map((segment) => (
            <option key={segment.id} value={segment.id}>
              {segment.name} ({segment.member_count} members)
            </option>
          ))}
        </select>
        <p className="mt-1 text-xs text-slate-500">
          The segment is re-evaluated at send time, so a recurring campaign always reaches whoever
          matches today — not whoever matched when it was written.
        </p>
      </div>

      {!isSequence && !isNudge && (
        <div className="grid gap-4 sm:grid-cols-3">
          <div>
            <label className="label" htmlFor="automation-recurrence">
              Repeats
            </label>
            <select
              id="automation-recurrence"
              className="input"
              value={recurrence}
              onChange={(event) => setRecurrence(event.target.value)}
            >
              <option value="ONCE">Once</option>
              <option value="DAILY">Daily</option>
              <option value="WEEKLY">Weekly</option>
              <option value="MONTHLY">Monthly</option>
            </select>
          </div>
          {recurrence === 'WEEKLY' && (
            <div>
              <label className="label" htmlFor="automation-weekday">
                On
              </label>
              <select
                id="automation-weekday"
                className="input"
                value={recurrenceDay}
                onChange={(event) => setRecurrenceDay(Number(event.target.value))}
              >
                {WEEKDAYS.map((day, index) => (
                  <option key={day} value={index}>
                    {day}
                  </option>
                ))}
              </select>
            </div>
          )}
          {recurrence === 'MONTHLY' && (
            <div>
              <label className="label" htmlFor="automation-monthday">
                Day of month
              </label>
              <input
                id="automation-monthday"
                type="number"
                min={1}
                max={31}
                className="input"
                value={recurrenceDay || 1}
                onChange={(event) => setRecurrenceDay(Number(event.target.value))}
              />
            </div>
          )}
          <div>
            <label className="label" htmlFor="automation-time">
              Send at (NZ time)
            </label>
            <input
              id="automation-time"
              type="time"
              className="input"
              value={sendTime}
              onChange={(event) => setSendTime(event.target.value)}
            />
          </div>
        </div>
      )}

      {isSequence && (
        <>
          <div>
            <label className="label" htmlFor="automation-trigger">
              Start the clock from
            </label>
            <select
              id="automation-trigger"
              className="input"
              value={triggerType}
              onChange={(event) => setTriggerType(event.target.value)}
            >
              <option value="SEGMENT_ENTRY">When they join the audience</option>
              <option value="SIGNUP">Their signup date</option>
              <option value="LAST_ORDER">Their last order</option>
              <option value="MANUAL">Nobody automatically — I’ll enrol them</option>
            </select>
            <p className="mt-1 text-xs text-slate-500">
              Step offsets are counted from this moment. A signup or last-order trigger can
              be weeks in the past, so steps that came due more than three days before
              somebody joined are skipped rather than fired at them all at once.
            </p>
          </div>

          <div>
            <label className="label" htmlFor="automation-enrollment">
              Enrollment
            </label>
            <select
              id="automation-enrollment"
              className="input"
              value={enrollmentMode}
              onChange={(event) => setEnrollmentMode(event.target.value)}
            >
              <option value="ROLLING">Rolling — new matching customers join as they qualify</option>
              <option value="FIXED_COHORT">Fixed cohort — the audience is locked at launch</option>
            </select>
          </div>

          <fieldset className="rounded-lg border border-slate-200 p-3">
            <legend className="px-1 text-xs font-medium text-slate-600">
              Steps (timed from each customer’s own enrollment)
            </legend>
            <div className="space-y-3">
              {steps.map((step, index) => (
                <div key={index} className="grid gap-2 sm:grid-cols-[6rem,1fr,auto]">
                  <div>
                    <label className="label" htmlFor={`step-offset-${index}`}>
                      Day
                    </label>
                    <input
                      id={`step-offset-${index}`}
                      type="number"
                      min={0}
                      max={365}
                      className="input"
                      value={step.offset_days}
                      onChange={(event) =>
                        setSteps((current) =>
                          current.map((entry, i) =>
                            i === index
                              ? { ...entry, offset_days: Number(event.target.value) }
                              : entry,
                          ),
                        )
                      }
                    />
                  </div>
                  <div>
                    <label className="label" htmlFor={`step-body-${index}`}>
                      Message
                    </label>
                    <textarea
                      id={`step-body-${index}`}
                      className="input"
                      rows={2}
                      value={step.message_template}
                      onChange={(event) =>
                        setSteps((current) =>
                          current.map((entry, i) =>
                            i === index
                              ? { ...entry, message_template: event.target.value }
                              : entry,
                          ),
                        )
                      }
                      placeholder="Hi {first_name}, … Reply STOP to opt out."
                    />
                  </div>
                  <div className="flex items-end">
                    <button
                      type="button"
                      className="btn-ghost"
                      onClick={() => setSteps((current) => current.filter((_, i) => i !== index))}
                      disabled={steps.length === 1}
                      aria-label={`Remove step ${index + 1}`}
                    >
                      Remove
                    </button>
                  </div>
                </div>
              ))}
            </div>
            <button
              type="button"
              className="btn-secondary mt-3"
              onClick={() =>
                setSteps((current) => [
                  ...current,
                  {
                    name: `Step ${current.length + 1}`,
                    offset_days: (current[current.length - 1]?.offset_days ?? 0) + 7,
                    message_template: '',
                  },
                ])
              }
            >
              Add step
            </button>
          </fieldset>

          <label className="flex items-center gap-2 text-sm text-slate-700">
            <input
              type="checkbox"
              checked={stopOnOrder}
              onChange={(event) => setStopOnOrder(event.target.checked)}
            />
            Stop messaging a customer once they place an order
          </label>

          <div>
            <label className="label" htmlFor="automation-ends">
              End date (optional)
            </label>
            <input
              id="automation-ends"
              type="date"
              className="input"
              value={endsAt}
              onChange={(event) => setEndsAt(event.target.value)}
            />
          </div>
        </>
      )}

      {isNudge && (
        <fieldset className="rounded-lg border border-slate-200 p-3">
          <legend className="px-1 text-xs font-medium text-slate-600">
            When each customer is nudged
          </legend>
          <p className="mb-3 text-xs text-slate-500">
            Timed from each customer’s own order history — the day of the week and the
            time of day they usually buy. Customers without enough history are simply
            not enrolled, and join automatically once they have ordered enough times.
          </p>
          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <label className="label" htmlFor="nudge-min-orders">
                Minimum completed orders
              </label>
              <input
                id="nudge-min-orders"
                type="number"
                min={2}
                max={20}
                className="input"
                value={minOrders}
                onChange={(event) => setMinOrders(Number(event.target.value))}
              />
              <p className="mt-1 text-xs text-slate-500">
                Below three, a repeated weekday is a one-in-seven coincidence rather than
                a habit.
              </p>
            </div>
            <div>
              <label className="label" htmlFor="nudge-min-gap">
                Minimum days between nudges
              </label>
              <input
                id="nudge-min-gap"
                type="number"
                min={1}
                max={90}
                className="input"
                value={minGapDays}
                onChange={(event) => setMinGapDays(Number(event.target.value))}
              />
              <p className="mt-1 text-xs text-slate-500">
                A weekly buyer gets a weekly nudge; a monthly buyer should not get four.
              </p>
            </div>
          </div>
          <p className="mt-3 text-xs text-slate-500">
            This runs indefinitely until a customer opts out. A discount is included only
            where their own history justifies it <em>and</em> an approved promotion exists
            in Brand settings.
          </p>
        </fieldset>
      )}

      {!isSequence && (
        <div>
          <label className="label" htmlFor="automation-template">
            Message {isNudge ? '(leave blank to use the standard nudge copy)' : '(leave blank to use the segment’s default copy)'}
          </label>
          <textarea
            id="automation-template"
            className="input"
            rows={3}
            value={messageTemplate}
            onChange={(event) => setMessageTemplate(event.target.value)}
            placeholder="Hi {first_name}, … Reply STOP to opt out."
          />
          <p className="mt-1 text-xs text-slate-500">
            Available placeholders: {'{first_name}'}, {'{city}'}, {'{website}'},{' '}
            {'{delivery_promise}'}
            {isNudge ? ', {usual_day}, {usual_category}, {offer_line}' : ''}
          </p>
        </div>
      )}

      {!isSequence && !isNudge && (
        <fieldset className="rounded-lg border border-slate-200 p-3">
          <legend className="px-1 text-xs font-medium text-slate-600">
            Wording variants (optional)
          </legend>
          <p className="mb-3 text-xs text-slate-500">
            Split the audience across alternative wordings. Each customer is assigned by
            their id, not at random, so the preview shows exactly what they will get.
          </p>
          {variants.map((variant, index) => (
            <div key={index} className="mb-2">
              <label className="label" htmlFor={`variant-${index}`}>
                Variant {String.fromCharCode(65 + index)}
              </label>
              <textarea
                id={`variant-${index}`}
                className="input"
                rows={2}
                value={variant}
                onChange={(event) =>
                  setVariants((current) =>
                    current.map((entry, i) => (i === index ? event.target.value : entry)),
                  )
                }
                placeholder="Hi {first_name}, … Reply STOP to opt out."
              />
            </div>
          ))}
          <div className="flex gap-2">
            <button
              type="button"
              className="btn-secondary"
              onClick={() => setVariants((current) => [...current, ''])}
              disabled={variants.length >= 10}
            >
              Add variant
            </button>
            {variants.length > 0 && (
              <button
                type="button"
                className="btn-ghost"
                onClick={() => setVariants((current) => current.slice(0, -1))}
              >
                Remove last
              </button>
            )}
          </div>
        </fieldset>
      )}

      {error && (
        <p className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700" role="alert">
          {error}
        </p>
      )}

      <div className="flex justify-end gap-2 pt-2">
        <button type="button" className="btn-secondary" onClick={onCancel}>
          Cancel
        </button>
        <button type="submit" className="btn-primary" disabled={saving}>
          {saving && <Spinner className="h-4 w-4" />}
          Create as draft
        </button>
      </div>
    </form>
  );
}
