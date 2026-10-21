Your workflow:

1. PLAN FIRST: Read the task file, ask any clarifying questions, then propose
   a concrete numbered implementation plan. Do not write any code until the
   user approves the plan.

2. ON APPROVAL: Update the "Agreed Plan" section of the task file with the
   final plan, then proceed to implement it step by step.

3. WHILE EXECUTING: After each step, tick the checkbox in the Progress Log
   and make a descriptive git commit.

4. IF BLOCKED: If you hit something that would materially change the plan,
   stop and discuss before proceeding.

5. ON COMPLETION: Mark Status as "complete". Add a "## Summary" section
   covering key decisions, patterns established, gotchas, and anything a
   future agent should know. Then remove the "## Agreed Plan" and
   "## Progress Log" sections — they are working scaffolding, not permanent
   record. The final file should read as a clean ADR:
   Status/Branch/Created → Objective → Context → Summary.
