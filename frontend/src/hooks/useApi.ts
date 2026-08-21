import { useCallback, useEffect, useRef, useState } from 'react';

import { ApiError, api } from '../api/client';

interface QueryState<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
}

/**
 * Fetch a GET endpoint, re-running when `path` or `deps` change.
 *
 * Requests are aborted on unmount and superseded by newer ones, so a slow
 * response can never overwrite fresher data.
 */
export function useQuery<T>(
  path: string | null,
  deps: unknown[] = [],
): QueryState<T> & { refetch: () => void } {
  const [state, setState] = useState<QueryState<T>>({
    data: null,
    loading: path !== null,
    error: null,
  });
  const [nonce, setNonce] = useState(0);
  const latestRequest = useRef(0);

  useEffect(() => {
    if (path === null) {
      setState({ data: null, loading: false, error: null });
      return;
    }

    const requestId = ++latestRequest.current;
    const controller = new AbortController();
    setState((current) => ({ ...current, loading: true, error: null }));

    api
      .get<T>(path, controller.signal)
      .then((data) => {
        if (requestId === latestRequest.current) {
          setState({ data, loading: false, error: null });
        }
      })
      .catch((error: unknown) => {
        if ((error as Error)?.name === 'AbortError') return;
        if (requestId !== latestRequest.current) return;
        setState({
          data: null,
          loading: false,
          error: error instanceof ApiError ? error.message : 'Something went wrong.',
        });
      });

    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, nonce, ...deps]);

  const refetch = useCallback(() => setNonce((n) => n + 1), []);
  return { ...state, refetch };
}

/** Run a mutation, tracking pending state and surfacing a readable error. */
export function useMutation<TArgs extends unknown[], TResult>(
  fn: (...args: TArgs) => Promise<TResult>,
): {
  run: (...args: TArgs) => Promise<TResult | null>;
  loading: boolean;
  error: string | null;
  reset: () => void;
} {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = useCallback(
    async (...args: TArgs) => {
      setLoading(true);
      setError(null);
      try {
        return await fn(...args);
      } catch (err: unknown) {
        setError(err instanceof ApiError ? err.message : 'Something went wrong.');
        return null;
      } finally {
        setLoading(false);
      }
    },
    [fn],
  );

  return { run, loading, error, reset: () => setError(null) };
}

/** Delay a rapidly-changing value (search boxes) before it triggers a fetch. */
export function useDebounced<T>(value: T, delayMs = 300): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs]);
  return debounced;
}
