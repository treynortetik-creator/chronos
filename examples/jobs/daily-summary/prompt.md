Summarize the last 24 hours of git activity in the current directory and its immediate subdirectories.

1. Find every git repository one or two levels down (a folder containing `.git`).
2. For each, run `git log --since="24 hours ago" --pretty=format:"%h %an %s" --no-merges` and
   `git status --short | head -20`.
3. Skip repositories with no commits and no uncommitted changes.
4. Write the result as a short markdown report: one heading per repository, bullet points for what
   changed in plain language, and a final "Needs attention" list for uncommitted work older than a day.

Read-only task: do not commit, push, pull, or modify any file other than the report.
Keep the report under 300 words.
