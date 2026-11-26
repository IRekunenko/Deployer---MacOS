# ⚡ Deployer for macOS

**English** · [Українська](README.uk.md) · [Русский](README.ru.md)

**Commit, push and deploy your projects to your own servers with one click: a native macOS app, no CI/CD setup, no FTP.**

Built for solo developers, freelancers and **vibe coders** who write code with Claude Code, Cursor, Copilot or ChatGPT and just want to get it onto a server safely, without learning GitHub Actions, Jenkins or Docker.

If you still upload folders with FileZilla and edit the database in phpMyAdmin, Deployer replaces that with a repeatable, safe workflow:

> write code → **Commit** → **Push** to Bitbucket / GitHub → **Deploy** to DEV → check → **Deploy** to PROD

## Features

- **Commit & Push** from a simple window: one or several Git services at once (Bitbucket, GitHub, GitLab, Gitea / self-hosted).
- **One-click deploy** over SSH to separate environments (DEV, STAGE, PROD…), each with its own server, folder and branch.
- **Preview before deploy**: see exactly which commits and files will change, and which files would be deleted.
- **Safe production**: allow only commits that are already in `main`, and confirm by typing the environment name.
- **Database backup** (`mysqldump`) before every deploy, plus a post-deploy command for migrations, cache clearing, `composer install`…
- **Rollback**: deploy any previous commit.
- **Only what is in Git reaches the server**: no stray local files, `.env` or `.DS_Store` on production.
- **Keep list**: configs, uploads and caches on the server are never overwritten or deleted.
- **Secrets in the macOS Keychain**: SSH passwords, key passphrases and DB passwords never touch the settings file.
- **SSH keys with passphrases** and **password login** are supported; prompts appear in the app window.
- **One shared Git key** for all servers: sent to the server only for the duration of a deploy, never stored there.
- **Nothing to install on the server**: just SSH, `git` and `rsync`. Works great with HestiaCP, cPanel-style hosting with SSH, VPS, Ubuntu / Debian servers.
- **Native macOS app** with Dock icon, menus and keyboard shortcuts. UI in **English, Ukrainian and Russian**.

## How it works

```
Mac (Deployer)                     Git service                    Server
──────────────                     ───────────                    ──────
Commit ──► Push ─────────────────► Bitbucket / GitHub / …
Deploy ──► SSH ──────────────────────────────────────────────────► git fetch (from the Git service)
                                                                   DB backup → rsync to the site folder
                                                                   → post-deploy command
```

## Requirements

- macOS 12 or later
- Xcode Command Line Tools (`xcode-select --install`): provides `git`, `python3` and `swiftc`
- On the server: SSH access, `git`, `rsync` (and `mysqldump` for database backups)

## Install

```bash
git clone https://github.com/IRekunenko/Deployer---MacOS.git
cd Deployer---MacOS
./build_app.sh --install
```

This builds `Deployer.app` and installs it into `/Applications`. Run the same command again to update.
Without `--install` the app is built into `dist/`.

## Quick start

1. **⎇ Git**: check the Git services you use (Bitbucket and GitHub are preconfigured; add GitLab or a self-hosted one if needed). Create the **shared Git key** and add its public part to your repositories' *Access keys* / *Deploy keys*.
2. **🖥 Servers**: add a server (IP, port, user) and pick an SSH key, or use a user name and password. Add this Mac's public key to the server (e.g. HestiaCP: *User → SSH Keys*). Press **Test connection**.
3. **＋ Project**: choose the local project folder, select which repositories to push to and which one the server pulls from, then configure environments (server, site folder, branch, files to keep, optional database).
4. Use **Commit**, **Push**, **Check**, **Preview** and **🚀 Deploy**.

> Before the first deploy, press **Preview** and review the list of deletions: anything that exists only on the server (configs, uploads, cache…) must be added to *Keep on the server*.

## Security

- Passwords are stored in the **macOS Keychain** only. The settings file contains no secrets.
- Secrets are sent to the server via the SSH session's stdin, never on a command line, so they do not appear in the server's process list. Temporary files are created with mode `600` and removed when the job ends.
- The local web UI listens on `127.0.0.1` only and rejects cross-site requests.
- Prefer read-only *Access keys* / *Deploy keys* for the shared Git key. Avoid connecting as `root`; use the site owner's account instead.

## Where things are stored

| What | Where |
|---|---|
| Settings | `~/Library/Application Support/Deployer/projects.json` |
| Secrets | Keychain, service `Deployer` |
| App log | `~/Library/Logs/Deployer.log` |
| Deploy history | `~/Library/Application Support/Deployer/history.log` (Mac), `~/deploy/history.log` (server) |
| Server-side working copies and DB backups | `~/deploy/` of the SSH user |

## Project layout

```
src/app.py             local HTTP server: API, git, SSH, Keychain
src/index.html         the user interface
src/remote_deploy.sh   script executed on the server (sent over SSH)
src/askpass.py         SSH_ASKPASS helper: password prompts in the app window
src/locales/*.json     UI translations (English strings are the keys)
macos/main.swift       native macOS window (WebKit) and app lifecycle
build_app.sh           builds Deployer.app
```

## Translations

English strings in the code are the translation keys. To add a language, copy `src/locales/uk.json` to `src/locales/<code>.json`, translate the values and add the code to `LANGS` in `src/app.py` and to the language selector in `src/index.html`. Pull requests are welcome.

## Contributing

Issues and pull requests are welcome: bug reports, new Git services, translations, ideas.

## License

[MIT](LICENSE) © Ihor Rekunenko

<sub>Keywords: macOS deploy tool, one-click deploy, deploy to VPS over SSH, rsync deploy, git deploy, Bitbucket deploy, GitHub deploy, HestiaCP deploy, PHP / Laravel / WordPress deploy, FileZilla alternative, simple CI/CD alternative, vibe coding, Claude Code, Cursor.</sub>
