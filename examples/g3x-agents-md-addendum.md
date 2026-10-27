## AgentBridge mode (owner-approved)

When a task is dispatched by AgentBridge (the prompt says so and the working directory is an
AgentBridge git worktree on the AgentBridge host):

- The workspace is that worktree, not `/Users/avp/project/g3x-lite`. Do not change directory
  elsewhere, push, rebase or touch `main`. AgentBridge commits, validates, integrates into
  `agentbridge/*` branches and opens draft PRs only after owner approval.
- Read README, docs/SAFETY.md, docs/PROJECT_STATE.md, docs/HANDOFF.md and docs/RESEARCH_PLAN.md
  as usual. All scope restrictions above still apply (Phase 0 read-only regarding firmware and
  hardware; STRICT NO-GO).
- Do not edit docs/PROJECT_STATE.md or docs/HANDOFF.md unless the task explicitly asks for it.
  The milestone's final task updates them once, which avoids merge conflicts between parallel tasks.
- Report findings in the task result with VERIFIED / INFERRED / UNKNOWN labels and exact citations.
  The evidence and commit rules above still apply.
- Never run tests/test_validate_witness_inspector.py or tools/build_witness_inspector.py: they
  build a camera payload binary.
