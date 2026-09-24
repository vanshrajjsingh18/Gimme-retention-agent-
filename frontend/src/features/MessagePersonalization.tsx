import { useEffect, useRef, useState } from 'react';
import type { RefObject } from 'react';

import { api } from '../api/client';
import { useDebounced, useQuery } from '../hooks/useApi';

/**
 * The Personalize menu and the live preview that sits under a message box.
 *
 * Two things make this worth a component rather than a panel inside the page.
 * The insert has to land where the cursor is — appending to the end is the
 * behaviour that makes somebody give up on merge tags and type the customer's
 * name by hand. And the preview has to come from the server: rendering the
 * tags in the browser would mean a second implementation of the field
 * mapping, which is exactly how a composer ends up promising a value the
 * sender does not fill.
 */
export interface MessageField {
  token: string;
  label: string;
  group: string;
  example: string;
  fallback: string;
  description: string;
  tag: string;
}

interface FieldsResponse {
  fields: MessageField[];
  groups: string[];
  syntax: string;
  sample_customers: { id: number; name: string }[];
}

interface PreviewResponse {
  text: string;
  template: string;
  missing_fields: string[];
  fallbacks_used: string[];
  unknown_tags: string[];
  valid_tags: string[];
  unfillable: string[];
  ok: boolean;
  customer_id: number | null;
  customer_name: string;
  characters: number;
}

interface Props {
  value: string;
  onChange: (next: string) => void;
  textareaRef: RefObject<HTMLTextAreaElement | null>;
  disabled?: boolean;
  /** Hidden when the model writes each message — there is no template to tag. */
  hidden?: boolean;
}

