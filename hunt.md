# Idea Hunt — Active Entry Point

`./hunt.sh` runs a multi-process research pipeline. `PROGRAM.md` defines the loop protocol, stage boundaries, and ledger schema; role prompts live under `roles/`; the header comments in `hunt.sh` and `README.md` define runtime parameters, structural gates, and guards. All generated prose must be English.

## Entry-Point Rules

- Stop when today's unanimous Strong Accept count reaches `SA_TARGET` (default 1). `SA_TARGET=0` removes the upper bound and accumulates indefinitely. Before the target is reached, continue without asking a human.
- Re-entry is idempotent: count today's Strong Accept rows in the ledger; if the target is already met, ensure publication and exit.
- Empty early-stage output or a failed structural gate receives the normal short retry. Escalate to abnormal cooldown only after `EMPTY_MAX` consecutive failures.
- Research has no time-window limit; classic and cross-domain sources are allowed.
- Report structure: 1 Key Literature · 2 Accepted Ideas (complete review table and directed prior-work record) · 3 Rejected Ideas · 4 Metadata (round count and review date).
