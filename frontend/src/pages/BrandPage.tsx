import { useEffect, useState } from 'react';

import { api } from '../api/client';
import {
  Card,
  ErrorState,
  LoadingState,
  PageHeader,
  SectionTitle,
  Spinner,
  notify,
} from '../components/ui';
import { useMutation, useQuery } from '../hooks/useApi';
import type { BrandSettings } from '../types';

/**
 * Brand settings ground every generated message.
 *
 * The lists here are not decoration: the promotions, coupon codes and products
 * are the *only* ones the LLM may mention and the validator will accept, so
 * the page says so plainly next to each field.
 */
export default function BrandPage() {
  const { data, loading, error, refetch } = useQuery<BrandSettings>('/api/v1/brand');
  const [form, setForm] = useState<BrandSettings | null>(null);
  const [dirty, setDirty] = useState(false);

  useEffect(() => {
    if (data) {
      setForm(data);
      setDirty(false);
    }
  }, [data?.updated_at]);

  const save = useMutation(async (payload: Partial<BrandSettings>) =>
    api.put<BrandSettings>('/api/v1/brand', payload),
  );

  if (loading) return <LoadingState label="Loading brand settings…" />;
  if (error) return <ErrorState message={error} onRetry={refetch} />;
  if (!form) return null;

  function set<K extends keyof BrandSettings>(key: K, value: BrandSettings[K]) {
    setForm((current) => (current ? { ...current, [key]: value } : current));
    setDirty(true);
  }

  return (
    <>
      <PageHeader
        title="Brand"
        description="These settings ground every generated message. Anything not listed here, the model may not say."
        actions={
          <button
            type="button"
            className="btn-primary"
            disabled={!dirty || save.loading}
            onClick={async () => {
              const { id, updated_at, ...payload } = form!;
              const result = await save.run(payload);
              if (result) {
                notify('Brand settings saved. Message grounding updated.');
                refetch();
              }
            }}
          >
            {save.loading && <Spinner className="h-4 w-4 text-white" />}
            Save changes
          </button>
        }
      />

      {save.error && (
        <p className="mb-4 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
          {save.error}
        </p>
      )}

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Identity">
          <div className="space-y-3">
            <TextField
              label="Company name"
              value={form.company_name}
              onChange={(v) => set('company_name', v)}
            />
            <TextArea
              label="Company description"
              value={form.company_description}
              onChange={(v) => set('company_description', v)}
              rows={3}
            />
            <TextArea
              label="Mission statement"
              value={form.mission_statement}
              onChange={(v) => set('mission_statement', v)}
              rows={2}
            />
            <TextField
              label="Website"
              value={form.website}
              onChange={(v) => set('website', v)}
            />
            <div className="grid gap-3 sm:grid-cols-2">
              <TextField
                label="Customer service email"
                value={form.customer_service_email}
                onChange={(v) => set('customer_service_email', v)}
              />
              <TextField
                label="Customer service phone"
                value={form.customer_service_phone}
                onChange={(v) => set('customer_service_phone', v)}
              />
            </div>
          </div>
        </Card>

        <Card title="Voice & tone">
          <div className="space-y-3">
            <TextArea
              label="Brand voice"
              value={form.brand_voice}
              onChange={(v) => set('brand_voice', v)}
              rows={3}
            />
            <TextField label="Tone" value={form.tone} onChange={(v) => set('tone', v)} />
            <div>
              <label className="label" htmlFor="emoji-usage">
                Emoji usage
              </label>
              <select
                id="emoji-usage"
                className="input"
                value={form.emoji_usage}
                onChange={(e) => set('emoji_usage', e.target.value)}
              >
                <option value="none">None</option>
                <option value="sparing">Sparing</option>
                <option value="liberal">Liberal</option>
              </select>
            </div>
            <ListField
              label="Communication principles"
              hint="One per line. Given to the model as writing rules."
              values={form.communication_principles}
              onChange={(v) => set('communication_principles', v)}
            />
            <ListField
              label="Preferred vocabulary"
              hint="Phrases the brand likes to use."
              values={form.preferred_vocabulary}
              onChange={(v) => set('preferred_vocabulary', v)}
            />
            <ListField
              label="Words to avoid"
              hint="Flagged as a warning if they appear in generated copy."
              values={form.words_to_avoid}
              onChange={(v) => set('words_to_avoid', v)}
            />
          </div>
        </Card>

        <Card
          title="Verified promotions & products"
          description="The only offers and products a generated message may mention. Leave empty for no-offer messaging."
        >
          <div className="space-y-3">
            <ListField
              label="Approved promotions"
              hint='Exact wording, e.g. "10% off your next order". Anything else is blocked as an invented offer.'
              values={form.allowed_promotions}
              onChange={(v) => set('allowed_promotions', v)}
            />
            <ListField
              label="Active coupon codes"
              hint="Any code not on this list blocks the message."
              values={form.active_coupon_codes}
              onChange={(v) => set('active_coupon_codes', v)}
            />
            <div>
              <label className="label" htmlFor="verified-products">
                Verified products
              </label>
              <textarea
                id="verified-products"
                className="input min-h-[110px] font-mono text-xs"
                value={form.verified_products
                  .map((p) => `${p.product_name ?? p.name ?? ''}|${p.price ?? ''}`)
                  .join('\n')}
                onChange={(e) =>
                  set(
                    'verified_products',
                    e.target.value
                      .split('\n')
                      .map((line) => line.trim())
                      .filter(Boolean)
                      .map((line) => {
                        const [product_name, price] = line.split('|');
                        return { product_name: product_name.trim(), price: (price ?? '').trim() };
                      }),
                  )
                }
                placeholder="Steinlager Classic 12pk|$28.99"
              />
              <p className="mt-1 text-xs text-slate-500">
                One per line as <code className="font-mono">name|price</code>. A customer's own
                purchase history is always allowed in addition to this list.
              </p>
            </div>
            <TextField
              label="Delivery promise"
              hint="A message may not claim faster delivery than this."
              value={form.delivery_promise}
              onChange={(v) => set('delivery_promise', v)}
            />
            <ListField
              label="Delivery areas"
              values={form.delivery_areas}
              onChange={(v) => set('delivery_areas', v)}
            />
          </div>
        </Card>

        <Card
          title="Legal & responsible drinking"
          description="Required on every marketing email. Missing the responsible drinking statement blocks a send."
        >
          <div className="space-y-3">
            <TextField
              label="Responsible drinking statement"
              value={form.responsible_drinking_statement}
              onChange={(v) => set('responsible_drinking_statement', v)}
            />
            <TextField
              label="Age restriction statement"
              value={form.age_restriction_statement}
              onChange={(v) => set('age_restriction_statement', v)}
            />
            <TextArea
              label="Legal disclaimer"
              value={form.legal_disclaimer}
              onChange={(v) => set('legal_disclaimer', v)}
              rows={2}
            />
            <div>
              <label className="label" htmlFor="minimum-age">
                Minimum age
              </label>
              <input
                id="minimum-age"
                type="number"
                min={18}
                max={25}
                className="input"
                value={form.minimum_age}
                onChange={(e) => set('minimum_age', Number(e.target.value))}
              />
            </div>
            <ListField
              label="Additional prohibited claims"
              hint="Blocks a message outright if the phrase appears. The built-in alcohol claim rules apply on top of these."
              values={form.prohibited_claims}
              onChange={(v) => set('prohibited_claims', v)}
            />
          </div>
        </Card>

        <Card title="Channel formatting" className="lg:col-span-2">
          <div className="grid gap-4 lg:grid-cols-3">
            <div className="space-y-3">
              <SectionTitle>Email</SectionTitle>
              <TextArea
                label="Email signature"
                value={form.email_signature}
                onChange={(v) => set('email_signature', v)}
                rows={2}
              />
              <NumberField
                label="Max email words"
                value={form.max_email_words}
                onChange={(v) => set('max_email_words', v)}
              />
            </div>
            <div className="space-y-3">
              <SectionTitle>SMS</SectionTitle>
              <TextArea
                label="SMS style guidance"
                value={form.sms_style}
                onChange={(v) => set('sms_style', v)}
                rows={2}
              />
              <NumberField
                label="Max SMS characters"
                value={form.max_sms_characters}
                onChange={(v) => set('max_sms_characters', v)}
              />
            </div>
            <div className="space-y-3">
              <SectionTitle>WhatsApp</SectionTitle>
              <TextArea
                label="WhatsApp closing"
                value={form.whatsapp_closing}
                onChange={(v) => set('whatsapp_closing', v)}
                rows={2}
              />
              <NumberField
                label="Max WhatsApp characters"
                value={form.max_whatsapp_characters}
                onChange={(v) => set('max_whatsapp_characters', v)}
              />
            </div>
          </div>
        </Card>
      </div>
    </>
  );
}

