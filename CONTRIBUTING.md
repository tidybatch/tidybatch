# Contributing to TidyBatch

Thanks for helping. The most useful thing you can send is a bug report with a folder layout that reproduces it.

## Reporting a bug

- Include `tidybatch --version`, your OS and your Python version.
- Paste the exact command and its full output. The preview is enough: you don't need `--apply` to show a wrong name.
- Describe the files: a listing (`ls -la` or `dir`) of the ones involved, or a short script that creates them. Names matter; contents usually don't.
- If an applied run went wrong, keep `<folder>/.tidybatch/`. `tidybatch undo <folder>` reads the journal in there, and the journal shows exactly what was moved.

## Pull requests

`tidybatch.py` in this repository is generated from the source of TidyBatch Pro (Core is Pro without `organize` and `dedupe`), so a pull request to it can't be merged as is. A PR is still the best way to propose a fix: it gets applied to the shared source together with its test, Core is regenerated, and you are credited as co-author of the commit.

Before opening one:

- add a test to `tests/test_tidybatch.py` that fails without your change;
- run `python -m unittest discover -s tests` (Python 3.9 or newer);
- keep it standard library only.

Changes to the README, the docs and the `dev/` scripts can be merged directly.

## Security

Found a way to lose or overwrite files, or to write outside the chosen folder? Please report it privately: see [SECURITY.md](SECURITY.md).
