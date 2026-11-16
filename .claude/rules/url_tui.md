---
paths:
  - "url_tui.py"
---

# url_tui.py

Takes one URL apart and explains each piece in plain words: warnings first, then scheme, login,
host (host, registered domain, subdomain, top-level domain, punycode), port, path segments, every
query parameter, the fragment, a few sites' own formats (YouTube, GitHub, Wikipedia, Google,
Amazon, Outlook Safe Links), the link without tracking, and — after `r` — the redirect chain.
A parameter holding another URL (or the end of the redirects) is inspected with Enter, and
Backspace comes back. The URL comes from the argument, stdin, or the clipboard (`pbpaste`).
`--once` prints the breakdown; `--once --follow` adds the redirects.

- **Offline by design.** `inspect()` never touches the network; only `follow()` does, and only on
  `r` or `--follow`: HEAD requests (GET when a server refuses HEAD) with redirects *not* followed
  by urllib (`NoRedirect`), so each hop and its status is recorded. Pages are never rendered, so
  JavaScript and meta-refresh redirects aren't seen; the help says so. It runs on a thread and
  the loop ticks only while it's busy.
- **The registered domain is a guess.** There's no Public Suffix List in the stdlib, so
  `registered_domain()` uses the last two labels, or three when the last two are in
  `TWO_PART_SUFFIXES` (co.uk, com.au, github.io, …). That's the row marked "◀ check" and the
  domain every warning names — extend the set rather than special-casing hosts.
- Everything the explanations say lives in tables at the top (`SCHEMES`, `TLDS`, `COUNTRIES`,
  `PARAMS`, `SECRET`, `REDIRECT`, `SHORTENERS`, `SITES`, `RISKY_EXT`). `PARAMS` tags tracking
  parameters `track`, which is also what `clean_url()` drops; it keeps the other pairs exactly as
  written rather than re-encoding them.
- Warnings it can raise: http, password or text before `@`, a brand or TLD in the subdomain
  (`paypal.com.evil.xyz`), mixed alphabets in a label and other non-Latin hosts, IP hosts and
  IPv4 written as one number/hex/octal (`socket.inet_aton` decides), shorteners, .zip/.mov
  domains, executable downloads, email addresses in parameters, redirect parameters, tokens in
  the fragment, data: URLs holding HTML, javascript:.
- Unwrapped automatically: Proofpoint URL Defense v2 (`u=` with `-XX` escapes) and v3
  (`/v3/__…__;`), Outlook Safe Links and Google's `/url?q=` via the redirect parameters, base64
  URLs (`aHR0c…`). JWTs in a parameter or path are decoded to show the algorithm, claims and
  expiry.

Testing: `--once` on a set of hostile URLs — userinfo plus brand subdomain plus port plus
`..` plus an email and redirect parameter and `#access_token=` in one; `https://xn--pypal-4ve.com/`;
`http://3232235777/`; a Safe Links and a urldefense v3 link; `example.com/setup.exe` (no
scheme); mailto, data:text/html, magnet, a JWT parameter. `--once --follow http://github.com`
shows one 301 hop. The curses half works in tmux at 100×30 and the 50×14 minimum.