function TextField({
  label,
  value,
  onChange,
  hint,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  hint?: string;
}) {
  const id = label.toLowerCase().replace(/\s+/g, '-');
  return (
    <div>
      <label className="label" htmlFor={id}>
        {label}
      </label>
      <input id={id} className="input" value={value} onChange={(e) => onChange(e.target.value)} />
      {hint && <p className="mt-1 text-xs text-slate-500">{hint}</p>}
    </div>
  );
}

function NumberField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: number;
  onChange: (value: number) => void;
}) {
  const id = label.toLowerCase().replace(/\s+/g, '-');
  return (
    <div>
      <label className="label" htmlFor={id}>
        {label}
      </label>
      <input
        id={id}
        type="number"
        className="input"
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
      />
    </div>
  );
}

function TextArea({
  label,
  value,
  onChange,
  rows = 3,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  rows?: number;
}) {
  const id = label.toLowerCase().replace(/\s+/g, '-');
  return (
    <div>
      <label className="label" htmlFor={id}>
        {label}
      </label>
      <textarea
        id={id}
        rows={rows}
        className="input"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
    </div>
  );
}

function ListField({
  label,
  values,
  onChange,
  hint,
}: {
  label: string;
  values: string[];
  onChange: (values: string[]) => void;
  hint?: string;
}) {
  const id = label.toLowerCase().replace(/\s+/g, '-');
  return (
    <div>
      <label className="label" htmlFor={id}>
        {label}
      </label>
      <textarea
        id={id}
        rows={Math.min(Math.max(values.length + 1, 3), 8)}
        className="input"
        value={values.join('\n')}
        onChange={(e) =>
          onChange(
            e.target.value
              .split('\n')
              .map((v) => v.trim())
              .filter(Boolean),
          )
        }
      />
      <p className="mt-1 text-xs text-slate-500">{hint ?? 'One per line.'}</p>
    </div>
  );
}
