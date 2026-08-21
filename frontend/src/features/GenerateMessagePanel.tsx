import { useState } from 'react';

import { api } from '../api/client';
import {
  Badge,
  EmptyState,
  SectionTitle,
  Spinner,
  notify,
} from '../components/ui';
import { useMutation, useQuery } from '../hooks/useApi';
import type { Channel, Message } from '../types';
import { humanize } from '../utils/format';
import { MESSAGE_STATUS_BADGE } from '../utils/theme';

const CHANNELS: Channel[] = ['EMAIL', 'SMS', 'WHATSAPP', 'PUSH'];

const VARIATION_LABELS: Record<string, string> = {
  default: 'Default',
  shorter: 'Shorter',
  warmer: 'Warmer',
  more_personal: 'More personal',
  more_playful: 'More playful',
  more_premium: 'More premium',
  remove_sales_language: 'Remove sales language',
};

/**
 * Generate, refine, edit, validate and approve one grounded message.
 *
 * Used both inside the Customer 360 modal and as the body of Message Studio.
 */
export default function GenerateMessagePanel({
  customerId,
  defaultObjective = '',
  onGenerated,
}: {
  customerId: number;
  defaultObjective?: string;
  onGenerated?: (message: Message) => void;
}) {
  const [channel, setChannel] = useState<Channel>('EMAIL');
  const [objective, setObjective] = useState(defaultObjective);
  const [message, setMessage] = useState<Message | null>(null);
  const [draftSubject, setDraftSubject] = useState('');
  const [draftBody, setDraftBody] = useState('');
  const [dirty, setDirty] = useState(false);

  const { data: variations } = useQuery<{ variations: { key: string; instruction: string }[] }>(
    '/api/v1/messages/variations',
  );

  const generate = useMutation(async (variation: string) => {
    const result = await api.post<Message>('/api/v1/messages/generate', {
      customer_id: customerId,
      channel,
      objective,
      variation,
    });
    setMessage(result);
    setDraftSubject(result.subject);
    setDraftBody(result.body);
    setDirty(false);
    onGenerated?.(result);
    return result;
  });

  const save = useMutation(async () => {
    if (!message) return null;
    const result = await api.patch<Message>(`/api/v1/messages/${message.id}`, {
      subject: draftSubject,
      body: draftBody,
    });
    setMessage(result);
    setDirty(false);
    return result;
  });

  const approve = useMutation(async () => {
    if (!message) return null;
    const result = await api.post<Message>(`/api/v1/messages/${message.id}/approve`);
    setMessage(result);
    return result;
  });

  const reject = useMutation(async () => {
    if (!message) return null;
    const result = await api.post<Message>(`/api/v1/messages/${message.id}/reject`);
    setMessage(result);
    return result;
  });

  const sendTest = useMutation(async (to: string) => {
    if (!message) return null;
    return api.post<{ success: boolean; is_simulated: boolean; error: string | null }>(
      `/api/v1/messages/${message.id}/send-test`,
      { to },
    );
  });

  const validation = message?.validation_result;
  const canApprove = Boolean(message) && validation?.valid === true && !dirty;

  return (
    <div className="space-y-5">
      <div className="grid gap-3 sm:grid-cols-2">
        <div>
          <label className="label" htmlFor="gen-channel">
            Channel
          </label>
          <select
            id="gen-channel"
            className="input"
            value={channel}
            onChange={(e) => setChannel(e.target.value as Channel)}
          >
            {CHANNELS.map((c) => (
              <option key={c} value={c}>
                {humanize(c)}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className="label" htmlFor="gen-objective">
            Objective
          </label>
          <input
            id="gen-objective"
            className="input"
            placeholder="e.g. REACTIVATION"
            value={objective}
            onChange={(e) => setObjective(e.target.value)}
          />
        </div>
      </div>

      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          className="btn-primary"
          onClick={() => generate.run('default')}
          disabled={generate.loading}
        >
          {generate.loading && <Spinner className="h-4 w-4 text-white" />}
          {message ? 'Regenerate' : 'Generate'}
        </button>
        {message &&
          (variations?.variations ?? [])
            .filter((v) => v.key !== 'default')
            .map((v) => (
              <button
                key={v.key}
                type="button"
                className="btn-secondary"
                title={v.instruction}
                onClick={() => generate.run(v.key)}
                disabled={generate.loading}
              >
                {VARIATION_LABELS[v.key] ?? humanize(v.key)}
              </button>
            ))}
      </div>

      {generate.error && (
        <p className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
          {generate.error}
        </p>
      )}

      {!message ? (
        <div className="rounded-lg border border-dashed border-slate-300">
          <EmptyState
            title="No message generated yet"
            description="The model only sees this customer's verified data and your brand settings. It cannot invent offers, products, prices or facts."
          />
        </div>
      ) : (
        <>
          <div className="flex flex-wrap items-center gap-2">
            <Badge
              className={
                MESSAGE_STATUS_BADGE[message.status] ?? 'bg-slate-100 text-slate-700 ring-slate-200'
              }
            >
              {humanize(message.status)}
            </Badge>
            <Badge className="bg-slate-100 text-slate-600 ring-slate-200">
              {message.llm_provider} · {message.llm_model}
            </Badge>
            <Badge className="bg-slate-100 text-slate-600 ring-slate-200">
              prompt {message.prompt_version}
            </Badge>
            {message.was_edited && (
              <Badge className="bg-indigo-50 text-indigo-700 ring-indigo-200">Edited</Badge>
            )}
            {dirty && (
              <Badge className="bg-amber-50 text-amber-800 ring-amber-200">Unsaved changes</Badge>
            )}
          </div>

          {channel === 'EMAIL' && (
            <div>
              <label className="label" htmlFor="draft-subject">
                Subject
              </label>
              <input
                id="draft-subject"
                className="input"
                value={draftSubject}
                onChange={(e) => {
                  setDraftSubject(e.target.value);
                  setDirty(true);
                }}
              />
            </div>
          )}

          <div>
            <label className="label" htmlFor="draft-body">
              Body
            </label>
            <textarea
              id="draft-body"
              className="input min-h-[220px] font-sans leading-relaxed"
              value={draftBody}
              onChange={(e) => {
                setDraftBody(e.target.value);
                setDirty(true);
              }}
            />
            <p className="mt-1 text-xs text-slate-500">
              {draftBody.length} characters · {draftBody.trim().split(/\s+/).filter(Boolean).length}{' '}
              words
            </p>
          </div>

          <ValidationPanel result={validation} dirty={dirty} />

          <div className="flex flex-wrap items-center gap-2 border-t border-slate-200 pt-4">
            <button
              type="button"
              className="btn-secondary"
              onClick={async () => {
                const result = await save.run();
                if (result) {
                  notify(
                    result.validation_result.valid
                      ? 'Saved and revalidated.'
                      : 'Saved. Validation is failing — see the findings above.',
                    result.validation_result.valid ? 'success' : 'error',
                  );
                }
              }}
              disabled={!dirty || save.loading}
            >
              {save.loading && <Spinner className="h-4 w-4" />}
              Save & revalidate
            </button>

            <button
              type="button"
              className="btn-secondary"
              onClick={async () => {
                const to = window.prompt('Send a test message to which address or number?');
                if (!to) return;
                const result = await sendTest.run(to);
                if (result) {
                  notify(
                    result.success
                      ? `Test message sent${result.is_simulated ? ' (simulated)' : ''}.`
                      : `Test send failed: ${result.error}`,
                    result.success ? 'success' : 'error',
                  );
                }
              }}
              disabled={sendTest.loading}
            >
              {sendTest.loading && <Spinner className="h-4 w-4" />}
              Send test
            </button>

            <div className="flex-1" />

            <button
              type="button"
              className="btn-secondary"
              onClick={async () => {
                await reject.run();
                notify('Message rejected.', 'info');
              }}
              disabled={reject.loading}
            >
              Reject
            </button>
            <button
              type="button"
              className="btn-primary"
              onClick={async () => {
                const result = await approve.run();
                if (result) notify('Message approved.');
              }}
              disabled={!canApprove || approve.loading}
              title={
                dirty
                  ? 'Save your edits before approving.'
                  : validation?.valid === false
                    ? 'Validation must pass before this message can be approved.'
                    : undefined
              }
            >
              {approve.loading && <Spinner className="h-4 w-4 text-white" />}
              Approve
            </button>
          </div>

          {(approve.error || save.error) && (
            <p className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
              {approve.error ?? save.error}
            </p>
          )}
        </>
      )}
    </div>
  );
}

export function ValidationPanel({
  result,
  dirty,
}: {
  result: Message['validation_result'] | undefined;
  dirty?: boolean;
}) {
  if (!result) return null;

  if (dirty) {
    return (
      <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3">
        <p className="text-sm font-medium text-amber-800">Unsaved changes</p>
        <p className="mt-0.5 text-xs text-amber-700">
          Save to revalidate. A message cannot be approved while edits are unvalidated.
        </p>
      </div>
    );
  }

  const errors = result.errors ?? [];
  const warnings = result.warnings ?? [];

  if (result.valid && warnings.length === 0) {
    return (
      <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-3">
        <p className="text-sm font-medium text-emerald-800">Validation passed</p>
        <p className="mt-0.5 text-xs text-emerald-700">
          Every claim is backed by verified customer data, brand settings or an approved promotion.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {errors.length > 0 && (
        <div className="rounded-lg border border-red-200 bg-red-50 px-4 py-3">
          <p className="text-sm font-medium text-red-800">
            {errors.length} blocking {errors.length === 1 ? 'issue' : 'issues'} — cannot be approved
          </p>
          <ul className="mt-2 space-y-1.5">
            {errors.map((f, index) => (
              <li key={`${f.code}-${index}`} className="text-xs text-red-700">
                <span className="font-mono font-medium">{f.code}</span> — {f.message}
                {f.excerpt && (
                  <span className="ml-1 rounded bg-red-100 px-1 py-0.5 font-mono">
                    “{f.excerpt}”
                  </span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      {warnings.length > 0 && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3">
          <SectionTitle>Warnings ({warnings.length})</SectionTitle>
          <ul className="space-y-1.5">
            {warnings.map((f, index) => (
              <li key={`${f.code}-${index}`} className="text-xs text-amber-800">
                <span className="font-mono font-medium">{f.code}</span> — {f.message}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