export default function MessagePersonalization({
  value,
  onChange,
  textareaRef,
  disabled = false,
  hidden = false,
}: Props) {
  const { data } = useQuery<FieldsResponse>('/api/v1/message-fields');
  const [customerId, setCustomerId] = useState<number | null>(null);
  const [preview, setPreview] = useState<PreviewResponse | null>(null);
  const [caret, setCaret] = useState<number | null>(null);
  const debounced = useDebounced(value, 400);
  const latest = useRef(0);

  // Put the cursor back after the text the operator just inserted, so they can
  // keep typing the sentence. Done from an effect rather than inline because
  // the textarea still holds the pre-insert value until React has re-rendered.
  useEffect(() => {
    if (caret === null) return;
    const el = textareaRef.current;
    if (el) {
      el.focus();
      el.setSelectionRange(caret, caret);
    }
    setCaret(null);
  }, [caret, textareaRef]);

  useEffect(() => {
    if (hidden || !debounced.trim()) {
      setPreview(null);
      return;
    }
    const request = ++latest.current;
    api
      .post<PreviewResponse>('/api/v1/message-fields/preview', {
        template: debounced,
        customer_id: customerId,
      })
      .then((result) => {
        // Normalised on arrival. The panel sits inside the composer, and a
        // response missing a field should degrade to "no warnings", never
        // take the page down with it while somebody is mid-sentence.
        if (request === latest.current) {
          setPreview({
            ...result,
            text: result?.text ?? '',
            missing_fields: result?.missing_fields ?? [],
            fallbacks_used: result?.fallbacks_used ?? [],
            unknown_tags: result?.unknown_tags ?? [],
            valid_tags: result?.valid_tags ?? [],
            unfillable: result?.unfillable ?? [],
          });
        }
      })
      .catch(() => {
        if (request === latest.current) setPreview(null);
      });
  }, [debounced, customerId, hidden]);

  if (hidden) return null;

  const fields = data?.fields ?? [];
  const samples = data?.sample_customers ?? [];

  function insert(tag: string) {
    const el = textareaRef.current;
    const start = el?.selectionStart ?? value.length;
    const end = el?.selectionEnd ?? start;
    onChange(value.slice(0, start) + tag + value.slice(end));
    setCaret(start + tag.length);
  }

  const byGroup = fields.reduce<Record<string, MessageField[]>>((acc, field) => {
    (acc[field.group] ||= []).push(field);
    return acc;
  }, {});

  return (
    <div className="mt-3 space-y-3">
      <div className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2.5">
        <div className="flex flex-wrap items-center gap-2">
          <label className="text-xs font-medium text-slate-700" htmlFor="personalize-field">
            Personalize
          </label>
          <select
            id="personalize-field"
            aria-label="Insert a merge tag"
            className="input h-8 w-auto py-0 text-xs"
            value=""
            disabled={disabled || fields.length === 0}
            onChange={(e) => {
              if (e.target.value) insert(e.target.value);
              e.target.value = '';
            }}
          >
            <option value="">Insert a field…</option>
            {Object.entries(byGroup).map(([group, groupFields]) => (
              <optgroup key={group} label={group}>
                {groupFields.map((field) => (
                  <option key={field.token} value={field.tag}>
                    {field.label} — {field.tag}
                  </option>
                ))}
              </optgroup>
            ))}
          </select>
          <span className="text-xs text-slate-500">
            Inserted where your cursor is. You can also type them by hand.
          </span>
        </div>

        {/* The common few stay one tap away. The dropdown holds everything;
            this row is for the tags almost every message uses. */}
        <div className="mt-2 flex flex-wrap gap-1.5">
          {fields.slice(0, 6).map((field) => (
            <button
              key={field.token}
              type="button"
              title={`${field.label} — e.g. ${field.example}`}
              disabled={disabled}
              className="rounded-md border border-slate-300 bg-white px-2 py-1 font-mono text-xs text-slate-700 hover:border-blue-400 hover:text-blue-700 disabled:opacity-50"
              onClick={() => insert(field.tag)}
            >
              {field.tag}
            </button>
          ))}
        </div>
      </div>

      <div className="rounded-lg border border-slate-200 bg-white px-3 py-2.5">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-xs font-medium text-slate-700">Preview</p>
          <select
            aria-label="Preview as customer"
            className="input h-8 w-auto py-0 text-xs"
            value={customerId ?? ''}
            onChange={(e) => setCustomerId(e.target.value ? Number(e.target.value) : null)}
          >
            <option value="">Sample customer</option>
            {samples.map((customer) => (
              <option key={customer.id} value={customer.id}>
                {customer.name}
              </option>
            ))}
          </select>
        </div>

        {!preview && (
          <p className="mt-2 text-xs text-slate-400">
            Write a message above to see how it will read.
          </p>
        )}

        {preview && (
          <>
            <p className="mt-2 whitespace-pre-wrap rounded-md bg-slate-50 px-3 py-2 text-sm leading-relaxed text-slate-800">
              {preview.text}
            </p>
            <p className="mt-1.5 text-xs text-slate-400">
              As {preview.customer_name} would receive it · {preview.characters} characters
            </p>

            {/* Tag by tag, because "a merge tag is wrong" in a 400-character
                body is not something anybody can act on. */}
            {(preview.valid_tags.length > 0 || preview.unknown_tags.length > 0) && (
              <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1">
                {preview.valid_tags.map((tag) => (
                  <span key={tag} className="font-mono text-xs text-emerald-700">
                    ✓ #{tag}#
                  </span>
                ))}
                {preview.unknown_tags.map((tag) => (
                  <span key={tag} className="font-mono text-xs text-red-700">
                    ✕ #{tag}# — unknown field
                  </span>
                ))}
              </div>
            )}

            {/* An unknown tag is the one thing here that stops a send, so it
                is stated as a blocker rather than as a note. */}
            {preview.unknown_tags.length > 0 && (
              <p className="mt-2 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
                {preview.unknown_tags.map((tag) => `#${tag}#`).join(', ')}{' '}
                {preview.unknown_tags.length === 1 ? 'is not a field' : 'are not fields'} this
                system can fill. The campaign cannot be approved until it is fixed.
              </p>
            )}

            {/* A gap no fallback can close is not a note, it is a customer
                who will be skipped — so it is said in those words. */}
            {preview.unknown_tags.length === 0 && preview.unfillable.length > 0 && (
              <p className="mt-2 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900">
                {preview.unfillable.map((tag) => `#${tag}#`).join(', ')} has no value for this
                customer and no fallback to stand in, so they would be skipped rather than sent
                a message with a gap in it.
              </p>
            )}

            {preview.unknown_tags.length === 0 &&
              preview.unfillable.length === 0 &&
              preview.fallbacks_used.length > 0 && (
                <p className="mt-2 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
                  This customer has nothing for{' '}
                  {preview.fallbacks_used.map((tag) => `#${tag}#`).join(', ')}. A fallback was
                  used above — other customers will see their own details.
                </p>
              )}
          </>
        )}
      </div>
    </div>
  );
}
