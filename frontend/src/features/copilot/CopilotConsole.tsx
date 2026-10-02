/**
 * The AI Copilot console: an operational command line for the engine.
 *
 * The model never acts from here directly. Reads come back as tool results;
 * every change arrives as an action card, and only the Confirm button on that
 * card — bound to its id — executes it.
 */
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';

import { ApiError, api, downloadFile } from '../../api/client';
import { Badge, Spinner, notify } from '../../components/ui';
import ActionCard, { RiskBadge } from './ActionCard';
import { SUGGESTED_COMMANDS } from './commands';
import Markdown from './Markdown';
import type {
  ActionOutcome,
  ActiveEntity,
  ConversationSummary,
  ConversationView,
  CopilotMessageView,
  ExecutionView,
} from './types';

const STORAGE_KEY = 'gimme.copilot.conversation';


function readStored(): number | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? Number(raw) || null : null;
  } catch {
    return null;
  }
}

function writeStored(id: number | null) {
  try {
    if (id) localStorage.setItem(STORAGE_KEY, String(id));
    else localStorage.removeItem(STORAGE_KEY);
  } catch {
    /* storage unavailable: the conversation still works for this visit */
  }
}

function errorText(error: unknown): string {
  return error instanceof ApiError ? error.message : 'Something went wrong.';
}

// --------------------------------------------------------------------------
// Pieces
// --------------------------------------------------------------------------
function ToolStep({ message }: { message: CopilotMessageView }) {
  const [open, setOpen] = useState(false);
  const tool = message.tool!;
  const data = tool.data as { download?: { path: string; filename: string } } | null;
  const period = tool.metadata?.period as string | undefined;
  const count = tool.metadata?.count as number | undefined;
  return (
    <div className="text-xs">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="inline-flex max-w-full items-center gap-1.5 rounded-md bg-slate-50 px-2 py-1 text-slate-600 ring-1 ring-inset ring-slate-200 hover:bg-slate-100"
        aria-expanded={open}
      >
        <span className={tool.success ? 'text-emerald-600' : 'text-red-600'}>{tool.success ? '✓' : '✕'}</span>
        <span className="font-mono">{tool.name}</span>
        {tool.risk && tool.risk !== 'READ' && <span className="text-amber-700">· planned</span>}
        {typeof count === 'number' && <span className="text-slate-400">· {count.toLocaleString()} rows</span>}
        {period && <span className="truncate text-slate-400">· {period}</span>}
      </button>
      {!tool.success && tool.errors?.length ? <p className="mt-1 text-red-700">{tool.errors.join('; ')}</p> : null}
      {data?.download && (
        <button
          type="button"
          className="btn-secondary ml-2 px-2 py-1 text-xs"
          onClick={() =>
            downloadFile(data.download!.path, data.download!.filename).catch((e) => notify(errorText(e), 'error'))
          }
        >
          Download CSV
        </button>
      )}
      {open && (
        <pre className="mt-1 max-h-72 overflow-auto rounded-lg bg-slate-50 p-2 text-[11px] text-slate-700 ring-1 ring-inset ring-slate-200">
          {JSON.stringify({ arguments: tool.arguments, metadata: tool.metadata, data: tool.data }, null, 2)}
        </pre>
      )}
    </div>
  );
}

function AssistantMessage({ message }: { message: CopilotMessageView }) {
  const copy = () => {
    navigator.clipboard?.writeText(message.content).then(
      () => notify('Copied', 'info'),
      () => notify('Copy failed', 'error'),
    );
  };
  return (
    <div className="group flex gap-3">
      <div className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-brand-600 text-xs font-bold text-white">
        AI
      </div>
      <div className={`min-w-0 flex-1 rounded-xl px-4 py-2.5 ring-1 ${message.meta?.error ? 'bg-red-50 ring-red-200' : 'bg-white ring-slate-200'}`}>
        <Markdown text={message.content} />
        <div className="mt-1 flex items-center gap-3 text-[11px] text-slate-400 opacity-0 transition-opacity group-hover:opacity-100">
          <button type="button" className="hover:text-slate-600" onClick={copy}>Copy</button>
          {message.meta?.latency_ms != null && <span>{Math.round(message.meta.latency_ms)} ms</span>}
        </div>
      </div>
    </div>
  );
}

