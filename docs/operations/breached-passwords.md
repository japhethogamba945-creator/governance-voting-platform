# Breached-password check

New passwords (invitation acceptance) are refused if they appear in a breached-password corpus (ASVS 2.1.7, finding F-2). The check is **offline**: `accounts.validators.BreachedPasswordValidator` reads a local mirror, so no password, and no part of its hash, leaves the server.

## Mirror format

`BREACHED_PASSWORDS_DIR` points to a directory with one file per five-character uppercase SHA-1 prefix, `00000.txt` to `FFFFF.txt`. Each line is `SUFFIX:COUNT`, where `SUFFIX` is the remaining 35 hexadecimal characters of the SHA-1. This is the "range" format of the Have I Been Pwned Pwned Passwords service. For each password, only the one file for its prefix is read. A missing file means "not listed", so a partial mirror is acceptable.

## Building it

1. Download the SHA-1 Pwned Passwords corpus as individual range files, for example with the official downloader (`haveibeenpwned-downloader`, writing one file per prefix). Check the corpus's licence terms before use. **[LEGAL-REVIEW]**
2. Copy the directory to read-only storage available to the web servers, and set `BREACHED_PASSWORDS_DIR`.
3. Refresh it periodically (for example, every quarter).

`manage.py check --deploy` warns (`accounts.W001`) when `DEBUG` is off and `BREACHED_PASSWORDS_DIR` is not set. **Deployment checklist item.**
