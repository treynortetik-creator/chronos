Do a read-only housekeeping scan of the current directory and its immediate subdirectories.

1. For each git repository, list local branches already merged into the default branch
   (`git branch --merged`) and branches with no commit in 60 days.
2. List the ten largest untracked or ignored files over 50 MB (`git status --ignored`, then `du`).
3. List common build output folders (`node_modules`, `.venv`, `dist`, `build`, `target`) with their sizes.

REPORT ONLY. Do not delete, move, or modify anything. Finish with a short "Suggested commands" block
the owner can copy and run by hand if they agree, clearly marked as not yet executed.