function ContextPanel({ view }: { view: ConversationView | null }) {
  const active: ActiveEntity | null = view?.context.active ?? null;
  const pending = view?.context.pending_action;
  const results = view?.context.last_result_set;
  const actions: ExecutionView[] = view?.recent_actions ?? [];
  return (
    <div className="space-y-4">
      <section className="card">
        <header className="card-header"><h2 className="card-title">Current context</h2></header>
        <div className="card-body space-y-2 text-sm">
          {!active && <p className="text-slate-500">Nothing selected yet. Ask about a campaign, segment or customer.</p>}
          {active && (
            <>
              <p className="text-xs uppercase tracking-wide text-slate-500">
                {active.kind_label ? `${active.kind_label} campaign` : active.type}
              </p>
              <p className="font-semibold text-slate-900">{active.name ?? `#${active.id}`}</p>
              <dl className="grid grid-cols-[6rem_1fr] gap-x-2 gap-y-1 text-xs">
                {active.status && (<><dt className="text-slate-500">Status</dt><dd><Badge>{active.status}</Badge></dd></>)}
                {active.channel && (<><dt className="text-slate-500">Channel</dt><dd>{active.channel}</dd></>)}
                {active.audience && (
                  <>
                    <dt className="text-slate-500">Audience</dt>
                    <dd>{active.audience.segment ?? `${active.audience.manual_customers ?? 0} customers`}{active.audience.members != null ? ` · ${active.audience.members}` : ''}</dd>
                  </>
                )}
                {typeof active.members === 'number' && (<><dt className="text-slate-500">Members</dt><dd>{active.members}</dd></>)}
                {active.timing && (
                  <><dt className="text-slate-500">Timing</dt><dd>{active.timing.minutes_before_predicted_order} min before · conf ≥ {active.timing.min_confidence}</dd></>
                )}
                {active.coupons && active.coupons.length > 0 && (
                  <><dt className="text-slate-500">Coupons</dt><dd className="font-mono">{active.coupons.filter((c) => c.enabled).map((c) => `${c.code} ${c.allocation}%`).join(' / ')}</dd></>
                )}
                {active.approved != null && (<><dt className="text-slate-500">Approved</dt><dd>{active.approved ? 'Yes' : 'No'}</dd></>)}
              </dl>
              {active.message_template && (
                <p className="rounded-lg bg-slate-50 px-2 py-1.5 text-xs text-slate-600 ring-1 ring-inset ring-slate-200">“{active.message_template}”</p>
              )}
              {active.type === 'automation' && (
                <Link className="text-xs font-medium text-brand-700 hover:underline" to={`/automations/${active.id}`}>Open campaign page →</Link>
              )}
              {active.type === 'customer' && (
                <Link className="text-xs font-medium text-brand-700 hover:underline" to={`/customers/${active.id}`}>Open Customer 360 →</Link>
              )}
            </>
          )}
          {pending && (
            <p className="rounded-lg bg-amber-50 px-2 py-1.5 text-xs text-amber-800 ring-1 ring-inset ring-amber-200">
              Waiting for you: #{pending.id} {pending.summary}
            </p>
          )}
          {results && (
            <p className="text-xs text-slate-500">
              “Them” = {results.count.toLocaleString()} {results.kind} — {results.description}
            </p>
          )}
        </div>
      </section>
      <section className="card">
        <header className="card-header"><h2 className="card-title">Recent actions</h2></header>
        <div className="card-body">
          {actions.length === 0 ? (
            <p className="text-sm text-slate-500">No changes made in this conversation yet.</p>
          ) : (
            <ul className="space-y-2 text-xs">
              {actions.map((a) => (
                <li key={a.id} className="flex items-start gap-2">
                  <span className={a.success ? 'text-emerald-600' : 'text-red-600'}>{a.success ? '✓' : '✕'}</span>
                  <div className="min-w-0">
                    <p className="font-medium text-slate-700">{a.tool_name.replace(/_/g, ' ')}</p>
                    <p className="text-slate-400">
                      {a.target_type ? `${a.target_type} ${a.target_id ?? ''} · ` : ''}receipt #{a.id}
                      {a.error ? ` · ${a.error}` : ''}
                    </p>
                  </div>
                  <RiskBadge risk={a.action_type} />
                </li>
              ))}
            </ul>
          )}
        </div>
      </section>
    </div>
  );
}

