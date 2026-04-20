# TODOs

## Receipt URL-off user preference (P2)

Context: some contractors in iMessage / SMS find URL-laden receipts noisy.
Requested 2026-04-19 while reviewing the CompanyCam-receipts PR. Deferred from that PR
to keep the immediate bug fix small.

Plan:
1. Add `users.receipts_show_urls: Mapped[bool] = mapped_column(Boolean, default=True)`.
2. Alembic migration.
3. Expose via `UserProfileResponse`. Not writable through the public `UserProfileUpdate`
   schema. Dedicated `set_preference(key, value)` tool for the LLM so it can honor a
   "don't give me URLs" request from the user.
4. In `backend/app/agent/tool_summary.py::render_receipt_line`, drop the URL line when
   the current user's preference is off. Grouped receipts likewise skip the URL line.
5. Tests: preference toggling, render output shape, LLM tool.

Blast radius: `backend/app/models.py`, `alembic/`, `backend/app/agent/tool_summary.py`,
new preferences tool, new tests. Estimate: one day CC.
