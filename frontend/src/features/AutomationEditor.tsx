import { useState } from 'react';

import { api } from '../api/client';
import { Spinner, notify } from '../components/ui';
import type { Automation, AutomationStep } from '../types';

type StepDraft = { name: string; offset_days: number; message_template: string };

const NUDGE_TOKENS = '{usual_day}, {usual_category}, {offer_line}';
const COMMON_TOKENS = '{first_name}, {city}, {website}, {delivery_promise}';

/**
 * Edit an automation's copy after it has been created.
 *
 * Changing what would be sent withdraws approval on the backend, so this says
 * so before saving rather than letting an operator discover it when the
 * automation quietly stops running.
 */
export default function AutomationEditor({
  automation,
  onSaved,
  onCancel,
}: {
  automation: Automation;
  onSaved: () => void;
  onCancel: () => void;
}) {
  const [name, setName] = useState(automation.name);
  const [description, setDescription] = useState(automation.description);
  const [messageTemplate, setMessageTemplate] = useState(automation.message_template);
  const [steps, setSteps] = useState<StepDraft[]>(
    automation.steps.map((step: AutomationStep) => ({
      name: step.name,
      offset_days: step.offset_days,
      message_template: step.message_template,
    })),
  );
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const isSequence = automation.kind === 'SEQUENCE';
  const isNudge = automation.kind === 'NUDGE';
  const base = `/api/v1/automations/${automation.id}`;

  const copyChanged = isSequence
    ? JSON.stringify(steps) !==
      JSON.stringify(
        automation.steps.map((s) => ({
          name: s.name,
          offset_days: s.offset_days,
          message_template: s.message_template,
        })),
      )
    : messageTemplate !== automation.message_template;

  const willNeedReapproval =
    copyChanged && automation.require_approval && automation.approved_at !== null;

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    setSaving(true);
    try {
      await api.patch<Automation>(base, {
        name: name.trim(),
        description: description.trim(),
        ...(isSequence ? {} : { message_template: messageTemplate }),
      });
      if (isSequence && copyChanged) {
        await api.put<Automation>(`${base}/steps`, steps);
      }
      notify(
        willNeedReapproval
          ? 'Saved. The copy changed, so this needs approving again before it can send.'
          : 'Saved.',
      );
      onSaved();
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
        <label className="label" htmlFor="edit-name">
          Name
        </label>
        <input
          id="edit-name"
          className="input"
          value={name}
          onChange={(event) => setName(event.target.value)}
          required
          maxLength={200}
        />
      </div>

      <div>
        <label className="label" htmlFor="edit-description">
          Description
        </label>
        <input
          id="edit-description"
          className="input"
          value={description}
          onChange={(event) => setDescription(event.target.value)}
        />
      </div>

      {isSequence ? (
        <fieldset className="rounded-lg border border-slate-200 p-3">
          <legend className="px-1 text-xs font-medium text-slate-600">Steps</legend>
          <div className="space-y-3">
            {steps.map((step, index) => (
              <div key={index} className="grid gap-2 sm:grid-cols-[6rem,1fr]">
                <div>
                  <label className="label" htmlFor={`edit-step-offset-${index}`}>
                    Day
                  </label>
                  <input
                    id={`edit-step-offset-${index}`}
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
                  <label className="label" htmlFor={`edit-step-body-${index}`}>
                    Message
                  </label>
                  <textarea
                    id={`edit-step-body-${index}`}
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
                  />
                </div>
              </div>
            ))}
          </div>
          <p className="mt-3 text-xs text-slate-500">
            Steps can only be changed while nobody is enrolled — re-timing a sequence under
            customers already partway through it would change what they receive and when.
          </p>
        </fieldset>
      ) : (
        <div>
          <label className="label" htmlFor="edit-template">
            Message
          </label>
          <textarea
            id="edit-template"
            className="input"
            rows={4}
            value={messageTemplate}
            onChange={(event) => setMessageTemplate(event.target.value)}
            placeholder="Hi {first_name}, … Reply STOP to opt out."
          />
          <p className="mt-1 text-xs text-slate-500">
            Placeholders: {COMMON_TOKENS}
            {isNudge ? `, ${NUDGE_TOKENS}` : ''}. Leave blank to use the default copy for
            this automation’s audience.
          </p>
        </div>
      )}

      {willNeedReapproval && (
        <p className="rounded-lg bg-amber-50 px-3 py-2 text-sm text-amber-900 ring-1 ring-amber-200">
          Changing the message withdraws approval, because approval was given for the copy
          that is there now. This automation will pause until somebody approves it again.
        </p>
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
          Save changes
        </button>
      </div>
    </form>
  );
}