// --------------------------------------------------------------------------
// Console
// --------------------------------------------------------------------------
export default function CopilotConsole({ compact = false }: { compact?: boolean }) {
  const [conversationId, setConversationId] = useState<number | null>(readStored);
  const [view, setView] = useState<ConversationView | null>(null);
  const [history, setHistory] = useState<ConversationSummary[]>([]);
  const [input, setInput] = useState('');
  const [sending, setSending] = useState<string | null>(null);
  const [busyAction, setBusyAction] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const bottom = useRef<HTMLDivElement>(null);

  const loadHistory = useCallback(() => {
    api.get<ConversationSummary[]>('/api/v1/ai/conversations').then(setHistory).catch(() => setHistory([]));
  }, []);

  useEffect(() => {
    loadHistory();
  }, [loadHistory]);

  useEffect(() => {
    writeStored(conversationId);
    if (conversationId === null) {
      setView(null);
      return;
    }
    if (view?.id === conversationId) return;
    setLoading(true);
    api
      .get<ConversationView>(`/api/v1/ai/conversations/${conversationId}`)
      .then((v) => setView(v))
      .catch(() => {
        setConversationId(null);
      })
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversationId]);

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [view?.messages.length, sending]);

  const send = async (text: string) => {
    const message = text.trim();
    if (!message || sending) return;
    setInput('');
    setError(null);
    setSending(message);
    try {
      const next = await api.post<ConversationView>('/api/v1/ai/chat', {
        message,
        conversation_id: conversationId,
      });
      setView(next);
      setConversationId(next.id);
      loadHistory();
    } catch (e) {
      setError(errorText(e));
      setInput(message);
    } finally {
      setSending(null);
    }
  };

  const act = async (path: 'confirm' | 'cancel' | 'retry', actionId: number) => {
    setBusyAction(actionId);
    setError(null);
    try {
      const outcome = await api.post<ActionOutcome>(`/api/v1/ai/${path}`, { action_id: actionId });
      setView(outcome.conversation);
      if (path === 'cancel') notify('Cancelled — nothing was changed.', 'info');
      else if (outcome.execution?.success) notify(outcome.duplicate ? 'Already done — nothing repeated.' : 'Done.', 'success');
      else notify(outcome.action.error ?? 'The action failed.', 'error');
    } catch (e) {
      setError(errorText(e));
      if (conversationId) {
        api.get<ConversationView>(`/api/v1/ai/conversations/${conversationId}`).then(setView).catch(() => undefined);
      }
    } finally {
      setBusyAction(null);
    }
  };

  const clear = async () => {
    if (!conversationId) return;
    try {
      await api.del(`/api/v1/ai/conversations/${conversationId}`);
      notify('Conversation cleared. Execution receipts are kept in the audit trail.', 'info');
    } catch (e) {
      setError(errorText(e));
      return;
    }
    setConversationId(null);
    loadHistory();
  };

  const groups = useMemo(() => {
    // Consecutive tool steps render together under the reply they produced.
    const out: (CopilotMessageView | CopilotMessageView[])[] = [];
    for (const m of view?.messages ?? []) {
      if (m.role === 'assistant' && !m.content) continue;
      if (m.role === 'tool') {
        const last = out[out.length - 1];
        if (Array.isArray(last)) last.push(m);
        else out.push([m]);
      } else out.push(m);
    }
    return out;
  }, [view]);

  const provider = view?.provider;
  const empty = !view || view.messages.length === 0;

  const conversation = (
    <div className={`flex min-h-0 flex-1 flex-col ${compact ? '' : 'card overflow-hidden'}`}>
      <div className="flex items-center justify-between gap-2 border-b border-slate-200 px-4 py-2.5">
        <div className="min-w-0">
          <p className="truncate text-sm font-semibold text-slate-900">{view?.title ?? 'New conversation'}</p>
          <p className="truncate text-[11px] text-slate-500">
            {provider
              ? provider.mode === 'mock'
                ? 'Offline planner — no AI model configured'
                : `${provider.provider} · ${provider.model}`
              : 'Every change is previewed and needs your confirmation'}
            {view?.context.now_local ? ` · ${view.context.now_local}` : ''}
          </p>
        </div>
        <div className="flex shrink-0 gap-1.5">
          <button type="button" className="btn-ghost px-2 py-1 text-xs" onClick={() => setConversationId(null)}>New</button>
          {conversationId && (
            <button type="button" className="btn-ghost px-2 py-1 text-xs text-red-600" onClick={clear}>Clear</button>
          )}
        </div>
      </div>

      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto bg-slate-50/60 px-4 py-4" aria-live="polite">
        {loading && <div className="flex justify-center py-10"><Spinner /></div>}
        {!loading && empty && !sending && (
          <div className="mx-auto max-w-xl py-8 text-center">
            <p className="text-base font-semibold text-slate-900">What would you like to do?</p>
            <p className="mt-1 text-sm text-slate-500">
              Ask in plain English. I inspect the engine with approved tools, show you a preview, and only change
              anything when you press Confirm.
            </p>
            <div className="mt-5 flex flex-wrap justify-center gap-2">
              {SUGGESTED_COMMANDS.map((c) => (
                <button key={c} type="button" className="rounded-full bg-white px-3 py-1.5 text-xs text-slate-700 ring-1 ring-slate-200 hover:bg-brand-50 hover:text-brand-700 hover:ring-brand-200" onClick={() => send(c)}>
                  {c}
                </button>
              ))}
            </div>
          </div>
        )}
        {groups.map((item, index) => {
          if (Array.isArray(item)) {
            const cards = item.filter((m) => m.action);
            return (
              <div key={`g${index}`} className="ml-10 space-y-2">
                <div className="flex flex-wrap gap-1.5">
                  {item.map((m) => <ToolStep key={m.id} message={m} />)}
                </div>
                {cards.map((m) => (
                  <ActionCard
                    key={`a${m.id}`}
                    action={m.action!}
                    busy={busyAction === m.action!.id}
                    onConfirm={(id) => act('confirm', id)}
                    onCancel={(id) => act('cancel', id)}
                    onRetry={(id) => act('retry', id)}
                  />
                ))}
              </div>
            );
          }
          const m = item;
          if (m.role === 'user') {
            return (
              <div key={m.id} className="flex justify-end">
                <p className="max-w-[85%] whitespace-pre-wrap rounded-xl bg-brand-600 px-4 py-2.5 text-sm text-white">{m.content}</p>
              </div>
            );
          }
          if (m.role === 'event') {
            const failed = /FAILED/.test(m.content);
            return (
              <p key={m.id} className={`mx-auto max-w-[90%] rounded-full px-3 py-1 text-center text-xs ${failed ? 'bg-red-50 text-red-700' : 'bg-emerald-50 text-emerald-800'}`}>
                {m.content.split(' Result:')[0]}
              </p>
            );
          }
          return <Fragment key={m.id}><AssistantMessage message={m} /></Fragment>;
        })}
        {sending && (
          <>
            <div className="flex justify-end">
              <p className="max-w-[85%] whitespace-pre-wrap rounded-xl bg-brand-600/80 px-4 py-2.5 text-sm text-white">{sending}</p>
            </div>
            <div className="ml-10 flex items-center gap-2 text-xs text-slate-500">
              <Spinner className="h-4 w-4" /> Working — inspecting the engine…
            </div>
          </>
        )}
        <div ref={bottom} />
      </div>

      {error && (
        <div className="flex items-center justify-between gap-2 border-t border-red-200 bg-red-50 px-4 py-2 text-xs text-red-700">
          <span>{error}</span>
          <button type="button" className="font-medium underline" onClick={() => setError(null)}>Dismiss</button>
        </div>
      )}

      <form
        className="border-t border-slate-200 bg-white p-3"
        onSubmit={(e) => {
          e.preventDefault();
          send(input);
        }}
      >
        <div className="flex items-end gap-2">
          <label htmlFor="copilot-input" className="sr-only">Message the AI Copilot</label>
          <textarea
            id="copilot-input"
            className="input min-h-[2.75rem] flex-1 resize-none"
            rows={compact ? 2 : 2}
            placeholder="e.g. Create a Smart Reorder campaign for lapsed beer customers…"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                send(input);
              }
            }}
            disabled={!!sending}
          />
          <button type="submit" className="btn-primary h-11 px-4" disabled={!!sending || !input.trim()}>
            {sending ? <Spinner className="h-4 w-4 text-white" /> : 'Send'}
          </button>
        </div>
        {!empty && (
          <div className="mt-2 flex gap-1.5 overflow-x-auto pb-1">
            {SUGGESTED_COMMANDS.slice(0, compact ? 3 : 6).map((c) => (
              <button key={c} type="button" className="shrink-0 rounded-full bg-slate-100 px-2.5 py-1 text-[11px] text-slate-600 hover:bg-slate-200" onClick={() => send(c)} disabled={!!sending}>
                {c}
              </button>
            ))}
          </div>
        )}
      </form>
    </div>
  );

  if (compact) return <div className="flex h-full min-h-0 flex-col">{conversation}</div>;

  return (
    <div className="grid h-[calc(100vh-8.5rem)] min-h-[32rem] grid-cols-1 gap-4 lg:grid-cols-[13rem_minmax(0,1fr)_19rem]">
      <aside className="hidden min-h-0 flex-col lg:flex">
        <button type="button" className="btn-primary mb-3 w-full" onClick={() => setConversationId(null)}>New conversation</button>
        <p className="px-1 pb-1 text-xs font-semibold uppercase tracking-wide text-slate-400">History</p>
        <ul className="min-h-0 flex-1 space-y-0.5 overflow-y-auto">
          {history.length === 0 && <li className="px-1 text-xs text-slate-400">No conversations yet.</li>}
          {history.map((c) => (
            <li key={c.id}>
              <button
                type="button"
                onClick={() => setConversationId(c.id)}
                className={`w-full truncate rounded-lg px-2 py-1.5 text-left text-xs ${c.id === conversationId ? 'bg-brand-50 font-medium text-brand-700' : 'text-slate-600 hover:bg-slate-100'}`}
                title={c.title}
              >
                {c.title}
              </button>
            </li>
          ))}
        </ul>
      </aside>
      {conversation}
      <aside className="hidden min-h-0 overflow-y-auto lg:block">
        <ContextPanel view={view} />
      </aside>
    </div>
  );
}
