// Scoring is a read-only calculation: retry the same completed evidence, never
// restart the camera task or submit an assessment to the patient's history.
export async function requestTestingScore<T>(
  request: (signal: AbortSignal) => Promise<Response>,
  { attempts = 3, timeoutMs = 12000, retryDelayMs = 750 } = {},
): Promise<T> {
  for (let attempt = 0; attempt < attempts; attempt++) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    let permanentError: Error | null = null;
    try {
      const response = await request(controller.signal);
      if (response.ok) return await response.json() as T;
      if (response.status < 500 && ![408, 429].includes(response.status)) {
        permanentError = new Error(response.status === 401 || response.status === 403
          ? "Sign-in could not be confirmed. Your completed test is still on this page; retry calculation after the connection is restored."
          : "The test evidence could not be scored. Please retry calculation.");
      }
    } catch {
      // A temporary network failure, timeout or interrupted response may recover.
    } finally {
      clearTimeout(timer);
    }
    if (permanentError) throw permanentError;
    if (attempt + 1 < attempts) await new Promise(resolve => setTimeout(resolve, retryDelayMs * (attempt + 1)));
  }
  throw new Error("Your test is complete, but the results service could not be reached. Your movement evidence is still on this page. Select Retry calculation; you do not need to repeat the exercise.");
}
