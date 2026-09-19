import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "./api";

export interface AsyncState<T> {
  data: T | undefined;
  error: ApiError | Error | undefined;
  loading: boolean;
  reload: () => void;
}

/**
 * Run an async loader, with reload and unmount safety.
 *
 * The `active` guard matters: several screens poll, and setting state after
 * unmount is both a React warning and a leak.
 */
export function useAsync<T>(loader: () => Promise<T>, deps: unknown[] = []): AsyncState<T> {
  const [data, setData] = useState<T | undefined>(undefined);
  const [error, setError] = useState<ApiError | Error | undefined>(undefined);
  const [loading, setLoading] = useState(true);
  const [nonce, setNonce] = useState(0);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    let active = true;
    setLoading(true);
    loader()
      .then((value) => {
        if (active && mounted.current) {
          setData(value);
          setError(undefined);
        }
      })
      .catch((caught: unknown) => {
        if (active && mounted.current) setError(caught as Error);
      })
      .finally(() => {
        if (active && mounted.current) setLoading(false);
      });
    return () => {
      active = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  return { data, error, loading, reload };
}

/** Poll while `shouldPoll` holds. Used by job and session screens. */
export function usePolling(reload: () => void, shouldPoll: boolean, intervalMs = 1500): void {
  useEffect(() => {
    if (!shouldPoll) return;
    const handle = window.setInterval(reload, intervalMs);
    return () => window.clearInterval(handle);
  }, [reload, shouldPoll, intervalMs]);
}
