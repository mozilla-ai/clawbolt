# TODOS

## QuickBooks

### Material cost research assistance

**What:** Help users look up material costs when building estimates (supplier APIs, personal price book).

**Why:** Users like Jesse research material costs separately and manually enter them. Automating this removes another manual step from the estimate workflow and is a differentiator vs generic tools.

**Context:** The voice-to-estimate design doc (2026-03-19) explicitly defers this. Jesse described needing to research material costs as a separate step outside QB. Two approaches: (1) Supplier API integrations (Home Depot, Lowe's) are complex and vary by region. (2) A "personal price book" stored in MEMORY.md or a dedicated table, built up over time from the user's own estimates, is simpler and self-improving. Start with approach 2: when the agent creates an estimate with material costs, persist those prices to memory. Over time, the agent can suggest prices based on past jobs. Approach 1 is a bigger lift and depends on supplier API availability.

**Effort:** M (personal price book) / XL (supplier APIs)
**Priority:** P2
**Depends on:** Core voice-to-estimate workflow must ship first
