# AGENTS.md

Guidance for agents changing this repository. It records the constraints a change has to respect; the [README](README.md) explains the behaviour to users, and [docs/](docs/) holds the background.

## What this is

Nginx configuration plus bash installers that make GridPane's FastCGI and Redis page caches ignore marketing query parameters (`gclid`, `utm_*`, …) without a redirect. Everything runs on a GridPane server against real nginx. Nothing here is an application and nothing runs in a browser.

The README is the user-facing source of truth for paths, behaviour and commands. A change to any of those updates it in the same commit.

## Non-negotiables

- **Add files, never edit GridPane's.** GridPane's templates include fixed globs (`/etc/nginx/conf.d/`, `/var/www/<site>/nginx/*-context.conf`, `mu-plugins/`). Write into those include points and leave every GridPane-managed file untouched.
- **Every mutation is transactional.** Back up each file you touch, write, run `nginx -t`, reload only when it passes, and restore the previous files when it fails. `install.sh` and `extras/*/install.sh` implement this (`remember` / `write_file` / `remove_file` / `rollback` / `apply`); extend that path instead of adding a second one.
- **The cache key stays stock.** A clean URL must hash to byte-identical GridPane key material (`…$host$request_uri`), so Nginx Helper and GridPane purges keep working. Both sandbox cache modes assert this.
- **PHP sees the clean URL on a cacheable request.** A listed parameter must never reach PHP through `REQUEST_URI` or `QUERY_STRING` when the response can be stored. That is why the golden rule holds: only parameters read by JavaScript belong on the list.
- **Pure nginx.** `map` and PCRE only. No Lua, no njs, no extra module, and no runtime dependency on the server beyond what GridPane already ships.

## Naming

The repository's short name is **gnmp** (gridpane nginx marketing params). Files the optional add-ons create on the server carry it:

- Exact cache log: `/etc/nginx/conf.d/gnmp-cachelog.conf` (one `log_format gnmpcache`) and `/var/www/<site>/nginx/gnmp-cachelog-main-context.conf`.
- `s64-cachelog.conf`, `s64cache` and `s64-cachelog-main-context.conf` are the previous spelling of those same files. Do not introduce new `s64` names. `extras/cache-log/install.sh` migrates an old install on its next enable run: it rewrites the site file, removes the old site file, and drops the old `log_format` once no site uses it. `--status` reports leftovers.
- The core installer keeps its `marketing-params` names (`/etc/nginx/marketing-params/`, `marketing-params-*-context.conf`, `$gp_mkt_*`) because they follow GridPane's own file naming.
- `extras/perf-digest/` keeps `s64-nginx-perf.sh`, `s64-slow-catch.sh` and their config, cron and history paths: installed cron lines and copies resolve those names. There, change only comments that refer to the cache-log files.

## Layout and generated files

- `install.sh` — server-wide engine plus the per-site switch/flag; performs the release update check.
- `build-engine.py` — generates `engine.conf` at install time from the map chain and `params.list`. It is not committed; `install.sh` runs it with the saved limits.
- `server/` — engine sources and the `conf.d` stub; `site/` — per-site switch and flag templates; `extras/` — optional add-ons; `tests/` — sandbox, installer, live and WooCommerce tests.
- `plans/` is a symlink to an external plans directory and is gitignored. Keep it out of commits.

## Tests

- `./test.sh` runs the sandbox and installer tests. It needs root and a GridPane server (real nginx, GridPane templates, Redis modules), and it never touches the live config. Run it before committing a change to nginx config, `install.sh` or the tests.
- `./test.sh --cache fcgi|redis|all`, `./test.sh --site <site>`, `./test.sh install`, `./test.sh live <site> [path]` and `./test.sh woo <site>` narrow the run.
- `tests/install_test.py` relocates `/etc/nginx`, `/var/www` and `/root` into a throw-away root by rewriting those strings. New paths in scripts and configs must use the same literals, or the test will miss them.
- `extras/cache-log/install.sh` has no automated test yet. Verify it in a throw-away root with `GP_CACHELOG_ROOT=<dir>` (which skips `nginx -t` and the reload): enable, `--status`, `--disable`, `--uninstall`, and the `s64` → `gnmp` migration.

## Docs, versions and commits

- Update the README whenever behaviour, file names, install paths or commands change.
- One semantic version covers the repository. Releases are tags `vX.Y.Z`; `extras/woo-purge/gp-woo-purge.php`'s `Version:` header matches the release. Installers treat only release tags as updates, so untagged commits on `main` are not updates.
- Commit subjects follow the existing conventional style (`feat(scope): …`, `fix(scope): …`, `docs: …`, `chore: …`) and the body explains why.
