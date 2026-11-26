#!/bin/bash
# Runs ON THE SERVER. Nothing needs to be installed there:
# app.py sends this file over SSH as `ssh host "bash -s"` with the variables prepended.
#
# Parameters (environment variables):
#   MODE       check | preview | deploy | repokey
#   KEY        unique environment key, e.g. project-prod
#   REPO       repository git URL
#   GIT_PROVIDER  bitbucket | github | gitlab | other (for hints)
#   GIT_KEY    per-project repository key on the server, e.g. ~/.ssh/deployer_project
#              (if missing, the user's default keys are used)
#   GIT_KEY_DATA  shared key from the Mac (private key contents). Written to a temporary
#              file only while the script runs and removed afterwards.
#   REF        branch or commit
#   TARGET     site folder, e.g. /home/user/web/example.com/public_html
#   DB_NAME, DB_USER, DB_PASS, DB_HOST  (optional) database to back up before deploying
#   PHP        (optional) PHP binary, e.g. php8.2
#   POST_DEPLOY (optional) command run in TARGET after copying
#   EXCLUDES   rsync excludes, one per line
#   ONLY_FROM  (optional) allow only commits contained in this branch (for production)

# Everything is inside a function: bash reads the whole script first, so commands
# that read stdin cannot swallow the rest of it.
main() {
  set -euo pipefail
  exec </dev/null
  : "${MODE:?}" "${KEY:?}" "${REPO:?}" "${REF:?}" "${TARGET:?}"

  CLEAN=()
  cleanup() { if [ ${#CLEAN[@]} -gt 0 ]; then rm -f "${CLEAN[@]}"; fi; }
  trap cleanup EXIT

  GK="${GIT_KEY:-}"; GK="${GK/#\~/$HOME}"
  KEY_SRC=""
  export GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes"
  if [ -n "${GIT_KEY_DATA:-}" ]; then
    TK=$(mktemp); CLEAN+=("$TK"); chmod 600 "$TK"
    printf '%s\n' "$GIT_KEY_DATA" > "$TK"
    unset GIT_KEY_DATA
    GIT_SSH_COMMAND="$GIT_SSH_COMMAND -i $TK -o IdentitiesOnly=yes"
    KEY_SRC="shared Git key"
  elif [ -n "$GK" ] && [ -f "$GK" ]; then
    GIT_SSH_COMMAND="$GIT_SSH_COMMAND -i $GK -o IdentitiesOnly=yes"
    KEY_SRC="project key on the server"
  fi
  case "${GIT_PROVIDER:-}" in
    github)    KEY_HINT="GitHub → repository → Settings → Deploy keys → Add deploy key (read-only)" ;;
    bitbucket) KEY_HINT="Bitbucket → repository → Repository settings → Access keys → Add key" ;;
    gitlab)    KEY_HINT="GitLab → project → Settings → Repository → Deploy keys" ;;
    *)         KEY_HINT="repository settings → Deploy keys / Access keys (read-only)" ;;
  esac

  # ---------- per-project repository key ----------
  if [ "$MODE" = repokey ]; then
    if [ -z "$GK" ]; then echo "❌ Key path is not set"; return 1; fi
    if [ ! -f "$GK" ]; then
      mkdir -p "$(dirname "$GK")"; chmod 700 "$(dirname "$GK")"
      ssh-keygen -q -t ed25519 -N "" -C "$(whoami)@$(hostname)-${KEY%-*}" -f "$GK"
      echo "✓ Key created: $GK"
    else
      echo "Key already exists: $GK"
    fi
    echo "Add it here: $KEY_HINT"
    echo
    cat "$GK.pub"
    echo
    if git ls-remote -q "$REPO" HEAD >/dev/null 2>&1; then
      echo "✓ Repository access already works"
    else
      echo "⚠️  No access yet: add the key and press Check"
    fi
    return 0
  fi
  PHP="${PHP:-php}"
  D="${DEPLOY_HOME:-$HOME/deploy}"
  SRC="$D/src/$KEY"
  STATE="$D/state/$KEY.rev"
  mkdir -p "$D/src" "$D/state" "$D/backups"

  EX=(--exclude=.git)
  while IFS= read -r e; do
    if [ -n "$e" ]; then EX+=(--exclude="$e"); fi
  done <<< "${EXCLUDES:-}"

  # DB credentials → temporary file with mode 600 (the password never shows up in ps)
  MYSQL_OPTS=()
  if [ -n "${DB_NAME:-}" ] && [ -n "${DB_USER:-}" ]; then
    MYCNF=$(mktemp); CLEAN+=("$MYCNF")
    esc() { local s=${1//\\/\\\\}; printf '%s' "${s//\"/\\\"}"; }
    printf '[client]\nuser="%s"\npassword="%s"\nhost="%s"\n' \
      "$(esc "$DB_USER")" "$(esc "${DB_PASS:-}")" "$(esc "${DB_HOST:-localhost}")" > "$MYCNF"
    MYSQL_OPTS=(--defaults-extra-file="$MYCNF")
  fi
  export DB_NAME DB_USER DB_PASS DB_HOST   # also available to the post-deploy command

  # ---------- check ----------
  if [ "$MODE" = check ]; then
    echo "Server: $(hostname), user: $(whoami)"
    for c in git rsync; do
      if command -v "$c" >/dev/null; then echo "✓ $c is installed"; else echo "❌ $c is not installed"; fi
    done
    if [ -d "$TARGET" ]; then echo "✓ Site folder exists"; else echo "❌ Folder not found: $TARGET"; fi
    if [ -w "$TARGET" ]; then echo "✓ Write permission OK"; else echo "❌ No write permission: $TARGET"; fi
    if git ls-remote -q "$REPO" HEAD >/dev/null 2>&1; then
      echo "✓ Repository access OK${KEY_SRC:+ ($KEY_SRC)}"
    else
      echo "❌ No access to $REPO${KEY_SRC:+ ($KEY_SRC)}"
      echo "   Press 🔑 and add the key shown: $KEY_HINT"
    fi
    if command -v "$PHP" >/dev/null; then echo "✓ $PHP $("$PHP" -r 'echo PHP_VERSION;')"; else echo "⚠️  $PHP not found"; fi
    if [ -n "${DB_NAME:-}" ]; then
      if mysql "${MYSQL_OPTS[@]}" -e "USE \`$DB_NAME\`" 2>/dev/null; then
        echo "✓ Database access OK: $DB_NAME"
      else
        echo "❌ No access to database $DB_NAME (check the DB user and password)"
      fi
    fi
    if [ -f "$STATE" ]; then echo "Currently deployed: $(cut -c1-7 "$STATE")"; fi
    return 0
  fi

  # Prevent two simultaneous deploys to the same environment
  exec 9>"$D/state/$KEY.lock"
  if command -v flock >/dev/null && ! flock -n 9; then echo "❌ Another deploy of $KEY is still running"; return 1; fi

  # ---------- fetch the code ----------
  if [ ! -d "$SRC/.git" ]; then
    echo "→ Cloning the repository…"
    git clone -q "$REPO" "$SRC"
  fi
  cd "$SRC"
  git remote set-url origin "$REPO"
  git fetch -q --prune --tags origin

  if NEW=$(git rev-parse --verify -q "origin/$REF^{commit}"); then :
  elif NEW=$(git rev-parse --verify -q "$REF^{commit}"); then :
  else echo "❌ Branch or commit not found in the repository: $REF (not pushed?)"; return 1
  fi

  if [ -n "${ONLY_FROM:-}" ] && ! git merge-base --is-ancestor "$NEW" "origin/$ONLY_FROM"; then
    echo "❌ Only commits already in branch $ONLY_FROM can be deployed here"
    return 1
  fi

  # ---------- what will change ----------
  OLD=$(cat "$STATE" 2>/dev/null || true)
  echo "══ $KEY ← $REF @ ${NEW:0:7}"
  echo "   $(git log -1 --format='%an, %ar: %s' "$NEW")"
  if [ -z "$OLD" ]; then
    echo "First deploy of this environment (previous version unknown)"
  elif [ "$OLD" = "$NEW" ]; then
    echo "This commit is already deployed."
  elif git cat-file -e "$OLD^{commit}" 2>/dev/null; then
    if git merge-base --is-ancestor "$NEW" "$OLD"; then
      echo "⚠️  ROLLBACK: server has ${OLD:0:7}, going back to ${NEW:0:7}"
      echo "Commits that will be removed:"
      git log --oneline --no-decorate -n 40 "$NEW..$OLD"
    else
      echo "New commits (server has ${OLD:0:7}):"
      git log --oneline --no-decorate -n 40 "$OLD..$NEW"
    fi
    CHANGED=$(git diff --name-only "$OLD" "$NEW")
    if grep -qiE '(^|/)migrations?/' <<< "$CHANGED"; then echo "⚠️  Database migrations changed"; fi
  else
    echo "Server has ${OLD:0:7} (not found in history)"
  fi

  git checkout -q -f --detach "$NEW"

  if [ "$MODE" = preview ]; then
    echo
    echo "Files that will change in $TARGET:"
    CH=$(rsync -rlc --delete --dry-run --itemize-changes "${EX[@]}" ./ "$TARGET/" | grep -vE '^\.[fdL][.tT]+ ' || true)
    if [ -z "$CH" ]; then
      echo "   (no file changes)"
    else
      printf '%s\n' "$CH" | sed -n '1,200p'
      N=$(grep -c . <<< "$CH" || true)
      DEL=$(grep -c '^\*deleting' <<< "$CH" || true)
      echo "Total: $N, deletions: $DEL"
      if [ "$DEL" -gt 0 ]; then
        echo "⚠️  Review the deletions! If a file must stay on the server, add it to the keep list."
      fi
    fi
    return 0
  fi

  # ---------- deploy ----------
  if [ -n "${DB_NAME:-}" ]; then
    B="$D/backups/$KEY-$(date +%F_%H%M%S).sql.gz"
    echo "→ Database backup: $DB_NAME"
    mysqldump "${MYSQL_OPTS[@]}" --single-transaction "$DB_NAME" | gzip > "$B"
    echo "   $B ($(du -h "$B" | cut -f1))"
    ls -t "$D/backups/$KEY-"*.sql.gz | tail -n +21 | xargs -r rm --   # keep the 20 most recent
  fi

  echo "→ Copying files to $TARGET"
  mkdir -p "$TARGET"
  OUT=$(rsync -rlc --delete --itemize-changes "${EX[@]}" ./ "$TARGET/")
  OUT=$(grep -vE '^\.[fdL][.tT]+ ' <<< "$OUT" || true)
  N=$(grep -c . <<< "$OUT" || true)
  DEL=$(grep -c '^\*deleting' <<< "$OUT" || true)
  echo "   files changed: $N (deleted: $DEL)"

  if [ -n "${POST_DEPLOY:-}" ]; then
    echo "→ $POST_DEPLOY"
    cd "$TARGET"
    PHP="$PHP" bash -c "$POST_DEPLOY"
  fi

  echo "$NEW" > "$STATE"
  echo "$(date '+%F %T')  $KEY  ${OLD:0:7} → ${NEW:0:7}  ($REF)" >> "$D/history.log"
  echo "✅ Done: $KEY → ${NEW:0:7}"
}
main
