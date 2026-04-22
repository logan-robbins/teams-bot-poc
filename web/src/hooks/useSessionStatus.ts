import useSWR from "swr";
import { sink } from "../lib/sink";
import type { SessionStatusResponse } from "../lib/types";

const REFRESH_MS = 500;

export function useSessionStatus() {
  const { data, error, isLoading, mutate } = useSWR<SessionStatusResponse>(
    "session-status",
    sink.status,
    {
      refreshInterval: REFRESH_MS,
      revalidateOnFocus: false,
      dedupingInterval: 100,
      shouldRetryOnError: true,
    },
  );

  return {
    status: data,
    session: data?.session,
    error,
    isLoading,
    refresh: mutate,
  };
}
