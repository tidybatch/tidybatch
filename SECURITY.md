# Security policy

TidyBatch renames and moves files, so we treat these as security issues:

- a run that loses, deletes or overwrites a file;
- a move outside the folder you gave it;
- a crash, hang or wrong result triggered by a crafted file name or a crafted photo (the EXIF date parser reads untrusted bytes).

**Please report them privately** with [Report a vulnerability](https://github.com/tidybatch/tidybatch/security/advisories/new) on this repository's Security tab, not in a public issue. Include the version (`tidybatch --version`), your OS and a way to reproduce it.

Fixes are released for the latest version only (currently 1.1.x).
