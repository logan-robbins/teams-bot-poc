import useSWR from "swr";
import { sink } from "../lib/sink";
import type { SessionAnalysisResponse } from "../lib/types";

const REFRESH_MS = 2_000;

export function useSessionAnalysis() {
  const { data, error, isLoading, mutate } = useSWR<SessionAnalysisResponse>(
    "session-analysis",
    sink.analysis,
    {
      refreshInterval: REFRESH_MS,
      revalidateOnFocus: false,
      dedupingInterval: 500,
      shouldRetryOnError: true,
    },
  );

  return {
    analysis: data?.analysis,
    error,
    isLoading,
    refresh: mutate,
  };
}
