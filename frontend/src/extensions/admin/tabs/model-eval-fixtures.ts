import type { EvalReport, EvalRun, EvalRunList, EvalSummary, EvalTurn } from '../admin-api';

// Shared by model-eval.test.tsx (the run form and history) and
// model-eval-report.test.tsx (one run's report), which exercise two pages
// against the same shapes.

export const USERS = {
  total: 1,
  skip: 0,
  limit: 200,
  items: [
    {
      id: 'user-1',
      user_id: 'google_abc',
      email: 'consenting@example.com',
      plan: 'pro',
      status: 'active',
      role: 'user',
      is_active: true,
      onboarding_complete: true,
      data_sharing_consent: true,
    },
  ],
};

export function summary(overrides: Partial<EvalSummary> = {}): EvalSummary {
  return {
    turns_total: 40,
    turns_completed: 40,
    turns_failed: 0,
    agreement_counts: { identical: 40 },
    safety_counts: {},
    blocking_findings: 0,
    judge_counts: {},
    identical_rate: 1,
    divergence_rate: 0,
    silent_noop_rate: 0,
    baseline: {
      provider: 'anthropic',
      model: 'incumbent',
      input_tokens: 100,
      output_tokens: 10,
      cache_read_tokens: 900,
      cache_creation_tokens: 0,
      cache_read_ratio: 0.9,
      total_cost_usd: '1.500000',
      pricing_available: true,
      latency_p50_ms: 1200,
      latency_p95_ms: 2400,
    },
    candidate: {
      provider: 'anthropic',
      model: 'candidate',
      input_tokens: 1000,
      output_tokens: 10,
      cache_read_tokens: 0,
      cache_creation_tokens: 0,
      cache_read_ratio: 0,
      total_cost_usd: '0.400000',
      pricing_available: true,
      latency_p50_ms: 600,
      latency_p95_ms: 900,
    },
    recommendation: 'safe_to_switch',
    reasons: ['no safety findings across 40 turns'],
    warnings: [],
    ...overrides,
  };
}

export function run(overrides: Partial<EvalRun> = {}): EvalRun {
  return {
    id: 'run-0001',
    user_id: 'user-1',
    user_email: 'consenting@example.com',
    user_consented: true,
    baseline_provider: 'anthropic',
    baseline_model: 'incumbent',
    candidate_provider: 'anthropic',
    candidate_model: 'candidate',
    judge_model: 'incumbent',
    requested_samples: 40,
    status: 'completed',
    progress_completed: 40,
    progress_total: 40,
    recommendation: 'safe_to_switch',
    error: '',
    created_at: '2026-05-01T12:00:00Z',
    summary: summary(),
    ...overrides,
  };
}

export function turn(overrides: Partial<EvalTurn> = {}): EvalTurn {
  return {
    message_seq: 1,
    message_timestamp: '2026-05-01T12:00:00Z',
    user_message: 'can you book that job',
    historic_reply: '',
    historic_tool_names: [],
    baseline: {
      text: '',
      tool_calls: [{ name: 'create_job', arguments: { customer: 'Acme Plumbing' } }],
      stop_reason: 'tool_use',
      input_tokens: 0,
      output_tokens: 0,
      cache_read_tokens: 0,
      latency_ms: 0,
      error: '',
    },
    candidate: {
      text: 'Sure, I can help with that.',
      tool_calls: [],
      stop_reason: 'end_turn',
      input_tokens: 0,
      output_tokens: 0,
      cache_read_tokens: 0,
      latency_ms: 0,
      error: '',
    },
    agreement: 'replied_instead_of_acting',
    safety_issues: [],
    judge_verdict: 'not_judged',
    judge_rationale: '',
    ...overrides,
  };
}

export function report(overrides: Partial<EvalReport> = {}): EvalReport {
  return { run: run(), turns: [turn()], total_turns: 1, ...overrides };
}


// ``listEvalRuns`` returns the run list plus the bounds the sample slider has
// to respect, so a mock has to carry them or the slider falls back to its own
// defaults and the test stops exercising the wired value.
export function runList(runs: EvalRun[] = [], overrides: Partial<EvalRunList> = {}): EvalRunList {
  return {
    runs,
    total: runs.length,
    max_samples: 200,
    min_turns_for_verdict: 20,
    ...overrides,
  };
}
