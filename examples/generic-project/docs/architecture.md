# Project architecture

This example keeps its context as plain files under `.context/` and its prose
references under `docs/`. Rules describe standards, memory records capture
reusable lessons, and work records track planned changes.

## Updating context

Edit a source file, then run `context-toolkit-query --doctor` to check which
stores the toolkit discovered. Recall and docs search use the current files on
the next request.
