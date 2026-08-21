import { useRef, useState } from 'react';

import { api, downloadFile } from '../api/client';
import {
  Badge,
  Card,
  EmptyState,
  LoadingState,
  Modal,
  PageHeader,
  SectionTitle,
  Spinner,
  TableShell,
  notify,
} from '../components/ui';
import { useMutation, useQuery } from '../hooks/useApi';
import type { IngestionJob } from '../types';
import { formatDateTime, formatNumber, humanize } from '../utils/format';

const ENTITY_TYPES = [
  { key: 'customers', label: 'Customers', hint: 'Requires external_id, plus an email or phone.' },
  {
    key: 'orders',
    label: 'Orders',
    hint: 'Requires external_id, customer_external_id, ordered_at, total_amount.',
  },
  {
    key: 'order_items',
    label: 'Order items',
    hint: 'Requires external_id, order_external_id, sku, product_name.',
  },
  { key: 'events', label: 'Events', hint: 'Requires customer_external_id and event_type.' },
  {
    key: 'consent_events',
    label: 'Consent events',
    hint: 'Requires customer_external_id, consent_type, granted.',
  },
] as const;

interface PreviewResult {
  entity_type: string;
  headers: string[];
  total_rows: number;
  missing_required_columns: string[];
  valid: boolean;
  sample_rows: Record<string, string>[];
}

export default function DataPage() {
  const [entityType, setEntityType] = useState<string>('customers');
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<PreviewResult | null>(null);
  const [dragging, setDragging] = useState(false);
  const [lastJob, setLastJob] = useState<IngestionJob | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const { data: jobs, refetch: refetchJobs } = useQuery<IngestionJob[]>('/api/v1/uploads?limit=15');

  const runPreview = useMutation(async (selected: File, type: string) => {
    const form = new FormData();
    form.append('entity_type', type);
    form.append('file', selected);
    const result = await api.upload<PreviewResult>('/api/v1/uploads/preview', form);
    setPreview(result);
    return result;
  });

  const runImport = useMutation(async () => {
    if (!file) return null;
    const form = new FormData();
    form.append('entity_type', entityType);
    form.append('file', file);
    const job = await api.upload<IngestionJob>('/api/v1/uploads', form);
    setLastJob(job);
    setFile(null);
    setPreview(null);
    if (inputRef.current) inputRef.current.value = '';
    refetchJobs();
    return job;
  });

  function selectFile(selected: File | null) {
    setFile(selected);
    setPreview(null);
    if (selected) runPreview.run(selected, entityType);
  }

  const active = ENTITY_TYPES.find((e) => e.key === entityType)!;

  return (
    <>
      <PageHeader
        title="Data & imports"
        description="Upload CSV files or push data through the authenticated ingestion API."
        actions={
          <a href={api.url('/docs')} target="_blank" rel="noreferrer" className="btn-secondary">
            API docs
          </a>
        }
      />

      <div className="grid gap-4 lg:grid-cols-3">
        <Card title="Upload a CSV" className="lg:col-span-2">
          <div className="mb-4">
            <label className="label" htmlFor="entity-type">
              What are you importing?
            </label>
            <div className="flex flex-wrap gap-1.5">
              {ENTITY_TYPES.map((entity) => (
                <button
                  key={entity.key}
                  type="button"
                  onClick={() => {
                    setEntityType(entity.key);
                    setPreview(null);
                    if (file) runPreview.run(file, entity.key);
                  }}
                  aria-pressed={entityType === entity.key}
                  className={`rounded-md px-2.5 py-1 text-xs font-medium ring-1 ring-inset transition-colors ${
                    entityType === entity.key
                      ? 'bg-brand-50 text-brand-700 ring-brand-300'
                      : 'bg-white text-slate-600 ring-slate-200 hover:bg-slate-50'
                  }`}
                >
                  {entity.label}
                </button>
              ))}
            </div>
            <p className="mt-2 text-xs text-slate-500">{active.hint}</p>
          </div>

          <div
            onDragOver={(e) => {
              e.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragging(false);
              const dropped = e.dataTransfer.files?.[0];
              if (dropped) selectFile(dropped);
            }}
            className={`rounded-xl border-2 border-dashed px-6 py-10 text-center transition-colors ${
              dragging ? 'border-brand-400 bg-brand-50' : 'border-slate-300 bg-slate-50'
            }`}
          >
            <input
              ref={inputRef}
              type="file"
              accept=".csv,text/csv"
              className="sr-only"
              id="csv-input"
              onChange={(e) => selectFile(e.target.files?.[0] ?? null)}
            />
            <p className="text-sm text-slate-600">
              Drag a CSV here, or{' '}
              <label
                htmlFor="csv-input"
                className="cursor-pointer font-medium text-brand-600 hover:text-brand-700"
              >
                browse for a file
              </label>
              .
            </p>
            {file && (
              <p className="mt-2 text-xs text-slate-500">
                {file.name} · {(file.size / 1024).toFixed(1)} KB
              </p>
            )}
            <button
              type="button"
              className="btn-ghost mt-3 px-2 py-1 text-xs"
              onClick={() =>
                downloadFile(
                  `/api/v1/uploads/templates/${entityType}.csv`,
                  `${entityType}-template.csv`,
                ).catch(() => notify('Could not download the template.', 'error'))
              }
            >
              Download {active.label.toLowerCase()} template
            </button>
          </div>

          {runPreview.error && (
            <p className="mt-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
              {runPreview.error}
            </p>
          )}

          {runPreview.loading && <LoadingState label="Reading file…" />}

          {preview && (
            <div className="mt-4">
              <SectionTitle>Preview</SectionTitle>
              {!preview.valid ? (
                <div className="rounded-lg border border-red-200 bg-red-50 px-4 py-3">
                  <p className="text-sm font-medium text-red-800">
                    Missing required columns:{' '}
                    {preview.missing_required_columns.join(', ')}
                  </p>
                  <p className="mt-1 text-xs text-red-700">
                    Download the template above to see the expected header row.
                  </p>
                </div>
              ) : (
                <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-3">
                  <p className="text-sm font-medium text-emerald-800">
                    {formatNumber(preview.total_rows)} rows ready to import
                  </p>
                  <p className="mt-0.5 text-xs text-emerald-700">
                    Rows are validated individually — one bad row will not fail the file.
                  </p>
                </div>
              )}

              {preview.sample_rows.length > 0 && (
                <div className="mt-3 overflow-x-auto rounded-lg border border-slate-200">
                  <table className="min-w-full divide-y divide-slate-200 text-xs">
                    <thead className="bg-slate-50">
                      <tr>
                        {preview.headers.slice(0, 8).map((header) => (
                          <th key={header} className="px-3 py-2 text-left font-semibold text-slate-600">
                            {header}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100">
                      {preview.sample_rows.map((row, index) => (
                        <tr key={index}>
                          {preview.headers.slice(0, 8).map((header) => (
                            <td key={header} className="max-w-[160px] truncate px-3 py-1.5 text-slate-700">
                              {row[header] ?? ''}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              <button
                type="button"
                className="btn-primary mt-4"
                disabled={!preview.valid || runImport.loading}
                onClick={async () => {
                  const job = await runImport.run();
                  if (job) {
                    notify(
                      job.status === 'COMPLETED'
                        ? `Imported ${job.accepted_rows} rows (${job.rejected_rows} rejected).`
                        : 'Import failed — see the details.',
                      job.status === 'COMPLETED' ? 'success' : 'error',
                    );
                  }
                }}
              >
                {runImport.loading && <Spinner className="h-4 w-4 text-white" />}
                Import {formatNumber(preview.total_rows)} rows
              </button>
            </div>
          )}
        </Card>

        <ApiKeysCard />
      </div>

      <Card title="Import history" className="mt-4" bodyClassName="">
        {!jobs || jobs.length === 0 ? (
          <EmptyState title="No imports yet" description="Uploaded files appear here." />
        ) : (
          <TableShell>
            <thead className="bg-slate-50">
              <tr>
                <th className="table-head">File</th>
                <th className="table-head">Type</th>
                <th className="table-head">Status</th>
                <th className="table-head text-right">Accepted</th>
                <th className="table-head text-right">Updated</th>
                <th className="table-head text-right">Rejected</th>
                <th className="table-head">When</th>
                <th className="table-head" />
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {jobs.map((job) => (
                <tr key={job.id} className="hover:bg-slate-50">
                  <td className="table-cell">
                    <span className="font-medium text-slate-800">{job.filename || '—'}</span>
                    <p className="text-xs text-slate-500">{humanize(job.source)}</p>
                  </td>
                  <td className="table-cell text-xs">{humanize(job.entity_type)}</td>
                  <td className="table-cell">
                    <Badge
                      className={
                        job.status === 'COMPLETED'
                          ? 'bg-emerald-50 text-emerald-700 ring-emerald-200'
                          : job.status === 'FAILED'
                            ? 'bg-red-50 text-red-700 ring-red-200'
                            : 'bg-slate-100 text-slate-600 ring-slate-200'
                      }
                    >
                      {humanize(job.status)}
                    </Badge>
                  </td>
                  <td className="table-cell text-right tabular-nums">
                    {formatNumber(job.accepted_rows)}
                  </td>
                  <td className="table-cell text-right tabular-nums text-slate-500">
                    {formatNumber(job.updated_rows)}
                  </td>
                  <td className="table-cell text-right tabular-nums">
                    {job.rejected_rows > 0 ? (
                      <span className="text-red-600">{formatNumber(job.rejected_rows)}</span>
                    ) : (
                      '0'
                    )}
                  </td>
                  <td className="table-cell text-xs text-slate-500">
                    {formatDateTime(job.created_at)}
                  </td>
                  <td className="table-cell">
                    {job.rejected_rows > 0 && (
                      <button
                        type="button"
                        className="btn-ghost px-2 py-1 text-xs"
                        onClick={() =>
                          downloadFile(
                            `/api/v1/uploads/${job.id}/errors.csv`,
                            `import-${job.id}-errors.csv`,
                          ).catch(() => notify('Could not download the report.', 'error'))
                        }
                      >
                        Error report
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </TableShell>
        )}
      </Card>

      {lastJob && lastJob.errors.length > 0 && (
        <Modal
          open
          title={`${lastJob.rejected_rows} rows were rejected`}
          description="Every other row was imported. Fix these and re-upload just those rows."
          onClose={() => setLastJob(null)}
          size="lg"
          footer={
            <>
              <button
                type="button"
                className="btn-secondary"
                onClick={() =>
                  downloadFile(
                    `/api/v1/uploads/${lastJob.id}/errors.csv`,
                    `import-${lastJob.id}-errors.csv`,
                  ).catch(() => notify('Could not download the report.', 'error'))
                }
              >
                Download error report
              </button>
              <button type="button" className="btn-primary" onClick={() => setLastJob(null)}>
                Done
              </button>
            </>
          }
        >
          <ul className="space-y-2">
            {lastJob.errors.slice(0, 50).map((error, index) => (
              <li
                key={index}
                className="rounded-lg border border-slate-200 px-3 py-2 text-xs"
              >
                <span className="font-mono font-medium text-slate-700">Row {error.row}</span>
                <span className="ml-2 text-slate-600">{error.error}</span>
                {Object.keys(error.data ?? {}).length > 0 && (
                  <p className="mt-0.5 font-mono text-slate-400">
                    {Object.entries(error.data)
                      .map(([k, v]) => `${k}=${v}`)
                      .join(' · ')}
                  </p>
                )}
              </li>
            ))}
          </ul>
        </Modal>
      )}
    </>
  );
}

function ApiKeysCard() {
  const { data: keys, refetch } = useQuery<
    { id: number; name: string; key_prefix: string; is_active: boolean; created_at: string }[]
  >('/api/v1/api-keys');
  const [newKey, setNewKey] = useState<string | null>(null);
  const [name, setName] = useState('');

  const create = useMutation(async () => {
    const result = await api.post<{ api_key: string }>('/api/v1/api-keys', { name });
    setNewKey(result.api_key);
    setName('');
    refetch();
    return result;
  });

  const revoke = useMutation(async (id: number) => {
    await api.del(`/api/v1/api-keys/${id}`);
    refetch();
  });

  return (
    <Card title="API keys" description="For pushing data from your storefront or ETL.">
      <div className="flex gap-2">
        <input
          className="input"
          placeholder="Key name, e.g. Shopify sync"
          value={name}
          onChange={(e) => setName(e.target.value)}
          aria-label="API key name"
        />
        <button
          type="button"
          className="btn-primary shrink-0"
          disabled={!name.trim() || create.loading}
          onClick={() => create.run()}
        >
          {create.loading && <Spinner className="h-4 w-4 text-white" />}
          Create
        </button>
      </div>

      {newKey && (
        <div className="mt-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2">
          <p className="text-xs font-medium text-amber-800">
            Copy this key now — it is never shown again.
          </p>
          <code className="mt-1 block break-all rounded bg-white px-2 py-1 font-mono text-xs text-slate-800">
            {newKey}
          </code>
          <button
            type="button"
            className="btn-ghost mt-1 px-2 py-1 text-xs"
            onClick={() => {
              navigator.clipboard?.writeText(newKey).then(
                () => notify('API key copied.'),
                () => notify('Could not copy — select the text manually.', 'error'),
              );
            }}
          >
            Copy
          </button>
        </div>
      )}

      <ul className="mt-4 divide-y divide-slate-100">
        {(keys ?? []).map((key) => (
          <li key={key.id} className="flex items-center justify-between gap-2 py-2">
            <div className="min-w-0">
              <p className="truncate text-sm text-slate-800">{key.name}</p>
              <p className="font-mono text-xs text-slate-500">{key.key_prefix}…</p>
            </div>
            {key.is_active ? (
              <button
                type="button"
                className="btn-ghost px-2 py-1 text-xs text-red-600 hover:bg-red-50"
                onClick={() => revoke.run(key.id)}
              >
                Revoke
              </button>
            ) : (
              <Badge className="bg-slate-100 text-slate-500 ring-slate-200">Revoked</Badge>
            )}
          </li>
        ))}
      </ul>

      <div className="mt-4 border-t border-slate-100 pt-3">
        <SectionTitle>Usage</SectionTitle>
        <pre className="overflow-x-auto rounded-lg bg-slate-900 px-3 py-2 text-xs text-slate-100">
          {`curl -X POST ${api.baseUrl}/api/v1/orders \\
  -H "X-API-Key: gimme_sk_..." \\
  -H "Content-Type: application/json" \\
  -d '[{"external_id":"ORD-1", ...}]'`}
        </pre>
      </div>
    </Card>
  );
}
